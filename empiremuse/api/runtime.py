"""Liga o modo externo ao bot: valida a configuração, cria o estado compartilhado e sobe o listener (contrato T-456 §2 e §6)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from empiremuse.api.context import ApiContext
from empiremuse.api.idempotency import IdempotencyStore
from empiremuse.api.keys import ServiceKeys
from empiremuse.api.limits import ApiLimits
from empiremuse.api.server import ApiHTTPServer, listen
from empiremuse.api.signing import PURPOSE_JUKEBOX, PURPOSE_RADIO_RESET, derive_key
from empiremuse.api.settings import ExternalPanelConfigError, external_panel_requested, load_external_panel_settings
from empiremuse.config_loader import resolve_port
from empiremuse.panel.authz import GuildAuthorizer
from empiremuse.panel.oauth import make_client_provider
from empiremuse.panel.services.jukebox import JukeboxService
from empiremuse.panel.services.radio import RadioPage
from empiremuse.panel.services.radio_setup import RadioSetupService
from empiremuse.panel.services.radio_stations import StationsPage
from empiremuse.panel.settings import PanelConfigError, load_session_limits
from empiremuse.panel.state import ensure_panel_state
from empiremuse.version import image_version

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExternalApiStatus:
    """``blocked``: o modo externo foi pedido e falhou. Nem a API nem o painel genérico sobem, sem cair no genérico; o bot toca."""

    blocked: bool = False
    server: Optional[ApiHTTPServer] = None


def _bot_version() -> str:
    return str(image_version() or "dev")


def warn_if_relay_is_off(config: Mapping[str, Any]) -> None:
    """``RUN_RPC_SERVER`` desligado: o servidor web não sobe e, com ele, nem a API externa. Avisa alto em vez de calar."""
    if external_panel_requested(config):
        logger.error(
            "EXTERNAL_PANEL_* está preenchida, mas RUN_RPC_SERVER=false: o servidor web não sobe e a API do painel externo "
            "não abre. Ligue RUN_RPC_SERVER=true ou limpe as variáveis EXTERNAL_PANEL_*."
        )


def start_external_api(
    pool: Any,
    config: Optional[Mapping[str, Any]],
    *,
    clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], float] = time.time,
) -> ExternalApiStatus:
    config = config or {}
    if not external_panel_requested(config):
        return ExternalApiStatus()
    try:
        settings = load_external_panel_settings(config, web_port=resolve_port(config.get("PORT")), now=wall_clock())
        limits = load_session_limits(config)
    except (ExternalPanelConfigError, PanelConfigError, ValueError) as error:
        logger.error("Painel externo desativado por configuração inválida; o bot segue sem painel e sem API: %s", error)
        return ExternalApiStatus(blocked=True)
    assert settings is not None  # ``external_panel_requested`` já foi conferido
    state = ensure_panel_state(pool, limits, clock=clock)
    ctx = ApiContext(
        settings=settings,
        keys=ServiceKeys(settings.service_key, settings.previous_key, settings.previous_expires_at),
        store=state.session_store,
        limits=ApiLimits(clock),
        oauth_provider=make_client_provider(pool, settings),
        pool=pool,
        clock=clock,
        wall_clock=wall_clock,
        bot_version=_bot_version,
        authorizer=GuildAuthorizer(pool, clock=clock),
        settings_service=state.settings_service,
        account_service=state.account_service,
        idempotency=IdempotencyStore(clock),
        radio_page=RadioPage(),  # sem trava própria (a das rádios vive em ``pool.radio``): só o cache de nomes
        stations_page=StationsPage(),
        # os serviços presos a uma chave de assinatura não ficam no estado do pool: cada consumidor os cria com a subchave dele
        jukebox_service=JukeboxService(
            state.task_runner,
            state.settings_service,
            derive_key(settings.signing_secret, PURPOSE_JUKEBOX),
            clock,
        ),
        radio_setup_service=RadioSetupService(derive_key(settings.signing_secret, PURPOSE_RADIO_RESET), clock),
    )
    try:
        server = listen(ctx, settings)
    except OSError as error:
        logger.error(
            "API do painel externo não abriu %s:%d (%s); o bot segue sem painel e sem API.",
            settings.bind,
            settings.port,
            type(error).__name__,
        )
        return ExternalApiStatus(blocked=True)
    # o apagamento de dados limpa também as respostas guardadas; só com o listener aberto
    state.session_store.add_user_listener(ctx.idempotency.purge_user)
    pool.panel_api_server = server
    logger.info("API do painel externo ativa em %s:%d (/api/v1).", settings.bind, settings.port)
    return ExternalApiStatus(server=server)
