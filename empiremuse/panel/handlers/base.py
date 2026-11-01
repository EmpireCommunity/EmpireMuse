"""Base dos handlers do painel: cabeçalhos de segurança e páginas de erro genéricas."""

from __future__ import annotations

import html
import logging
from typing import Any, Optional

import tornado.web

from empiremuse.panel.services.base import panel_translator
from empiremuse.panel.sessions import PanelUser, Session, SessionStore
from empiremuse.panel.settings import PanelSettings
from empiremuse.rules.error_code import safe_error_code

logger = logging.getLogger(__name__)

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' https://cdn.discordapp.com data:; connect-src 'self'; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
HSTS_VALUE = "max-age=31536000"
ERROR_TEMPLATE = "erro.html"
SESSION_COOKIE = "painel_sessao"
HOST_PREFIX = "__Host-"
CDN_AVATARS = "https://cdn.discordapp.com"
DEFAULT_AVATAR_COUNT = 6
DISCORD_ID_SHIFT = 22

_ERROR_TEXTS = {
    400: ("Requisição inválida", "Não foi possível entender a solicitação."),
    403: ("Acesso negado", "Você não tem permissão para acessar este recurso."),
    404: ("Página não encontrada", "O endereço que você tentou acessar não existe."),
    405: ("Método não permitido", "Esta ação não é permitida neste endereço."),
    429: ("Muitas requisições", "Aguarde um instante e tente novamente."),
    500: ("Erro interno", "Algo deu errado do nosso lado. Tente novamente em instantes."),
}
_DEFAULT_ERROR_TEXT = ("Não foi possível concluir", "Não foi possível concluir a solicitação.")
_FALLBACK_ERROR_BODY = '<!doctype html><meta charset="utf-8"><title>Erro</title><p>Erro {code}.</p>{reference}'


def avatar_url_for(user: Optional[PanelUser]) -> str:
    """URL do avatar do usuário (ou o avatar padrão do Discord); "" quando deslogado."""
    if user is None:
        return ""
    if user.avatar:
        return f"{CDN_AVATARS}/avatars/{user.id}/{user.avatar}.png?size=64"
    index = (int(user.id) >> DISCORD_ID_SHIFT) % DEFAULT_AVATAR_COUNT if user.id.isdigit() else 0
    return f"{CDN_AVATARS}/embed/avatars/{index}.png"


class PanelResponseMixin:
    """Cabeçalhos de segurança, sessão atual e página de erro genérica (``erro.html``)."""

    @property
    def panel_settings(self) -> PanelSettings:
        return self.application.settings["panel_settings"]

    @property
    def uses_host_prefix(self) -> bool:
        return self.panel_settings.is_https and not self.panel_settings.insecure_cookies

    def cookie_name(self, base: str) -> str:
        return HOST_PREFIX + base if self.uses_host_prefix else base

    def cookie_kwargs(self) -> dict[str, Any]:
        return {"httponly": True, "samesite": "Lax", "path": "/", "secure": not self.panel_settings.insecure_cookies}

    def clear_panel_cookie(self, base: str) -> None:
        self.clear_cookie(self.cookie_name(base), **self.cookie_kwargs())

    @property
    def session_id(self) -> Optional[str]:
        value = self.get_signed_cookie(self.cookie_name(SESSION_COOKIE))
        return value.decode("ascii", "ignore") if value else None

    @property
    def current_session(self) -> Optional[Session]:
        if not hasattr(self, "_current_session"):
            store: SessionStore = self.application.settings["session_store"]
            self._current_session = store.get(self.session_id)
        return self._current_session

    def get_template_namespace(self) -> dict[str, Any]:
        namespace = super().get_template_namespace()
        user = self.current_session.user if self.current_session else None
        # ``_`` (I-13): devolve ``str`` já traduzido; nos templates só em texto ou atributo entre aspas.
        translator = panel_translator(self.application.settings.get("pool"))
        namespace.update(usuario=user, avatar_url=avatar_url_for(user), _=translator)
        return namespace

    def _request_summary(self) -> str:
        # Sem query string: o ``code`` e o ``state`` do OAuth não podem ir parar em logs.
        return f"{self.request.method} {self.request.path} ({self.request.remote_ip})"

    def log_exception(self, typ: Any, value: Any, tb: Any) -> None:
        """Como o padrão do Tornado, mas sem ``repr(request)`` (o URI traria ``code`` e ``state`` do OAuth)."""
        summary = self._request_summary()
        if isinstance(value, tornado.web.HTTPError):
            if value.log_message:
                try:
                    detail = value.log_message % value.args
                except (
                    Exception
                ):  # o modelo vem de quem levantou o HTTPError: um formato inválido não pode derrubar o log
                    detail = value.log_message
                logger.warning("%d %s: %s", value.status_code, summary, detail)
            return
        code = self._error_code = safe_error_code()  # o mesmo código vai ao log e à página de erro
        logger.error(
            "Uncaught exception %s%s",
            summary,
            f" [{code}]" if code else "",
            exc_info=(typ, value, tb),
            # K15: o agregador usa o código para apontar o traceback já impresso.
            extra={"error_code": code} if code else None,
        )

    def set_default_headers(self) -> None:
        for name, value in SECURITY_HEADERS.items():
            self.set_header(name, value)
        if self.panel_settings.is_https:
            self.set_header("Strict-Transport-Security", HSTS_VALUE)

    def render_error(self, status_code: int, titulo: str, mensagem: str, referencia: str = "") -> None:
        """Responde com a página de erro genérica, sem detalhes internos (``referencia``: código do erro, se houver)."""
        self.set_status(status_code)
        self.set_header("Cache-Control", "no-store")
        self.render(ERROR_TEMPLATE, titulo=titulo, codigo=status_code, mensagem=mensagem, referencia=referencia)

    def write_error(self, status_code: int, **kwargs: Any) -> None:
        """Responde sempre com a página genérica, sem stack trace nem detalhes internos."""
        titulo, mensagem = _ERROR_TEXTS.get(status_code, _DEFAULT_ERROR_TEXT)
        referencia = getattr(self, "_error_code", "") if status_code >= 500 else ""  # só o 500 real tem código
        try:
            self.render_error(status_code, titulo, mensagem, referencia)
        except Exception:
            logger.exception("Falha ao renderizar %s", ERROR_TEMPLATE)
            self.clear()
            self.set_status(status_code)
            self.set_header("Content-Type", "text/html; charset=UTF-8")
            self.set_header("Cache-Control", "no-store")
            reference = f"<p>Código do erro: <code>{html.escape(referencia)}</code></p>" if referencia else ""
            self.finish(_FALLBACK_ERROR_BODY.format(code=int(status_code), reference=reference))


class PanelHandler(PanelResponseMixin, tornado.web.RequestHandler):
    """Handler base das páginas HTML do painel."""

    def render_page(self, template_name: str, **context: Any) -> None:
        self.set_header("Cache-Control", "no-store")
        self.render(template_name, **context)


class PanelStaticHandler(PanelResponseMixin, tornado.web.StaticFileHandler):
    """Arquivos de ``panel/static/`` com os mesmos cabeçalhos e erros do painel."""


class NotFoundHandler(PanelHandler):
    """Qualquer rota desconhecida sob /painel responde com o 404 genérico."""

    def prepare(self) -> None:
        raise tornado.web.HTTPError(404)
