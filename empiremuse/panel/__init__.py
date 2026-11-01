"""Painel web do EmpireMuse: sub-aplicação Tornado isolada sob ``/painel``.

Roda no mesmo processo, porta e loop do servidor web (``web_app``). ``make_router`` separa
``/painel`` do aplicativo principal (``/`` e ``/ws`` permanecem intactos).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

import tornado.routing
import tornado.web

from empiremuse.panel.authz import GuildAuthorizer
from empiremuse.panel.handlers.account import AccountHandler, FavoritesExportHandler
from empiremuse.panel.handlers.auth import CallbackHandler, LoginHandler, LogoutHandler
from empiremuse.panel.handlers.base import NotFoundHandler, PanelStaticHandler
from empiremuse.panel.handlers.brand import BRAND_ROUTE, BrandHandler
from empiremuse.panel.handlers.pages import HealthHandler, IndexHandler, RedirectToIndexHandler
from empiremuse.panel.handlers.servers import ServerDetailHandler, ServersHandler
from empiremuse.panel.handlers.channel import ChannelHandler, ChannelTaskHandler
from empiremuse.panel.handlers.radio import (
    RadioChatHandler,
    RadioLocalHandler,
    RadioReserveHandler,
    RadiosHandler,
    RadioStationHandler,
    RadioStationsHandler,
)
from empiremuse.panel.handlers.settings import WRITE_LIMIT_PER_MINUTE, LinksExportHandler, SettingsHandler
from empiremuse.panel.oauth import DiscordOAuthClient, make_client_provider
from empiremuse.panel.ratelimit import RateLimiter
from empiremuse.panel.services.account import AccountService
from empiremuse.panel.services.channel import ChannelService
from empiremuse.panel.services.service import SettingsService
from empiremuse.panel.services.tasks import TaskRunner
from empiremuse.panel.sessions import LoginAttemptStore, SessionStore
from empiremuse.panel.settings import PanelSettings

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool

PANEL_DIR = Path(__file__).resolve().parent
PANEL_PREFIX = "/painel"
STATIC_URL_PREFIX = PANEL_PREFIX + "/static/"
logger = logging.getLogger(__name__)
_PANEL_PATH_PATTERN = PANEL_PREFIX + r"(?:/.*)?"


def _log_request(handler: tornado.web.RequestHandler) -> None:
    """Log de acesso sem query string (``code`` e ``state`` do OAuth ficam fora dos logs)."""
    status = handler.get_status()
    level = logging.WARNING if status >= 400 else logging.DEBUG
    logger.log(level, "%d %s %s", status, handler.request.method, handler.request.path)


def build_panel_app(
    pool: BotPool,
    settings: PanelSettings,
    *,
    oauth_client: Optional[DiscordOAuthClient] = None,
    clock: Callable[[], float] = time.monotonic,
) -> Optional[tornado.web.Application]:
    """Cria a aplicação do painel, ou ``None`` quando ele está desligado.

    ``oauth_client`` é injetável (testes); sem ele, o client_id vem de ``PANEL_OAUTH_CLIENT_ID`` ou da
    aplicação do primeiro bot do pool, resolvido no primeiro login.
    """
    if not settings.enabled:
        return None

    settings_service = SettingsService()
    task_runner = TaskRunner(clock=clock)
    return tornado.web.Application(
        [
            (PANEL_PREFIX, RedirectToIndexHandler),
            (PANEL_PREFIX + "/", IndexHandler),
            (PANEL_PREFIX + "/saude", HealthHandler),
            (PANEL_PREFIX + "/login", LoginHandler),
            (PANEL_PREFIX + "/callback", CallbackHandler),
            (PANEL_PREFIX + "/logout", LogoutHandler),
            (PANEL_PREFIX + "/conta", AccountHandler),
            (PANEL_PREFIX + "/conta/favoritos/exportar", FavoritesExportHandler),
            (PANEL_PREFIX + "/servidores", ServersHandler),
            (PANEL_PREFIX + r"/servidores/([^/]+)", ServerDetailHandler),
            (PANEL_PREFIX + r"/servidores/([^/]+)/links/exportar", LinksExportHandler),
            (PANEL_PREFIX + r"/servidores/([^/]+)/canal/tarefas/(.*)", ChannelTaskHandler),
            (PANEL_PREFIX + r"/servidores/([^/]+)/canal", ChannelHandler),
            (PANEL_PREFIX + r"/servidores/([^/]+)/radios/([^/]+)/estacoes", RadioStationsHandler),
            (PANEL_PREFIX + r"/servidores/([^/]+)/radios/([^/]+)/estacao", RadioStationHandler),
            (PANEL_PREFIX + r"/servidores/([^/]+)/radios/([^/]+)/local", RadioLocalHandler),
            (PANEL_PREFIX + r"/servidores/([^/]+)/radios/([^/]+)/reserva", RadioReserveHandler),
            (PANEL_PREFIX + r"/servidores/([^/]+)/radios/([^/]+)", RadioChatHandler),
            (PANEL_PREFIX + r"/servidores/([^/]+)/radios", RadiosHandler),
            (PANEL_PREFIX + r"/servidores/([^/]+)/([^/]+)", SettingsHandler),
            (BRAND_ROUTE, BrandHandler),
            (PANEL_PREFIX + "/.*", NotFoundHandler),
        ],
        debug=False,
        template_path=str(PANEL_DIR / "templates"),
        static_path=str(PANEL_DIR / "static"),
        static_url_prefix=STATIC_URL_PREFIX,
        static_handler_class=PanelStaticHandler,
        default_handler_class=NotFoundHandler,
        xsrf_cookies=True,
        xsrf_cookie_kwargs={"httponly": True, "samesite": "Lax", "secure": not settings.insecure_cookies},
        cookie_secret=settings.cookie_secret,
        log_function=_log_request,
        panel_settings=settings,
        pool=pool,
        session_store=SessionStore(settings, clock=clock),
        attempt_store=LoginAttemptStore(clock=clock),
        rate_limiter=RateLimiter(clock=clock),
        oauth_provider=make_client_provider(pool, settings, oauth_client),
        authorizer=GuildAuthorizer(pool, clock=clock),
        settings_service=settings_service,
        account_service=AccountService(settings_service),
        task_runner=task_runner,
        channel_service=ChannelService(task_runner, settings_service, settings.cookie_secret, clock),
        write_limiter=RateLimiter(limit=WRITE_LIMIT_PER_MINUTE, clock=clock),
    )


def make_router(
    main_app: tornado.web.Application, panel_app: Optional[tornado.web.Application]
) -> tornado.routing.Router:
    """Envia ``/painel`` e ``/painel/...`` ao painel e todo o resto ao aplicativo principal."""
    rules = []
    if panel_app is not None:
        rules.append(tornado.routing.Rule(tornado.routing.PathMatches(_PANEL_PATH_PATTERN), panel_app))
    rules.append(tornado.routing.Rule(tornado.routing.AnyMatches(), main_app))
    return tornado.routing.RuleRouter(rules)
