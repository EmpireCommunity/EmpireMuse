"""Estado compartilhado do painel: um único ``SessionStore`` e um único ``TaskRunner`` por pool (contrato T-456 §6).

Fica fora de ``build_panel_app`` porque a API externa, o painel genérico e o apagamento de dados (``destroy_user``) usam os
mesmos objetos, inclusive com o painel genérico desligado.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from empiremuse.panel.services.tasks import TaskRunner
from empiremuse.panel.sessions import SessionStore


@dataclass(frozen=True)
class PanelState:
    session_store: SessionStore
    task_runner: TaskRunner


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
    return PanelState(store, runner)
