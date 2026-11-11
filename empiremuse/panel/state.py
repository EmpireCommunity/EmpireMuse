"""Estado compartilhado do painel: um único ``SessionStore``, ``TaskRunner`` e serviço de formulários por pool (contrato T-456 §6).

Fica fora de ``build_panel_app`` porque a API externa, o painel genérico e o apagamento de dados (``destroy_user``) usam os
mesmos objetos, inclusive com o painel genérico desligado. Os serviços de formulário também são únicos: o lock por escopo
(servidor, bot ou usuário) vale entre o painel genérico e a API, que gravam os mesmos documentos. As páginas de rádio e de
estações (sem trava própria; a trava das rádios vive em ``pool.radio``) também são únicas, para dividir o cache de nomes.
Ficam **fora** daqui os serviços presos a uma chave de assinatura (Jukebox, reset de rádio, apagar): cada consumidor os
constrói com a chave dele (``cookie_secret`` no genérico, subchave derivada na API), por cima dos serviços únicos.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from empiremuse.panel.services.account import AccountService
from empiremuse.panel.services.radio import RadioPage
from empiremuse.panel.services.radio_stations import StationsPage
from empiremuse.panel.services.service import SettingsService
from empiremuse.panel.services.tasks import TaskRunner
from empiremuse.panel.sessions import SessionStore


@dataclass(frozen=True)
class PanelState:
    session_store: SessionStore
    task_runner: TaskRunner
    settings_service: SettingsService
    account_service: AccountService
    radio_page: RadioPage  # as rádios do servidor e a busca de estações (com o cache de nomes do diretório)
    stations_page: StationsPage


def ensure_panel_state(pool: Any, limits: Any, *, clock: Callable[[], float] = time.monotonic) -> PanelState:
    """O estado do pool; cria (e registra em ``pool.panel_sessions`` e ``pool.panel_tasks``) o que ainda não existe.

    ``limits`` é qualquer objeto com ``session_ttl_hours`` e ``session_idle_minutes`` (``PanelSettings``).
    """
    store = getattr(pool, "panel_sessions", None)
    if not isinstance(store, SessionStore):
        store = pool.panel_sessions = SessionStore(limits, clock=clock)
    runner = getattr(pool, "panel_tasks", None)
    if not isinstance(runner, TaskRunner):
        runner = pool.panel_tasks = TaskRunner(clock=clock)
    forms = getattr(pool, "panel_forms", None)
    if not isinstance(forms, SettingsService):
        forms = pool.panel_forms = SettingsService(getattr(pool, "user_locks", None))
    accounts = getattr(pool, "panel_accounts", None)
    if not isinstance(accounts, AccountService):
        accounts = pool.panel_accounts = AccountService(forms)
    radios = getattr(pool, "panel_radios", None)
    if not isinstance(radios, RadioPage):
        radios = pool.panel_radios = RadioPage()
    stations = getattr(pool, "panel_stations", None)
    if not isinstance(stations, StationsPage):
        stations = pool.panel_stations = StationsPage()
    return PanelState(store, runner, forms, accounts, radios, stations)
