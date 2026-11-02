"""As estações personalizadas favoritas de CADA MEMBRO (decisão do dono): a estrela do painel da personalizada liga/desliga a estação no ar nas favoritas de
quem clicou; o filtro "Favoritas" mostra as de quem o escolheu.

Um documento por (servidor, membro) em ``global.radio_custom_favorites`` (``_id`` = ``<servidor>:<membro>``), com compare-and-set em ``rev``. Só guarda ids de
estações do servidor (24 hex); estação apagada some da lista na leitura. No máximo 24 por membro. As favoritas da rádio AO VIVO são outras (por servidor).
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from empiremuse.rules.radio_station import valid_station_id
from empiremuse.utils.radio.premium import CAS_ATTEMPTS, GLOBAL_DB, RadioUnavailable

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool

logger = logging.getLogger(__name__)

CUSTOM_FAVORITES_DB = "radio_custom_favorites"
MAX_CUSTOM_FAVORITES = 24


class Toggled(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    FULL = "full"


class CustomFavorites:
    def __init__(self, pool: BotPool) -> None:
        self.pool = pool

    @staticmethod
    def _key(guild_id: int, user_id: int) -> str:
        return f"{int(guild_id)}:{int(user_id)}"

    async def _find(self, key: str) -> dict[str, Any] | None:
        try:
            return await self.pool.database.find_doc(key, db_name=CUSTOM_FAVORITES_DB, collection=GLOBAL_DB)
        except Exception as error:  # noqa: BLE001 - só o tipo vai ao log (R-12)
            logger.warning("Favoritas da personalizada não lidas (%s)", type(error).__name__)
            raise RadioUnavailable from error

    @staticmethod
    def _ids(doc: dict[str, Any] | None) -> list[str]:
        raw = doc.get("stations") if isinstance(doc, dict) else None
        found: list[str] = []
        for item in raw if isinstance(raw, list) else []:
            if valid_station_id(item) and item not in found:
                found.append(item)
        return found[:MAX_CUSTOM_FAVORITES]

    async def list(self, guild_id: int, user_id: int) -> list[str]:
        return self._ids(await self._find(self._key(guild_id, user_id)))

    async def toggle(self, guild_id: int, user_id: int, station_id: str) -> Toggled:
        """Liga/desliga ``station_id`` nas favoritas do membro. ``FULL`` com 24 (nada muda); ``RadioUnavailable`` se o banco não respondeu."""
        key = self._key(guild_id, user_id)
        for _ in range(CAS_ATTEMPTS):
            doc = await self._find(key)
            current = self._ids(doc)
            if station_id in current:
                result, ids = Toggled.REMOVED, [i for i in current if i != station_id]
            elif len(current) >= MAX_CUSTOM_FAVORITES:
                return Toggled.FULL
            else:
                result, ids = Toggled.ADDED, [*current, station_id]
            rev = doc["rev"] if isinstance(doc, dict) and isinstance(doc.get("rev"), int) else 0
            updated = {"_id": key, "stations": ids, "rev": rev + 1}
            try:
                saved = await self.pool.database.save_doc_cas(
                    key,
                    updated,
                    expected_rev=rev if doc is not None else None,
                    db_name=CUSTOM_FAVORITES_DB,
                    collection=GLOBAL_DB,
                )
            except Exception as error:  # noqa: BLE001
                logger.warning("Favoritas da personalizada não gravadas (%s)", type(error).__name__)
                raise RadioUnavailable from error
            if saved:
                return result
        raise RadioUnavailable
