"""Premium de rádio por servidor (item 8, R1, R-1 e R-2): quem concedeu e quantas rádios estão ligadas agora.

O documento existe = o servidor é premium. ``active`` é o contador das rádios LIGADAS (pausadas incluídas) e só muda por transição
confirmada com compare-and-set (``rev``): reservar ao ligar, liberar ao desligar. O teto é ``rules.radio.MAX_PER_GUILD``. Quem escreve
é só quem passou pela conferência de ``OWNER_IDS`` (comando de dono); esta classe não decide quem pode: guarda e conta.
A leitura tem cache de ``CACHE_SECONDS`` (cada retomada e cada reconexão confere o premium de novo, sem ir ao banco toda vez).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from empiremuse.rules.radio import MAX_PER_GUILD, PREMIUM_MAX, clamp_active, has_room

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool

logger = logging.getLogger(__name__)

PREMIUM_DB = "premium"  # nome da coleção
GLOBAL_DB = "global"  # banco (o argumento ``collection`` dos métodos do banco, como no ``ServerStore``)
CACHE_SECONDS = 30.0
CAS_ATTEMPTS = 6


class RadioUnavailable(Exception):
    """O banco não respondeu: quem chama deixa a decisão para depois (a página web responde 503)."""


@dataclass(frozen=True)
class Premium:
    guild_id: int
    active: int
    rev: int = 0
    granted_by: str = ""

    @property
    def free_slots(self) -> int:
        return max(0, PREMIUM_MAX - self.active)


def _parse(doc: Optional[dict[str, Any]]) -> Optional[Premium]:
    if not doc:
        return None
    try:
        guild_id = int(str(doc["_id"]))
    except KeyError, ValueError:
        return None
    rev = doc.get("rev", 0)
    return Premium(
        guild_id=guild_id,
        active=clamp_active(doc.get("active")),
        rev=rev if isinstance(rev, int) and not isinstance(rev, bool) and rev >= 0 else 0,
        granted_by=str(doc.get("granted_by", ""))[:40],
    )


class PremiumStore:
    def __init__(self, pool: BotPool, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.pool = pool
        self._clock = clock
        self._cache: dict[int, tuple[float, Optional[Premium]]] = {}
        self._generation: dict[
            int, int
        ] = {}  # sobe a cada ``_forget``: uma leitura que começou antes não repõe o cache velho

    # ------------------------------------------------------------------ banco

    @property
    def _database(self) -> Any:
        return self.pool.database

    async def _find(self, guild_id: int) -> Optional[Premium]:
        try:
            return _parse(await self._database.find_doc(guild_id, db_name=PREMIUM_DB, collection=GLOBAL_DB))
        except Exception as error:  # noqa: BLE001 - qualquer falha do banco vira "indisponível"; só o tipo vai ao log
            logger.warning("Premium de rádio não lido (%s)", type(error).__name__)
            raise RadioUnavailable from error

    async def _cas(self, guild_id: int, doc: dict[str, Any], expected_rev: Optional[int]) -> bool:
        try:
            return bool(
                await self._database.save_doc_cas(
                    guild_id, doc, expected_rev=expected_rev, db_name=PREMIUM_DB, collection=GLOBAL_DB
                )
            )
        except Exception as error:  # noqa: BLE001
            logger.warning("Premium de rádio não gravado (%s)", type(error).__name__)
            raise RadioUnavailable from error

    def _forget(self, guild_id: int) -> None:
        self._generation[guild_id] = self._generation.get(guild_id, 0) + 1
        self._cache.pop(guild_id, None)

    # ------------------------------------------------------------------ leitura (cache de 30 s)

    async def get(self, guild_id: int) -> Optional[Premium]:
        """O premium do servidor (``None`` = não é premium), com cache de 30 s. Banco fora do ar levanta ``RadioUnavailable``."""
        now = self._clock()
        cached = self._cache.get(guild_id)
        if cached is not None and now - cached[0] < CACHE_SECONDS:
            return cached[1]
        generation = self._generation.get(guild_id, 0)
        premium = await self._find(guild_id)
        if (
            self._generation.get(guild_id, 0) == generation
        ):  # uma revogação/reserva durante a leitura invalida o resultado
            self._cache[guild_id] = (now, premium)
        return premium

    async def fresh(self, guild_id: int) -> Optional[Premium]:
        """Sem cache: para a transição de ligar/desligar e para a revogação."""
        self._forget(guild_id)
        return await self.get(guild_id)

    async def all(self) -> list[Premium]:
        """Todos os servidores premium (para o recálculo da subida e para o comando de dono listar)."""
        try:
            docs = [doc async for doc in self._database.iter_data(PREMIUM_DB, GLOBAL_DB)]
        except Exception as error:  # noqa: BLE001
            logger.warning("Premiums de rádio não listados (%s)", type(error).__name__)
            raise RadioUnavailable from error
        return [premium for doc in docs if (premium := _parse(doc)) is not None]

    # ------------------------------------------------------------------ concessão (R-1: só o comando de dono chama)

    async def grant(self, guild_id: int, *, by: int | str) -> Premium:
        """Concede o premium (idempotente: quem já tem fica como está, com o contador intacto)."""
        for _ in range(CAS_ATTEMPTS):
            current = await self._find(guild_id)
            if current is not None:
                self._forget(guild_id)
                return current
            doc = {"active": 0, "granted_by": str(by)[:40], "granted_at": time.time(), "rev": 0}
            if await self._cas(guild_id, doc, None):
                self._forget(guild_id)
                return Premium(guild_id, 0, 0, str(by)[:40])
        raise RadioUnavailable

    async def revoke(self, guild_id: int) -> bool:
        """Apaga o documento (o passo 1 da revogação; desligar as rádios é do ``RadioStore``). ``True`` se existia."""
        existed = await self._find(guild_id) is not None
        try:
            await self._database.delete_data(guild_id, db_name=PREMIUM_DB, collection=GLOBAL_DB)
        except Exception as error:  # noqa: BLE001
            logger.warning("Premium de rádio não apagado (%s)", type(error).__name__)
            raise RadioUnavailable from error
        self._forget(guild_id)
        return existed

    # ------------------------------------------------------------------ o contador (R-2)

    async def reserve(self, guild_id: int) -> bool:
        """Reserva uma vaga (``active`` +1) se o servidor é premium e ainda cabe. ``False`` = sem premium ou sem vaga."""
        for _ in range(CAS_ATTEMPTS):
            current = await self._find(guild_id)
            if current is None or not has_room(current.active):
                self._forget(guild_id)
                return False
            doc = {"active": current.active + 1, "rev": current.rev + 1}
            if await self._cas(guild_id, doc, current.rev):
                self._forget(guild_id)
                return True
        raise RadioUnavailable

    async def release(self, guild_id: int) -> None:
        """Devolve uma vaga (``active`` -1, nunca abaixo de zero). Sem documento (premium revogado) não há o que devolver."""
        for _ in range(CAS_ATTEMPTS):
            current = await self._find(guild_id)
            if current is None or current.active <= 0:
                self._forget(guild_id)
                return
            doc = {"active": current.active - 1, "rev": current.rev + 1}
            if await self._cas(guild_id, doc, current.rev):
                self._forget(guild_id)
                return
        raise RadioUnavailable

    async def set_active(self, guild_id: int, value: int) -> None:
        """Grava o contador recalculado (só a subida usa, a partir das rádios realmente ligadas)."""
        for _ in range(CAS_ATTEMPTS):
            current = await self._find(guild_id)
            if current is None or current.active == value:
                self._forget(guild_id)
                return
            if await self._cas(guild_id, {"active": min(value, MAX_PER_GUILD), "rev": current.rev + 1}, current.rev):
                self._forget(guild_id)
                return
        raise RadioUnavailable
