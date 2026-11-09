"""Login com Discord (authorization code + state + PKCE), callback e logout."""

from __future__ import annotations

import hmac
import logging
import re
import secrets
from typing import Optional

from empiremuse.panel.handlers.base import SESSION_COOKIE, PanelHandler
from empiremuse.panel.handlers.erase import ERASE_PATH
from empiremuse.panel.handlers.pages import INDEX_PATH
from empiremuse.panel.oauth import (
    DiscordOAuthClient,
    PROMPT_CONSENT,
    PROMPT_NONE,
    DiscordOAuthError,
    make_pkce_pair,
    parse_managed_guilds,
    parse_user,
)
from empiremuse.panel.sessions import LoginAttempt

logger = logging.getLogger(__name__)

LOGIN_COOKIE = "painel_login"
ATTEMPT_COOKIE_MAX_AGE = 10 * 60
STATE_BYTES = 32
NEXT_MAX_LENGTH = 200
_SAFE_NEXT = re.compile(r"/painel/[A-Za-z0-9\-._~/%?=&+,:@]*", re.ASCII)
_ENCODED_TRAVERSAL = re.compile(r"%(?:2e|2f|5c)", re.IGNORECASE)  # '.', '/' e '\\' codificados

_MSG_EXPIRED = (
    "Link de login expirado",
    "Sua tentativa de login expirou ou já foi usada. Volte ao início e entre novamente.",
)
_MSG_DENIED = (
    "Acesso negado",
    "Você cancelou a autorização no Discord. Para usar o painel, entre novamente e autorize o acesso.",
)
_MSG_DISCORD = (
    "Discord indisponível",
    "Não foi possível falar com o Discord agora. Tente novamente em alguns instantes.",
)
_MSG_RATE = ("Muitas tentativas", "Você tentou entrar muitas vezes seguidas. Aguarde um minuto e tente de novo.")
_MSG_UNAVAILABLE = ("Login indisponível", "O login com Discord não está disponível no momento.")


def safe_next_path(value: str) -> str:
    """Aceita só caminhos internos ``/painel/...``; qualquer outra coisa volta para o início."""
    if (
        not value
        or len(value) > NEXT_MAX_LENGTH
        or "//" in value
        or ".." in value
        or _ENCODED_TRAVERSAL.search(value)
        or not _SAFE_NEXT.fullmatch(value)
    ):
        return INDEX_PATH
    return value


class AuthHandler(PanelHandler):
    rate_scope = ""

    def prepare(self) -> None:
        self.set_header("Cache-Control", "no-store")

    def rate_limited(self) -> bool:
        """Aplica o limite por IP; responde 429 e devolve True quando excedido."""
        limiter = self.application.settings["rate_limiter"]
        if limiter.allow(f"{self.rate_scope}:{self.request.remote_ip}"):
            return False
        self.set_header("Retry-After", "60")
        self.render_error(429, *_MSG_RATE)
        return True

    def oauth_client(self) -> Optional[DiscordOAuthClient]:
        return self.application.settings["oauth_provider"]()


class LoginHandler(AuthHandler):
    rate_scope = "login"

    def get(self) -> None:
        if self.rate_limited():
            return
        oauth = self.oauth_client()
        if oauth is None:
            self.render_error(503, *_MSG_UNAVAILABLE)
            return
        state = secrets.token_urlsafe(STATE_BYTES)
        verifier, challenge = make_pkce_pair()
        next_path = safe_next_path(self.get_argument("next", ""))
        attempt_id = self.application.settings["attempt_store"].create(state, verifier, next_path)
        self.set_signed_cookie(
            self.cookie_name(LOGIN_COOKIE),
            attempt_id,
            expires_days=None,
            max_age=ATTEMPT_COOKIE_MAX_AGE,
            **self.cookie_kwargs(),
        )
        # Só o caminho de apagar dados pede consentimento de novo: o login silencioso (``prompt=none``) não prova presença de ninguém.
        prompt = PROMPT_CONSENT if next_path == ERASE_PATH else PROMPT_NONE
        self.redirect(oauth.authorize_url(state, challenge, prompt))


class CallbackHandler(AuthHandler):
    rate_scope = "callback"

    async def get(self) -> None:
        if self.rate_limited():
            return
        attempt = self._take_attempt()
        if self.get_argument("error", None) == "access_denied":
            self.render_error(403, *_MSG_DENIED)
            return
        code = self.get_argument("code", "")
        state = self.get_argument("state", "")
        if attempt is None or not code or not state or not hmac.compare_digest(state.encode(), attempt.state.encode()):
            self.render_error(400, *_MSG_EXPIRED)
            return
        oauth = self.oauth_client()
        if oauth is None:
            self.render_error(503, *_MSG_UNAVAILABLE)
            return
        identity = await self._read_identity(oauth, code, attempt)
        if identity is None:
            self.render_error(502, *_MSG_DISCORD)
            return
        self._start_session(*identity)
        self.redirect(attempt.next_path)

    def _take_attempt(self) -> Optional[LoginAttempt]:
        name = self.cookie_name(LOGIN_COOKIE)
        raw = self.get_signed_cookie(name)
        self.clear_panel_cookie(LOGIN_COOKIE)
        attempt_id = raw.decode("ascii", "ignore") if raw else None
        return self.application.settings["attempt_store"].consume(attempt_id)

    async def _read_identity(self, oauth: DiscordOAuthClient, code: str, attempt: LoginAttempt):
        """Troca o código e lê usuário e servidores; o token é sempre revogado e nunca guardado."""
        token = None
        try:
            token = await oauth.exchange_code(code, attempt.code_verifier)
            user = parse_user(await oauth.fetch_user(token))
            guilds = parse_managed_guilds(await oauth.fetch_guilds(token))
            return user, guilds
        except DiscordOAuthError as exc:
            logger.warning("Falha no login com Discord: %s", exc)
            return None
        finally:
            if token:
                await self._revoke(oauth, token)

    async def _revoke(self, oauth: DiscordOAuthClient, token: str) -> None:
        try:
            await oauth.revoke(token)
        except DiscordOAuthError as exc:
            logger.warning("Não foi possível revogar o token do Discord: %s", exc)

    def _start_session(self, user, guilds) -> None:
        store = self.application.settings["session_store"]
        store.destroy(self.session_id)  # nunca reaproveita um id anterior (sem fixação de sessão)
        session_id = store.create(user, guilds)
        self.set_signed_cookie(self.cookie_name(SESSION_COOKIE), session_id, expires_days=None, **self.cookie_kwargs())


class LogoutHandler(AuthHandler):
    """Somente POST (o XSRF é exigido pelo Tornado antes de chegar aqui)."""

    def post(self) -> None:
        self.application.settings["session_store"].destroy(self.session_id)
        self.clear_panel_cookie(SESSION_COOKIE)
        self.redirect(INDEX_PATH)
