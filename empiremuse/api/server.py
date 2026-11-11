"""O listener próprio da API (contrato T-456 §2 [M3, N1]): outra aplicação Tornado, outro socket, nunca o de ``web_app``."""

from __future__ import annotations

import ipaddress
import logging
import time
from typing import Any, Callable

import tornado.httpserver
import tornado.iostream
import tornado.web

from empiremuse.api.base import NotFoundHandler, log_request
from empiremuse.api.context import ApiContext
from empiremuse.api.handlers import HealthHandler, MetaHandler, SessionHandler, SessionsHandler, StatusHandler
from empiremuse.api.handlers_account import account_routes
from empiremuse.api.handlers_erasure import erasure_routes
from empiremuse.api.handlers_guilds import guild_routes
from empiremuse.api.handlers_jukebox import jukebox_routes
from empiremuse.api.handlers_radio import radio_routes
from empiremuse.api.handlers_stations import station_routes
from empiremuse.api.settings import ExternalPanelSettings, IPNetwork

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"
MAX_BODY_BYTES = 16 * 1024
DENIED_LOG_INTERVAL_SECONDS = 60
CONNECTION_TIMEOUT_SECONDS = 60  # conexão parada e corpo que não chega


def build_api_app(ctx: ApiContext) -> tornado.web.Application:
    args = {"ctx": ctx}
    # O nome de cada rota com parâmetro é o caminho dele no ``openapi.yaml`` (o teste de contrato confere os dois lados).
    data_routes = [
        tornado.web.url(API_PREFIX + pattern, handler, args, name=API_PREFIX + template)
        for pattern, handler, template in [
            *account_routes(),
            *erasure_routes(),
            *guild_routes(),
            *jukebox_routes(),
            *radio_routes(),
            *station_routes(),
        ]
    ]
    return tornado.web.Application(
        [
            (API_PREFIX + "/meta", MetaHandler, args),
            (API_PREFIX + "/health", HealthHandler, args),
            (API_PREFIX + "/status", StatusHandler, args),
            (API_PREFIX + "/sessions", SessionsHandler, args),
            (API_PREFIX + "/session", SessionHandler, args),
            *data_routes,
        ],
        default_handler_class=NotFoundHandler,
        default_handler_args=args,
        log_function=log_request,
    )


def peer_allowed(address: Any, networks: tuple[IPNetwork, ...]) -> bool:
    """O par TCP está em alguma das sub-redes? Endereço ilegível (soquete Unix, texto) nunca está."""
    try:
        ip = ipaddress.ip_address(address[0])
    except TypeError, IndexError, ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip.version == network.version and ip in network for network in networks)


class ApiHTTPServer(tornado.httpserver.HTTPServer):
    """``HTTPServer`` que confere o par TCP de cada conexão: fora das sub-redes, fecha sem resposta (nunca ``remote_ip``)."""

    # O ``HTTPServer`` do Tornado é configurável: o construtor passa por ``initialize``.
    def initialize(  # type: ignore[override]
        self,
        app: tornado.web.Application,
        networks: tuple[IPNetwork, ...],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().initialize(
            app,
            xheaders=False,
            max_body_size=MAX_BODY_BYTES,
            idle_connection_timeout=CONNECTION_TIMEOUT_SECONDS,
            body_timeout=CONNECTION_TIMEOUT_SECONDS,
        )
        self._networks = networks
        self._clock = clock
        self._denied = 0
        self._next_denied_log = 0.0

    def handle_stream(self, stream: tornado.iostream.IOStream, address: Any) -> None:
        if self._networks and not peer_allowed(address, self._networks):
            self._note_denied()
            stream.close()
            return
        super().handle_stream(stream, address)

    def _note_denied(self) -> None:
        self._denied += 1
        now = self._clock()
        if now >= self._next_denied_log:
            logger.warning(
                "API do painel externo: %d conexão(ões) fora de EXTERNAL_PANEL_API_CIDRS fechada(s).", self._denied
            )
            self._denied, self._next_denied_log = 0, now + DENIED_LOG_INTERVAL_SECONDS


def listen(ctx: ApiContext, settings: ExternalPanelSettings) -> ApiHTTPServer:
    """Sobe o listener da API em ``bind:port`` (levanta ``OSError`` se a porta não abrir)."""
    server = ApiHTTPServer(build_api_app(ctx), settings.cidrs, clock=ctx.clock)
    server.listen(settings.port, address=settings.bind)
    return server
