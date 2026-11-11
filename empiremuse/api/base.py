"""Base dos handlers da API: autenticação, limites, cabeçalhos, corpo JSON e o envelope de erro (contrato T-456 §2, §4, §7)."""

from __future__ import annotations

import json
import logging
import re
import secrets
from typing import Any, Optional

import tornado.web

from empiremuse.api.context import ApiContext
from empiremuse.api.errors import (
    BOT_UNAVAILABLE,
    FORBIDDEN,
    NOT_FOUND,
    SESSION_EXPIRED,
    VALIDATION_FAILED,
    ApiError,
    rate_limited,
)
from empiremuse.panel.sessions import Session
from empiremuse.rules.error_code import safe_error_code
from empiremuse.utils.user_ban import user_bans_of

logger = logging.getLogger(__name__)

SESSION_HEADER = "X-EM-Session"
REQUEST_ID_HEADER = "X-Request-ID"
JSON_CONTENT_TYPE = "application/json"
REQUEST_ID_BYTES = 8
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{8,64}", re.ASCII)
_SESSION_TOKEN = re.compile(r"[A-Za-z0-9_-]{20,128}", re.ASCII)
_READ_METHODS = frozenset({"GET", "HEAD"})

AUTH_NONE = "none"  # só o /health, que fica na porta interna
AUTH_KEY = "key"  # chave de serviço
AUTH_SESSION = "session"  # chave de serviço e token de sessão
AUTH_SESSION_OPTIONAL = "session_optional"  # a sessão, se vier e valer (reauth)


class ApiHandler(tornado.web.RequestHandler):
    """Toda rota da API herda daqui. Cookies são ignorados (nunca lidos) e toda resposta sai ``no-store``."""

    auth = AUTH_KEY
    creates_sessions = False  # a rota gasta também o balde de criação de sessões

    def initialize(self, ctx: ApiContext) -> None:
        self.ctx = ctx
        self.session_token: Optional[str] = None
        self.session: Optional[Session] = None
        self.key_fingerprint = ""
        incoming = self.request.headers.get(REQUEST_ID_HEADER, "")
        # o id do painel correlaciona os logs dos dois lados; nunca carrega o IP do usuário
        self.request_id = incoming if _REQUEST_ID.fullmatch(incoming) else secrets.token_hex(REQUEST_ID_BYTES)

    def set_default_headers(self) -> None:
        self.set_header("Cache-Control", "no-store")
        self.set_header("X-Content-Type-Options", "nosniff")

    # ------------------------------------------------------------------ autenticação e limites

    def prepare(self) -> None:
        self.set_header(REQUEST_ID_HEADER, self.request_id)
        if self.auth == AUTH_NONE:
            return
        self.key_fingerprint = self._authenticate_key()
        if self.auth != AUTH_KEY:
            self._load_session(required=self.auth == AUTH_SESSION)
        if self.session is not None:  # por usuário (não por sessão), antes do balde da chave
            limiter = self.ctx.limits.user_read if self.request.method in _READ_METHODS else self.ctx.limits.user_write
            self._spend(limiter, self.session.user.id)
        if self.creates_sessions:
            self._spend(self.ctx.limits.sessions, self.key_fingerprint)
        self._spend(self.ctx.limits.key, self.key_fingerprint)

    def _spend(self, limiter: Any, key: str) -> None:
        if not limiter.allow(key):
            raise rate_limited(limiter.retry_after(key))

    def _authenticate_key(self) -> str:
        peer = self.request.remote_ip  # sem ``xheaders`` neste listener: é o par TCP, nunca um cabeçalho
        failures = self.ctx.limits.key_failures
        blocked_for = failures.retry_after(peer)
        if blocked_for:
            logger.debug("API do painel externo: par bloqueado por falhas de chave (%s).", peer)
            raise rate_limited(blocked_for)
        scheme, _, credential = self.request.headers.get("Authorization", "").partition(" ")
        provided = credential.strip() if scheme.lower() == "bearer" else None
        fingerprint = self.ctx.keys.match(provided, self.ctx.wall_clock())
        if fingerprint is not None:
            return fingerprint
        if not failures.allow(peer):  # a falha que passa de 30 por minuto
            logger.warning("API do painel externo: excesso de falhas de chave de serviço do par %s.", peer)
            raise rate_limited(failures.retry_after(peer))
        logger.debug("API do painel externo: chave de serviço ausente, errada ou vencida (%s).", peer)
        raise ApiError(401, FORBIDDEN)

    def _load_session(self, *, required: bool) -> None:
        token = self.request.headers.get(SESSION_HEADER, "")
        session = None
        if _SESSION_TOKEN.fullmatch(token):
            store = self.ctx.store
            session = store.get_api(token, self.key_fingerprint)
            bans = user_bans_of(self.ctx.pool)
            if session is not None and bans is not None and bans.is_blocked(session.user.id):
                store.destroy_user(session.user.id)  # E5: o banido perde todas as sessões
                session = None
        if session is None:
            if required:
                raise ApiError(401, SESSION_EXPIRED)
            return
        self.session_token, self.session = token, session

    # ------------------------------------------------------------------ corpo e resposta

    def json_body(self) -> dict[str, Any]:
        """O corpo como objeto JSON; ``Content-Type`` fora de ``application/json``, JSON inválido ou não objeto = erro."""
        media = self.request.headers.get("Content-Type", "").partition(";")[0].strip().lower()
        if media != JSON_CONTENT_TYPE:
            raise ApiError(415, VALIDATION_FAILED, fields=["content_type"])
        try:
            data = json.loads(self.request.body)
        except ValueError, RecursionError:
            raise ApiError(400, VALIDATION_FAILED) from None
        if not isinstance(data, dict):
            raise ApiError(400, VALIDATION_FAILED)
        return data

    def write_error(self, status_code: int, **kwargs: Any) -> None:
        exc = kwargs["exc_info"][1] if "exc_info" in kwargs else None
        fields, retry_after = None, None
        if isinstance(exc, ApiError):
            code, fields, retry_after = exc.code, exc.fields, exc.retry_after
        elif status_code == 404:
            code = NOT_FOUND
        elif status_code == 405:
            code, fields = VALIDATION_FAILED, ["method"]
        elif status_code < 500:
            code = VALIDATION_FAILED
        else:
            code = BOT_UNAVAILABLE
        self.set_header(REQUEST_ID_HEADER, self.request_id)
        if retry_after is not None:
            self.set_header("Retry-After", str(retry_after))
        error: dict[str, Any] = {"code": code}
        if fields:
            error["fields"] = fields
        error["request_id"] = self.request_id
        self.finish({"error": error})

    def log_exception(self, typ: Any, value: Any, tb: Any) -> None:
        """Como o padrão do Tornado, mas sem ``repr(request)`` e sem corpo nem cabeçalhos (a chave, o token de sessão, o
        ``code`` e o ``code_verifier`` nunca vão para o log). Falha esperada (``HTTPError``) já sai no log de acesso."""
        if isinstance(value, tornado.web.HTTPError):
            return
        code = safe_error_code()
        logger.error(
            "Uncaught exception %s %s [%s]%s",
            self.request.method,
            self.request.path,
            self.request_id,
            f" [{code}]" if code else "",
            exc_info=(typ, value, tb),
            extra={"error_code": code} if code else None,
        )


class NotFoundHandler(ApiHandler):
    """Caminho fora da API: ``404`` no envelope, para qualquer método e sem exigir a chave."""

    auth = AUTH_NONE

    def prepare(self) -> None:
        super().prepare()
        raise ApiError(404, NOT_FOUND)


def log_request(handler: tornado.web.RequestHandler) -> None:
    """Log de acesso sem query string nem corpo (o ``code`` do OAuth e os tokens ficam fora dos logs)."""
    status = handler.get_status()
    level = logging.WARNING if status >= 500 else logging.DEBUG
    logger.log(
        level,
        "%d %s %s [%s]",
        status,
        handler.request.method,
        handler.request.path,
        getattr(handler, "request_id", "-"),
    )
