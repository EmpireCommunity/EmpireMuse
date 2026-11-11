"""O que os handlers da API compartilham: configuração, chaves, sessões, limites e relógios (todos injetáveis)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Optional

from empiremuse.api.idempotency import IdempotencyStore
from empiremuse.api.keys import ServiceKeys
from empiremuse.api.limits import ApiLimits
from empiremuse.api.settings import ExternalPanelSettings
from empiremuse.panel.authz import GuildAuthorizer
from empiremuse.panel.oauth import DiscordOAuthClient
from empiremuse.panel.services.account import AccountService
from empiremuse.panel.services.service import SettingsService
from empiremuse.panel.sessions import SessionStore


@dataclass(frozen=True)
class ApiContext:
    settings: ExternalPanelSettings
    keys: ServiceKeys
    store: SessionStore
    limits: ApiLimits
    oauth_provider: Callable[[], Optional[DiscordOAuthClient]]
    pool: Any
    clock: Callable[[], float]  # monótono, o mesmo do store
    wall_clock: Callable[[], float]  # epoch, para datas e para a chave anterior
    bot_version: Callable[[], str]
    authorizer: GuildAuthorizer  # a decisão por servidor, reconferida a cada escrita (``fresh=True``)
    settings_service: SettingsService  # os mesmos serviços do painel genérico (e os mesmos locks)
    account_service: AccountService
    idempotency: IdempotencyStore

    def iso_at(self, monotonic_instant: float) -> str:
        """Um instante do relógio do store como data ISO 8601 em UTC (``2026-11-11T12:00:00Z``)."""
        epoch = self.wall_clock() - (self.clock() - monotonic_instant)
        return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
