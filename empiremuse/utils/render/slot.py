"""A vaga única de render do processo (``RENDER_SLOTS = 1``), dividida pelos players dos 5 bots e pelo visor da rádio 24 h.

Só o DESENHO entra na vaga: busca e decodificação de capa ficam fora (têm os próprios limites). O desenho roda numa thread
(``asyncio.to_thread``), e a vaga só é devolvida quando a thread termina, mesmo que quem pediu seja cancelado: ``to_thread``
não se cancela, e devolver antes deixaria dois desenhos ao mesmo tempo.
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from empiremuse.utils.render.budget import RENDER_SLOTS, SLOT_WAIT_S, SLOW_RENDER_S

logger = logging.getLogger(__name__)

T = TypeVar("T")
ToThread = Callable[..., Awaitable[Any]]


class RenderBusy(Exception):
    """A vaga não abriu dentro do prazo: quem pediu mantém a imagem anterior."""


class RenderSlot:
    """``asyncio.Semaphore`` + contador de espera. ``pressure()`` = quantos pedidos esperam a vaga agora."""

    def __init__(
        self,
        slots: int = RENDER_SLOTS,
        *,
        wait_s: float = SLOT_WAIT_S,
        slow_s: float = SLOW_RENDER_S,
        clock: Callable[[], float] = time.monotonic,
        to_thread: ToThread = asyncio.to_thread,
    ) -> None:
        self._semaphore = asyncio.Semaphore(slots)
        self._waiting = 0
        self._wait_s = wait_s
        self._slow_s = slow_s
        self._clock = clock
        self._to_thread = to_thread

    def pressure(self) -> int:
        return self._waiting

    async def _acquire(self) -> None:
        if not self._semaphore.locked():  # vaga livre e ninguém na fila: entra sem esperar (nem prazo)
            await self._semaphore.acquire()
            return
        self._waiting += 1
        try:
            await asyncio.wait_for(self._semaphore.acquire(), self._wait_s)
        except TimeoutError:
            raise RenderBusy from None
        finally:
            self._waiting -= 1

    async def run(self, fn: Callable[..., T], *args: Any, label: str = "render") -> T:
        """``fn(*args)`` numa thread, dentro da vaga. ``RenderBusy`` se a espera passar do prazo. ``label`` (o id da skin ou
        ``"visor"``) só aparece no log de desenho lento, nunca o conteúdo."""
        await self._acquire()
        started = self._clock()
        work = asyncio.ensure_future(self._to_thread(fn, *args))
        try:
            result = await asyncio.shield(work)
        except asyncio.CancelledError:
            if work.done():
                self._finish(work, started, label)
            else:
                work.add_done_callback(lambda done: self._finish(done, started, label))
            raise
        except BaseException:
            self._finish(work, started, label)
            raise
        self._finish(work, started, label)
        return result

    def _finish(self, work: asyncio.Future[Any], started: float, label: str) -> None:
        """Devolve a vaga quando o desenho acabou de verdade; o erro de um desenho órfão é lido (não vira aviso do loop)."""
        self._semaphore.release()
        if not work.cancelled():
            work.exception()
        elapsed = self._clock() - started
        if elapsed > self._slow_s:
            logger.warning("Render lento (%s, %.2f s)", label, elapsed)


_SLOTS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, RenderSlot]" = weakref.WeakKeyDictionary()


def shared_slot() -> RenderSlot:
    """A vaga do processo, uma por laço de eventos (em produção, um só). Chamar dentro do laço."""
    loop = asyncio.get_running_loop()
    slot = _SLOTS.get(loop)
    if slot is None:
        slot = _SLOTS[loop] = RenderSlot()
    return slot
