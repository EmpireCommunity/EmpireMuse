"""Controles do dono da rádio (item 8, R1, R-1 e R-3): "pausar todas" e os bots só-rádio.

- ``radio_control`` (documento único ``control``): ``paused_all`` é a válvula de segurança, porque não há teto global. Sobrevive a reinício.
  Enquanto o valor não foi lido do banco com sucesso, vale como pausado (falha fechada: sem saber, nenhuma rádio retoma por conta própria).
- ``bot_roles`` (documento único ``roles``): ``radio_only`` = identificadores dos bots reservas dedicados a rádio. O principal nunca entra.

Quem pode escrever é decidido por quem chama (comando de dono, só ``OWNER_IDS``); aqui só se guarda, com compare-and-set em ``rev``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Optional

from empiremuse.rules.servers import is_main_identifier
from empiremuse.utils.radio.premium import CAS_ATTEMPTS, GLOBAL_DB, RadioUnavailable

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool

logger = logging.getLogger(__name__)

CONTROL_DB = "radio_control"
CONTROL_ID = "control"
ROLES_DB = "bot_roles"
ROLES_ID = "roles"
MAX_ROLE_ENTRIES = 32


class MainBotRefused(Exception):
    """O bot principal não pode ser só-rádio."""


class _Document:
    """Um documento único com ``rev``: leitura e escrita por compare-and-set, sempre convertendo falha do banco em ``RadioUnavailable``."""

    db_name = ""
    doc_id = ""

    def __init__(self, pool: BotPool) -> None:
        self.pool = pool

    async def _read(self) -> Optional[dict[str, Any]]:
        try:
            return await self.pool.database.find_doc(self.doc_id, db_name=self.db_name, collection=GLOBAL_DB)
        except Exception as error:  # noqa: BLE001 - só o tipo vai ao log
            logger.warning("Controle de rádio não lido (%s)", type(error).__name__)
            raise RadioUnavailable from error

    async def _write(self, body: dict[str, Any], expected_rev: Optional[int]) -> bool:
        try:
            return bool(
                await self.pool.database.save_doc_cas(
                    self.doc_id, body, expected_rev=expected_rev, db_name=self.db_name, collection=GLOBAL_DB
                )
            )
        except Exception as error:  # noqa: BLE001
            logger.warning("Controle de rádio não gravado (%s)", type(error).__name__)
            raise RadioUnavailable from error

    async def _mutate(self, change: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        for _ in range(CAS_ATTEMPTS):
            current = await self._read()
            base = dict(current) if current else {}
            body = {**change(base)}
            rev = base.get("rev", 0) if isinstance(base.get("rev", 0), int) else 0
            body["rev"] = rev + 1
            if await self._write(body, None if current is None else rev):
                return body
        raise RadioUnavailable


class RadioControl(_Document):
    db_name = CONTROL_DB
    doc_id = CONTROL_ID

    def __init__(self, pool: BotPool) -> None:
        super().__init__(pool)
        self._loaded = False
        self._paused = True
        self._listeners: list[Callable[[bool], None]] = []

    @property
    def paused_all(self) -> bool:
        """Todas as rádios do processo estão pausadas pelo dono? (Antes da primeira leitura com sucesso: sim.)"""
        return self._paused

    def on_change(self, listener: Callable[[bool], None]) -> None:
        self._listeners.append(listener)

    async def load(self) -> bool:
        """Lê o valor do banco (na subida e quando alguém quiser conferir). Sem documento = não pausado."""
        doc = await self._read()
        self._paused = bool(doc.get("paused_all")) if doc else False
        self._loaded = True
        return self._paused

    async def set_paused_all(self, value: bool, *, by: int | str) -> bool:
        """Grava e aplica ``paused_all``. Devolve ``True`` se o valor MUDOU (repetir o mesmo comando não faz nada)."""
        before = (await self._read() or {}).get("paused_all")
        await self._mutate(lambda body: {**body, "paused_all": bool(value), "by": str(by)[:40], "at": time.time()})
        changed = bool(before) != bool(value)
        self._paused, self._loaded = bool(value), True
        if changed:
            for listener in list(self._listeners):
                try:
                    listener(self._paused)
                except Exception as error:  # noqa: BLE001 - um ouvinte com defeito não impede os outros
                    logger.warning("Ouvinte de pausa geral falhou (%s)", type(error).__name__)
        return changed


class BotRoles(_Document):
    db_name = ROLES_DB
    doc_id = ROLES_ID

    def __init__(self, pool: BotPool) -> None:
        super().__init__(pool)
        self._radio_only: frozenset[str] = frozenset()

    def is_radio_only(self, identifier: object) -> bool:
        """O bot de ``identifier`` é só-rádio? (Leitura em memória; ``load`` atualiza.) O principal nunca é."""
        return isinstance(identifier, str) and not is_main_identifier(identifier) and identifier in self._radio_only

    @property
    def radio_only(self) -> tuple[str, ...]:
        return tuple(sorted(self._radio_only))

    async def load(self) -> tuple[str, ...]:
        doc = await self._read()
        listed = doc.get("radio_only", []) if doc else []
        self._radio_only = frozenset(
            item for item in listed if isinstance(item, str) and item and not is_main_identifier(item)
        )
        return self.radio_only

    async def add(self, identifier: str) -> tuple[str, ...]:
        if is_main_identifier(identifier):
            raise MainBotRefused
        await self._mutate(
            lambda body: {
                **body,
                "radio_only": sorted({*body.get("radio_only", []), identifier[:40]})[:MAX_ROLE_ENTRIES],
            }
        )
        return await self.load()

    async def remove(self, identifier: str) -> tuple[str, ...]:
        await self._mutate(
            lambda body: {**body, "radio_only": [i for i in body.get("radio_only", []) if i != identifier]}
        )
        return await self.load()
