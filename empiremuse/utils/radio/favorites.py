"""As estações favoritas do SERVIDOR (item 8): a lista que o menu administrativo da rádio ao vivo alimenta e o seletor oferece como uma lista; a estrela pessoal usa user_favorites.py.

Um documento por servidor na coleção ``radio_favorites`` do banco ``global`` (``_id`` = id do servidor em texto), com compare-and-set em ``rev``: dois membros
favoritando ao mesmo tempo não perdem um ao outro. Só guarda o ``uuid`` e o nome já limpo pela função única de texto externo (R-11); o endereço do fluxo
nunca é guardado (cada play pergunta ao diretório de novo). No máximo 24 estações, o que cabe num select junto com os outros itens do seletor.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, NamedTuple

from empiremuse.rules.radio_station import valid_station_id
from empiremuse.rules.radio_stations import Station, valid_uuid
from empiremuse.rules.text import external_text
from empiremuse.utils.radio.directory import DirectoryError
from empiremuse.utils.radio.perms import can_manage_guild
from empiremuse.utils.radio.premium import CAS_ATTEMPTS, GLOBAL_DB, RadioUnavailable

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool

logger = logging.getLogger(__name__)

FAVORITES_DB = "radio_favorites"
MAX_FAVORITES = 24
SERVER_FAVORITES_MAX = MAX_FAVORITES  # o teto da lista do servidor vale para a SOMA de ao vivo, ``custom`` e ``genre``
NAME_LIMIT = 100


class FavoriteForbidden(Exception):
    """O ator não pode gerenciar as favoritas deste servidor."""


class FavoriteNotFound(Exception):
    """Estação ou filtro ausente/inválido; sem payload pessoal."""


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
            if (
                isinstance(uuid, str)
                and valid_uuid(uuid)
                and isinstance(name, str)
                and name
                and all(f.uuid != uuid for f in found)
            ):
                found.append(
                    FavoriteStation(uuid, external_text(name, NAME_LIMIT, markdown=False))
                )  # sem escapar de novo: o nome já veio escapado
        return found[:MAX_FAVORITES]

    @staticmethod
    def _refs(doc: dict[str, Any] | None) -> list[str]:
        """Os ids ``custom`` (24 hex) e ``genre`` (``g:<id>``) do documento, SEM nome (resolvido na leitura); documento antigo, sem ``refs``, lê normal."""
        raw = doc.get("refs") if isinstance(doc, dict) else None
        found: list[str] = []
        for item in raw if isinstance(raw, list) else []:
            if valid_station_id(item) and item not in found:
                found.append(item)
        return found[:MAX_FAVORITES]

    @classmethod
    def _read(cls, doc: dict[str, Any] | None) -> tuple[list[FavoriteStation], list[str]]:
        """``(estações ao vivo, refs)`` com o teto de 24 sobre a SOMA: o que passa do teto (dado gravado ruim) sai da leitura."""
        stations = cls._stations(doc)
        return stations, cls._refs(doc)[: max(0, MAX_FAVORITES - len(stations))]

    @staticmethod
    def _document(guild_id: int, stations: list[FavoriteStation], refs: list[str], rev: int) -> dict[str, Any]:
        """O documento SEMPRE com as duas listas: todo escritor preserva a outra (um que regravasse só ``stations`` perderia ``refs``).
        Nunca guarda quem adicionou (a exclusão de dados depende disso: este documento não tem id de usuário)."""
        return {
            "_id": str(int(guild_id)),
            "stations": [{"uuid": f.uuid, "name": f.name} for f in stations],
            "refs": list(refs),
            "rev": rev,
        }

    async def list(self, guild_id: int) -> list[FavoriteStation]:
        return self._stations(await self._find(guild_id))

    async def list_all(self, guild_id: int) -> tuple[list[FavoriteStation], list[str]]:
        """As duas listas do servidor, na ordem de inserção de cada uma (ao vivo e depois ``custom``/``genre``)."""
        return self._read(await self._find(guild_id))

    async def add(self, guild_id: int, actor_id: int, station: Station) -> AddResult:
        """Põe a estação na lista do servidor. ``ALREADY`` se já estava e ``FULL`` com 24 (somando os ``custom``/``genre``); ``RadioUnavailable`` se o banco não respondeu."""
        await self._authorize(guild_id, actor_id)
        for _ in range(CAS_ATTEMPTS):
            doc = await self._find(guild_id)
            current, refs = self._read(doc)
            if any(f.uuid == station.uuid for f in current):
                return AddResult.ALREADY
            if len(current) + len(refs) >= MAX_FAVORITES:
                return AddResult.FULL
            rev = doc["rev"] if isinstance(doc, dict) and isinstance(doc.get("rev"), int) else 0
            added = FavoriteStation(station.uuid, external_text(station.name, NAME_LIMIT, markdown=False))
            # o nome da estação já passou pela função única (R-11)
            if await self._save(
                guild_id, self._document(guild_id, [*current, added], refs, rev + 1), rev if doc is not None else None
            ):
                return AddResult.ADDED
        raise RadioUnavailable

    async def add_live(
        self,
        guild_id: int,
        actor_id: int,
        uuid: str,
        resolve: Callable[[str], Awaitable[Station | None]],
    ) -> AddResult:
        """Como ``add``, mas a estação só é lida do diretório (``resolve``) DEPOIS de ``ALREADY`` e ``FULL``: um pedido que não muda nada nunca gasta o
        balde do diretório, e a leitura é feita uma vez só (um retry do CAS não volta a ele). Estação que o diretório não conhece: ``FavoriteNotFound``;
        diretório fora: ``RadioUnavailable``. O nome nunca vem do chamador."""
        if not valid_uuid(uuid):
            raise FavoriteNotFound
        await self._authorize(guild_id, actor_id)
        resolved: list[FavoriteStation] = []
        for _ in range(CAS_ATTEMPTS):
            doc = await self._find(guild_id)
            current, refs = self._read(doc)
            if any(f.uuid == uuid for f in current):
                return AddResult.ALREADY
            if len(current) + len(refs) >= MAX_FAVORITES:
                return AddResult.FULL
            if not resolved:
                try:
                    station = await resolve(uuid)
                except DirectoryError as error:
                    raise RadioUnavailable from error
                if station is None:
                    raise FavoriteNotFound
                resolved.append(FavoriteStation(uuid, external_text(station.name, NAME_LIMIT, markdown=False)))
            rev = doc["rev"] if isinstance(doc, dict) and isinstance(doc.get("rev"), int) else 0
            if await self._save(
                guild_id,
                self._document(guild_id, [*current, resolved[0]], refs, rev + 1),
                rev if doc is not None else None,
            ):
                return AddResult.ADDED
        raise RadioUnavailable

    async def add_ref(
        self, guild_id: int, actor_id: int, ref: str, exists: Callable[[str], Awaitable[bool]]
    ) -> AddResult:
        """Põe uma estação do servidor (24 hex) ou um gênero (``g:<id>``) na lista do servidor, sem nome (resolvido na leitura). ``exists`` só é
        chamado depois de ``ALREADY`` e ``FULL`` e uma vez; ``False`` = ``FavoriteNotFound`` (a de outro servidor é igual a uma que não existe)."""
        if not valid_station_id(ref):
            raise FavoriteNotFound
        await self._authorize(guild_id, actor_id)
        checked = False
        for _ in range(CAS_ATTEMPTS):
            doc = await self._find(guild_id)
            current, refs = self._read(doc)
            if ref in refs:
                return AddResult.ALREADY
            if len(current) + len(refs) >= MAX_FAVORITES:
                return AddResult.FULL
            if not checked:
                if not await exists(ref):
                    raise FavoriteNotFound
                checked = True
            rev = doc["rev"] if isinstance(doc, dict) and isinstance(doc.get("rev"), int) else 0
            if await self._save(
                guild_id, self._document(guild_id, current, [*refs, ref], rev + 1), rev if doc is not None else None
            ):
                return AddResult.ADDED
        raise RadioUnavailable

    async def _authorize(self, guild_id: int, actor_id: int) -> None:
        if not await can_manage_guild(self.pool, guild_id, actor_id):
            raise FavoriteForbidden

    async def remove(self, guild_id: int, actor_id: int, uuid: str) -> bool:
        return await self._remove(guild_id, actor_id, uuid=uuid)

    async def remove_ref(self, guild_id: int, actor_id: int, ref: str) -> bool:
        return await self._remove(guild_id, actor_id, ref=ref)

    async def clear(self, guild_id: int, actor_id: int) -> bool:
        """Limpa as estações AO VIVO (o botão do Discord); os ``custom``/``genre`` ficam."""
        return await self._remove(guild_id, actor_id, everything=True)

    async def _remove(
        self, guild_id: int, actor_id: int, *, uuid: str | None = None, ref: str | None = None, everything: bool = False
    ) -> bool:
        await self._authorize(guild_id, actor_id)
        for _ in range(CAS_ATTEMPTS):
            doc = await self._find(guild_id)
            current, refs = self._read(doc)
            updated = [] if everything else [f for f in current if f.uuid != uuid]
            updated_refs = [r for r in refs if r != ref]
            if updated == current and updated_refs == refs:
                return False
            rev = doc["rev"] if isinstance(doc, dict) and isinstance(doc.get("rev"), int) else 0
            if await self._save(
                guild_id, self._document(guild_id, updated, updated_refs, rev + 1), rev if doc is not None else None
            ):
                return True
        raise RadioUnavailable
