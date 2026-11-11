"""Estado compartilhado do painel: um único ``SessionStore``, ``TaskRunner`` e serviço de formulários por pool (contrato T-456 §6).

Fica fora de ``build_panel_app`` porque a API externa, o painel genérico e o apagamento de dados (``destroy_user``) usam os
mesmos objetos, inclusive com o painel genérico desligado. Os serviços de formulário também são únicos: o lock por escopo
(servidor, bot ou usuário) vale entre o painel genérico e a API, que gravam os mesmos documentos.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from empiremuse.panel.services.account import AccountService
from empiremuse.panel.services.service import SettingsService
from empiremuse.panel.services.tasks import TaskRunner
from empiremuse.panel.sessions import SessionStore


@dataclass(frozen=True)
class PanelState:
    session_store: SessionStore
    task_runner: TaskRunner
    settings_service: SettingsService
    account_service: AccountService


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
    return PanelState(store, runner, forms, accounts)
