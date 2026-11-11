"""Favoritas e filtros pessoais globais (J3), sem cache e sem URLs.

Um documento por usuário em global.radio_user, CAS em rev. A identidade vem da
interação/sessão. Só a UI efêmera ou a conta do próprio membro pode ler a lista.
A futura exclusão usa _id decimal e não precisa importar este módulo.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeGuard

from empiremuse.rules.radio_stations import valid_uuid
from empiremuse.rules.text import external_text
from empiremuse.utils.radio.custom_favorites import Toggled
from empiremuse.utils.radio.directory import DirectoryError
from empiremuse.utils.radio.favorites import AddResult, FavoriteNotFound, FavoriteStation, NAME_LIMIT, RadioFavorites
from empiremuse.utils.radio.premium import CAS_ATTEMPTS, GLOBAL_DB, RadioUnavailable

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool
    from empiremuse.utils.radio import RadioHub

USER_FAVORITES_DB = "radio_user"
USER_LIVE_FAVORITES_MAX = 24
USER_FILTERS_MAX = 10
_NODE = re.compile(r"[A-Za-z0-9_:.\-]{1,48}\Z")
Change = Callable[[list[FavoriteStation], list[str]], Awaitable[tuple[list[FavoriteStation], list[str], Any]]]


def valid_node(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and _NODE.fullmatch(value) is not None


_valid_node = valid_node


class UserRadioFavorites:
    def __init__(self, pool: BotPool, hub: RadioHub) -> None:
        self.pool, self.hub = pool, hub

    @staticmethod
    def _key(user: int) -> str:
        if type(user) is not int:
            raise TypeError("user must be int from the interaction or session")
        return str(user)

    async def _find(self, key: str) -> dict[str, Any] | None:
        try:
            return await self.pool.database.find_doc(key, db_name=USER_FAVORITES_DB, collection=GLOBAL_DB)
        except Exception as error:  # noqa: BLE001 - nenhum identificador ou conteúdo pessoal no log
            raise RadioUnavailable from error

    @staticmethod
    def _nodes(doc: dict[str, Any] | None) -> list[str]:
        raw = doc.get("filters") if isinstance(doc, dict) else None
        found: list[str] = []
        for item in raw if isinstance(raw, list) else []:
            node = item.get("node") if isinstance(item, dict) else None
            if _valid_node(node) and node not in found:
                found.append(node)
        return found[:USER_FILTERS_MAX]

    async def stations(self, user: int) -> list[FavoriteStation]:
        return RadioFavorites._stations(await self._find(self._key(user)))

    async def filters(self, user: int) -> list[str]:
        nodes = self._nodes(await self._find(self._key(user)))
        tree = self.hub.locations
        return [n for n in nodes if tree.node(n) is not None] if tree is not None else []

    async def _change(self, user: int, change: Change) -> Any:
        key = self._key(user)
        for _ in range(CAS_ATTEMPTS):
            doc = await self._find(key)
            stations, nodes = RadioFavorites._stations(doc), self._nodes(doc)
            updated, filters, result = await change(stations, nodes)
            if updated == stations and filters == nodes:
                return result
            rev = doc["rev"] if isinstance(doc, dict) and type(doc.get("rev")) is int else 0
            updated_doc = {
                "_id": key,
                "stations": [{"uuid": f.uuid, "name": f.name} for f in updated],
                "filters": [{"node": n} for n in filters],
                "rev": rev + 1,
            }
            try:
                saved = await self.pool.database.save_doc_cas(
                    key,
                    updated_doc,
                    expected_rev=rev if doc is not None else None,
                    db_name=USER_FAVORITES_DB,
                    collection=GLOBAL_DB,
                )
            except Exception as error:  # noqa: BLE001
                raise RadioUnavailable from error
            if saved:
                return result
        raise RadioUnavailable

    async def add_station(self, user: int, uuid: str) -> AddResult:
        """Põe uma estação AO VIVO nas favoritas da conta (a estrela da API; o ``toggle`` fica para a do Discord). Ordem: formato, leitura, já está, cheia (24),
        diretório (``by_uuid_strict``, uma vez só e só aqui), CAS. O nome nunca vem do chamador."""
        self._key(user)
        if not valid_uuid(uuid):
            raise FavoriteNotFound
        resolved: list[FavoriteStation] = []

        async def add(
            current: list[FavoriteStation], nodes: list[str]
        ) -> tuple[list[FavoriteStation], list[str], AddResult]:
            if any(f.uuid == uuid for f in current):
                return current, nodes, AddResult.ALREADY
            if len(current) >= USER_LIVE_FAVORITES_MAX:
                return current, nodes, AddResult.FULL
            if not resolved:
                try:
                    station = await self.hub.directory.by_uuid_strict(uuid)
                except DirectoryError as error:
                    raise RadioUnavailable from error
                if station is None:
                    raise FavoriteNotFound
                resolved.append(FavoriteStation(uuid, external_text(station.name, NAME_LIMIT, markdown=False)))
            return [*current, resolved[0]], nodes, AddResult.ADDED

        return await self._change(user, add)

    async def add_filter(self, user: int, node_id: str) -> AddResult:
        """Põe um local (nó da árvore) nos filtros da conta. Ordem: formato, leitura, já está, cheio (10), a árvore conhece o nó, CAS."""
        self._key(user)
        if not _valid_node(node_id):
            raise FavoriteNotFound

        async def add(
            stations: list[FavoriteStation], nodes: list[str]
        ) -> tuple[list[FavoriteStation], list[str], AddResult]:
            if node_id in nodes:
                return stations, nodes, AddResult.ALREADY
            if len(nodes) >= USER_FILTERS_MAX:
                return stations, nodes, AddResult.FULL
            if self.hub.locations is None or self.hub.locations.node(node_id) is None:
                raise FavoriteNotFound
            return stations, [*nodes, node_id], AddResult.ADDED

        return await self._change(user, add)

    async def toggle_station(self, user: int, uuid: str) -> Toggled:
        self._key(user)
        if not valid_uuid(uuid):
            raise FavoriteNotFound

        resolved: list[FavoriteStation] = []  # resolvida uma vez só: um retry do CAS nunca volta ao diretório

        async def resolve() -> FavoriteStation:
            if not resolved:
                try:
                    station = await self.hub.directory.by_uuid_strict(uuid)
                except DirectoryError as error:
                    raise RadioUnavailable from error
                if station is None:
                    raise FavoriteNotFound
                resolved.append(FavoriteStation(uuid, external_text(station.name, NAME_LIMIT, markdown=False)))
            return resolved[0]

        async def toggle(
            current: list[FavoriteStation], nodes: list[str]
        ) -> tuple[list[FavoriteStation], list[str], Toggled]:
            if any(f.uuid == uuid for f in current):
                return [f for f in current if f.uuid != uuid], nodes, Toggled.REMOVED
            if len(current) >= USER_LIVE_FAVORITES_MAX:
                return current, nodes, Toggled.FULL
            return (
                [*current, await resolve()],
                nodes,
                Toggled.ADDED,
            )

        return await self._change(user, toggle)

    async def toggle_filter(self, user: int, node_id: str) -> Toggled:
        self._key(user)
        if not _valid_node(node_id):
            raise FavoriteNotFound

        async def toggle(
            stations: list[FavoriteStation], nodes: list[str]
        ) -> tuple[list[FavoriteStation], list[str], Toggled]:
            if node_id in nodes:
                return stations, [n for n in nodes if n != node_id], Toggled.REMOVED
            if len(nodes) >= USER_FILTERS_MAX:
                return stations, nodes, Toggled.FULL
            if self.hub.locations is None or self.hub.locations.node(node_id) is None:
                raise FavoriteNotFound
            return stations, [*nodes, node_id], Toggled.ADDED

        return await self._change(user, toggle)

    async def remove_station(self, user: int, uuid: str) -> bool:
        async def remove(
            stations: list[FavoriteStation], nodes: list[str]
        ) -> tuple[list[FavoriteStation], list[str], bool]:
            return [f for f in stations if f.uuid != uuid], nodes, any(f.uuid == uuid for f in stations)

        return await self._change(user, remove)

    async def clear_stations(self, user: int) -> bool:
        async def clear(
            stations: list[FavoriteStation], nodes: list[str]
        ) -> tuple[list[FavoriteStation], list[str], bool]:
            return [], nodes, bool(stations)

        return await self._change(user, clear)

    async def remove_filter(self, user: int, node_id: str) -> bool:
        async def remove(
            stations: list[FavoriteStation], nodes: list[str]
        ) -> tuple[list[FavoriteStation], list[str], bool]:
            return stations, [n for n in nodes if n != node_id], node_id in nodes

        return await self._change(user, remove)

    async def clear_filters(self, user: int) -> bool:
        async def clear(
            stations: list[FavoriteStation], nodes: list[str]
        ) -> tuple[list[FavoriteStation], list[str], bool]:
            return stations, [], bool(nodes)

        return await self._change(user, clear)
