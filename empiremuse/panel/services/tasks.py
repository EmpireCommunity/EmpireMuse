"""Tarefas assíncronas do painel (configurar/reiniciar a Jukebox), em memória, com acompanhamento por polling.

Uma tarefa por servidor por vez; dono = usuário do painel que a criou. Tarefas terminadas expiram depois de 15 minutos.
As ``asyncio.Task`` ficam em um conjunto rastreado (sem ``create_task`` solto) e ``shutdown()`` cancela todas.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

TASK_TIMEOUT_SECONDS = 60
MAX_TASKS_PER_USER = 5
MAX_TASKS_TOTAL = 500
TASK_TTL_SECONDS = 15 * 60
TASK_ID_BYTES = 24  # token_urlsafe(24) = 32 caracteres

STATE_PENDING = "pendente"
STATE_RUNNING = "executando"
STATE_OK = "ok"
STATE_ERROR = "erro"
FINAL_STATES = (STATE_OK, STATE_ERROR)

MSG_GENERIC = "Não foi possível concluir. Tente novamente."
MSG_TIMEOUT = "A operação demorou mais que o esperado e foi interrompida."
MSG_SHUTDOWN = "A operação foi interrompida porque o bot está sendo reiniciado."

Job = Callable[[], Awaitable[str]]


class TaskFailure(Exception):
    """Falha esperada de uma tarefa; ``message`` é texto pt-BR seguro para mostrar ao usuário."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class TaskBusy(Exception):
    """Já há uma tarefa em andamento neste servidor."""


class TaskLimit(Exception):
    """Limite de tarefas por usuário ou total atingido (todas ainda em andamento)."""


@dataclass
class Task:
    id: str
    owner: str
    guild_id: int
    bot_id: str
    kind: str
    state: str
    message: str
    created_at: float
    finished_at: Optional[float] = None

    @property
    def final(self) -> bool:
        return self.state in FINAL_STATES


def valid_task_id(value: str) -> bool:
    """Formato do id (32 caracteres urlsafe ASCII); ids malformados nem chegam a ser procurados."""
    return len(value) == 32 and value.isascii() and all(char.isalnum() or char in "-_" for char in value)


class TaskRunner:
    """Registro e execução das tarefas; relógio, timeout e limites são injetáveis (padrões = constantes)."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = TASK_TIMEOUT_SECONDS,
        max_per_user: int = MAX_TASKS_PER_USER,
        max_total: int = MAX_TASKS_TOTAL,
        ttl: float = TASK_TTL_SECONDS,
    ) -> None:
        self._clock, self._timeout, self._ttl = clock, timeout, ttl
        self._max_per_user, self._max_total = max_per_user, max_total
        self._tasks: dict[str, Task] = {}
        self._running: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ consulta

    def get(self, task_id: str) -> Optional[Task]:
        self._sweep()
        return self._tasks.get(task_id)

    def active_for_guild(self, guild_id: int) -> Optional[Task]:
        self._sweep()
        return next((t for t in self._tasks.values() if t.guild_id == guild_id and not t.final), None)

    # ------------------------------------------------------------------ criação e execução

    def submit(self, owner: str, guild_id: int, bot_id: str, kind: str, job: Job) -> Task:
        """Registra e agenda a tarefa; levanta ``TaskBusy`` (servidor ocupado) ou ``TaskLimit``."""
        if self.active_for_guild(guild_id) is not None:
            raise TaskBusy
        self._evict_oldest_finished(lambda t: t.owner == owner, self._max_per_user)
        self._evict_oldest_finished(lambda t: True, self._max_total)
        task = Task(
            id=secrets.token_urlsafe(TASK_ID_BYTES),
            owner=owner,
            guild_id=guild_id,
            bot_id=bot_id,
            kind=kind,
            state=STATE_PENDING,
            message="",
            created_at=self._clock(),
        )
        self._tasks[task.id] = task
        runner = asyncio.get_running_loop().create_task(self._run(task, job))
        self._running.add(runner)
        runner.add_done_callback(self._running.discard)
        return task

    async def _run(self, task: Task, job: Job) -> None:
        task.state = STATE_RUNNING
        try:
            async with asyncio.timeout(self._timeout):
                task.message = await job()
            self._finish(task, STATE_OK)
        except TaskFailure as failure:
            task.message = failure.message
            self._finish(task, STATE_ERROR)
        except TimeoutError:
            task.message = MSG_TIMEOUT
            self._finish(task, STATE_ERROR)
        except asyncio.CancelledError:
            task.message = MSG_SHUTDOWN
            self._finish(task, STATE_ERROR)
            raise
        except Exception as exc:  # só o tipo no log: a mensagem pode trazer dados do Discord ou do usuário
            logger.warning("Falha inesperada na tarefa %s (%s)", task.kind, type(exc).__name__)
            task.message = MSG_GENERIC
            self._finish(task, STATE_ERROR)

    def _finish(self, task: Task, state: str) -> None:
        task.state = state
        task.finished_at = self._clock()

    async def shutdown(self) -> None:
        """Cancela as tarefas em andamento e espera o cancelamento terminar."""
        running = list(self._running)
        for runner in running:
            runner.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)

    # ------------------------------------------------------------------ limites e expiração

    def _sweep(self) -> None:
        now = self._clock()
        expired = [t.id for t in self._tasks.values() if t.finished_at is not None and now - t.finished_at >= self._ttl]
        for task_id in expired:
            del self._tasks[task_id]

    def _evict_oldest_finished(self, scope: Callable[[Task], bool], limit: int) -> None:
        """Garante vaga para mais uma tarefa no escopo, removendo as terminadas mais antigas; senão ``TaskLimit``."""
        self._sweep()
        while sum(1 for t in self._tasks.values() if scope(t)) >= limit:
            finished = [t for t in self._tasks.values() if scope(t) and t.final]
            if not finished:
                raise TaskLimit
            del self._tasks[min(finished, key=lambda t: t.finished_at or 0).id]
