"""A saúde das estações ao vivo (item 8, R3, R-10): quem marca uma estação como indisponível e quem a reabilita.

- **Marca:** 3 ciclos de tentativas esgotados em 1 h (``record_exhausted``) tornam a estação indisponível por 30 min, no documento
  ``global.stations_health`` (``_id`` = uuid, ``{fails, until}``). Vencido o ``until``, a estação volta a valer; no autocomplete ela aparece com ⛔ enquanto vale.
- **Health check:** uma tarefa por processo, a cada 10 min, 2 estações em paralelo e até 20 por ciclo, deduplicadas por uuid. Só olha
  estação INDISPONÍVEL que uma rádio LIGADA deste processo usa (principal ou reserva): 50 estações quebradas sem rádio = 0 requisições. A URL vem de
  ``/json/stations/byuuid`` (nunca de ``/json/url``: zero cliques) e os primeiros 16 KiB são lidos pelo mesmo caminho do relay (R-8).
- Banco fora: marcar e reabilitar falham calados (só o tipo no log, R-12); a rádio nunca depende disto para tocar.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any

from empiremuse.utils.radio.premium import CAS_ATTEMPTS, GLOBAL_DB

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool
    from empiremuse.utils.radio import RadioHub

logger = logging.getLogger(__name__)

HEALTH_DB = "stations_health"
CYCLES_TO_MARK = 3
WINDOW_S = 3600.0
UNAVAILABLE_TTL_S = 1800.0
CHECK_EVERY_S = 600.0
PARALLEL = 2
PER_CYCLE = 20

Probe = Callable[[Any], Awaitable[bool]]


class StationHealth:
    def __init__(self, pool: BotPool, *, clock: Callable[[], float] = time.time) -> None:
        self.pool = pool
        self._clock = clock
        self._until: dict[
            str, float
        ] = {}  # uuid -> até quando vale a indisponibilidade (cópia local para o autocomplete)

    @property
    def _database(self) -> Any:
        return self.pool.database

    # ------------------------------------------------------------------ leitura

    def unavailable(self, uuid: object) -> bool:
        """Síncrono, sobre a cópia local (``refresh`` e ``record_exhausted`` a mantêm): serve ao autocomplete e à escolha da reserva."""
        until = self._until.get(uuid) if isinstance(uuid, str) else None
        if until is None:
            return False
        if until <= self._clock():
            self._until.pop(uuid, None)  # type: ignore[arg-type]
            return False
        return True

    def unavailable_uuids(self) -> list[str]:
        return sorted(u for u in list(self._until) if self.unavailable(u))

    async def refresh(self) -> None:
        """Relê as marcas do banco (outro processo pode ter marcado). Banco fora: mantém a cópia local."""
        try:
            docs = [doc async for doc in self._database.iter_data(HEALTH_DB, GLOBAL_DB, None)]
        except Exception as error:  # noqa: BLE001
            logger.warning("Saúde das estações não lida (%s)", type(error).__name__)
            return
        now = self._clock()
        fresh: dict[str, float] = {}
        for doc in docs:
            until, key = doc.get("until"), doc.get("_id")
            if isinstance(key, str) and isinstance(until, (int, float)) and until > now:
                fresh[key] = float(until)
            elif isinstance(key, str) and not [
                t for t in doc.get("fails", []) if isinstance(t, (int, float)) and now - t < WINDOW_S
            ]:
                await self.clear(key)  # vencida e sem falhas recentes: o documento não cresce para sempre
        self._until = fresh

    # ------------------------------------------------------------------ escrita

    async def record_exhausted(self, uuid: object) -> bool:
        """Um ciclo de tentativas esgotado para ``uuid``. ``True`` se com este a estação passou a indisponível (3 em 1 h)."""
        if not isinstance(uuid, str):
            return False
        try:
            for _ in range(CAS_ATTEMPTS):
                now = self._clock()
                doc = await self._database.find_doc(uuid, db_name=HEALTH_DB, collection=GLOBAL_DB)
                fails = [
                    float(t) for t in (doc or {}).get("fails", []) if isinstance(t, (int, float)) and now - t < WINDOW_S
                ]
                fails.append(now)
                marked = len(fails) >= CYCLES_TO_MARK
                until = now + UNAVAILABLE_TTL_S if marked else float((doc or {}).get("until") or 0)
                rev = (doc or {}).get("rev")
                updated = {"_id": uuid, "fails": [] if marked else fails, "until": until, "rev": (rev or 0) + 1}
                saved = await self._database.save_doc_cas(
                    uuid, updated, expected_rev=rev, db_name=HEALTH_DB, collection=GLOBAL_DB
                )
                if saved:
                    if marked:
                        self._until[uuid] = until
                    return marked
        except Exception as error:  # noqa: BLE001 - só o tipo vai ao log (R-12)
            logger.warning("Saúde da estação não gravada (%s)", type(error).__name__)
        return False

    async def clear(self, uuid: str) -> None:
        self._until.pop(uuid, None)
        try:
            await self._database.delete_data(uuid, db_name=HEALTH_DB, collection=GLOBAL_DB)
        except Exception as error:  # noqa: BLE001
            logger.warning("Saúde da estação não limpa (%s)", type(error).__name__)


class HealthChecker:
    """O ciclo de reabilitação. ``probe(station)`` devolve se a estação já toca (o relay lê 16 KiB); injetável para testar."""

    def __init__(
        self,
        hub: RadioHub,
        probe: Probe,
        *,
        referenced: Callable[[], Iterable[str]],
        recovered: Callable[[str], Awaitable[None]],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.hub = hub
        self._probe = probe
        self._referenced = referenced
        self._recovered = recovered
        self._sleep = sleep
        self._task: asyncio.Task | None = None
        self.requests = 0

    async def run_once(self) -> int:
        """Um ciclo. Devolve quantas estações foram reabilitadas."""
        health = self.hub.health
        await health.refresh()
        wanted = set(self._referenced())
        batch = [u for u in health.unavailable_uuids() if u in wanted][:PER_CYCLE]  # deduplicado: é conjunto
        if not batch:
            return 0
        gate = asyncio.Semaphore(PARALLEL)
        results = await asyncio.gather(*(self._check(uuid, gate) for uuid in batch))
        return sum(results)

    async def _check(self, uuid: str, gate: asyncio.Semaphore) -> bool:
        async with gate:
            try:
                self.requests += 1
                station = await self.hub.directory.by_uuid(uuid, reserve=0.0)  # byuuid: não registra clique
                if station is None or not await self._probe(station):
                    return False
                await self.hub.health.clear(uuid)
                await self._recovered(uuid)
                return True
            except Exception as error:  # noqa: BLE001 - uma estação ruim não derruba o ciclo; só o tipo no log
                logger.warning("Estação não reconferida (%s)", type(error).__name__)
                return False

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await self._sleep(CHECK_EVERY_S)
            try:
                await self.run_once()
            except Exception as error:  # noqa: BLE001
                logger.warning("Ciclo de saúde das estações falhou (%s)", type(error).__name__)

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
