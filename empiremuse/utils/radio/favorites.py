"""As estações favoritas do SERVIDOR (item 8): a lista que o painel da rádio ao vivo alimenta com "⭐ Favoritar" e o seletor de estações oferece como uma lista.

Um documento por servidor na coleção ``radio_favorites`` do banco ``global`` (``_id`` = id do servidor em texto), com compare-and-set em ``rev``: dois membros
favoritando ao mesmo tempo não perdem um ao outro. Só guarda o ``uuid`` e o nome já limpo pela função única de texto externo (R-11); o endereço do fluxo
nunca é guardado (cada play pergunta ao diretório de novo). No máximo 24 estações, o que cabe num select junto com os outros itens do seletor.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Any, NamedTuple

from empiremuse.rules.radio_stations import Station, valid_uuid
from empiremuse.rules.text import external_text
from empiremuse.utils.radio.premium import CAS_ATTEMPTS, GLOBAL_DB, RadioUnavailable

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool

logger = logging.getLogger(__name__)

FAVORITES_DB = "radio_favorites"
MAX_FAVORITES = 24
NAME_LIMIT = 100


class FavoriteStation(NamedTuple):
    uuid: str
    name: str


class AddResult(StrEnum):
    ADDED = "added"
    ALREADY = "already"
    FULL = "full"


class RadioFavorites:
    def __init__(self, pool: BotPool) -> None:
        self.pool = pool

    async def _find(self, guild_id: int) -> dict[str, Any] | None:
        try:
            return await self.pool.database.find_doc(str(int(guild_id)), db_name=FAVORITES_DB, collection=GLOBAL_DB)
        except Exception as error:  # noqa: BLE001 - só o tipo vai ao log (R-12)
            logger.warning("Favoritas da rádio não lidas (%s)", type(error).__name__)
            raise RadioUnavailable from error

    async def _save(self, guild_id: int, doc: dict[str, Any], expected_rev: int | None) -> bool:
        try:
            return bool(
                await self.pool.database.save_doc_cas(
                    str(int(guild_id)), doc, expected_rev=expected_rev, db_name=FAVORITES_DB, collection=GLOBAL_DB
                )
            )
        except Exception as error:  # noqa: BLE001
            logger.warning("Favoritas da rádio não gravadas (%s)", type(error).__name__)
            raise RadioUnavailable from error

    @staticmethod
    def _stations(doc: dict[str, Any] | None) -> list[FavoriteStation]:
        """As favoritas de um documento; item que não passa nas regras é ignorado em silêncio (dado gravado ruim nunca levanta)."""
        raw = doc.get("stations") if isinstance(doc, dict) else None
        found: list[FavoriteStation] = []
        for item in raw if isinstance(raw, list) else []:
            uuid, name = (item.get("uuid"), item.get("name")) if isinstance(item, dict) else (None, None)
            if valid_uuid(uuid) and isinstance(name, str) and name and all(f.uuid != uuid for f in found):
                found.append(
                    FavoriteStation(uuid, external_text(name, NAME_LIMIT, markdown=False))
                )  # sem escapar de novo: o nome já veio escapado
        return found[:MAX_FAVORITES]

    async def list(self, guild_id: int) -> list[FavoriteStation]:
        return self._stations(await self._find(guild_id))

    async def add(self, guild_id: int, station: Station) -> AddResult:
        """Põe a estação na lista do servidor. ``ALREADY`` se já estava e ``FULL`` com 24; ``RadioUnavailable`` se o banco não respondeu."""
        for _ in range(CAS_ATTEMPTS):
            doc = await self._find(guild_id)
            current = self._stations(doc)
            if any(f.uuid == station.uuid for f in current):
                return AddResult.ALREADY
            if len(current) >= MAX_FAVORITES:
                return AddResult.FULL
            rev = doc["rev"] if isinstance(doc, dict) and isinstance(doc.get("rev"), int) else 0
            updated = {
                "_id": str(int(guild_id)),
                "stations": [
                    *({"uuid": f.uuid, "name": f.name} for f in current),
                    {
                        "uuid": station.uuid,
                        "name": station.name[:NAME_LIMIT],
                    },  # o nome da estação já passou pela função única (R-11),
                ],
                "rev": rev + 1,
            }
            if await self._save(guild_id, updated, rev if doc is not None else None):
                return AddResult.ADDED
        raise RadioUnavailable
