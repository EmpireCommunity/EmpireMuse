"""B6: substituto do genérico no modo externo válido; nunca lê destino, caminho ou query do visitante."""

import logging

import tornado.web

logger = logging.getLogger(__name__)


class ExternalPanelRedirectHandler(tornado.web.RequestHandler):
    def initialize(self, destination: str) -> None:
        self.destination = destination  # base_url já validada por ExternalPanelSettings, nunca por argumento HTTP

    def set_default_headers(self) -> None:
        self.set_header("Cache-Control", "no-store")
        self.set_header("Referrer-Policy", "no-referrer")
        self.set_header("X-Content-Type-Options", "nosniff")

    def prepare(self) -> None:
        if self.request.method not in ("GET", "HEAD"):
            raise tornado.web.HTTPError(404)

    def get(self) -> None:
        self.redirect(self.destination, permanent=False, status=302)

    def head(self) -> None:
        self.get()

    def write_error(self, status_code: int, **kwargs) -> None:
        self.set_header("Content-Type", "text/plain; charset=utf-8")
        self.finish("Not Found")

    def log_exception(self, typ, value, tb) -> None:
        logger.error("Falha no redirecionamento do painel (%s).", typ.__name__)


def _log_request(handler: tornado.web.RequestHandler) -> None:
    logger.log(
        logging.WARNING if handler.get_status() >= 400 else logging.DEBUG,
        "%d %s /painel",
        handler.get_status(),
        handler.request.method,
    )


def build_redirect_app(base_url: str) -> tornado.web.Application:
    """Recebe SOMENTE a base_url validada do modo externo cujo listener abriu. Não cria OAuth, sessão, serviço nem tarefa."""
    return tornado.web.Application(
        [(r"/painel(?:/.*)?", ExternalPanelRedirectHandler, {"destination": base_url})], log_function=_log_request
    )
