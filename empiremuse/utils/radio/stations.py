"""As estações do servidor para a rádio personalizada no banco (item 8, R5, contrato §2): ler, criar, editar e apagar, com as regras de ``rules/radio_station``.

Coleção ``radio_stations`` do banco ``global``, um documento por estação (``_id`` = 12 bytes aleatórios em hex). Tudo que muda passa por compare-and-set em
``rev``; criar e editar ao mesmo tempo no MESMO servidor passam por um cadeado por servidor (o nome é único e o teto é de 10, coisas que o CAS de um
documento só não vê). Apagar uma estação em uso por uma rádio é recusado e a recusa diz quais rádios a usam. O chat e o painel web chamam estas mesmas funções.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import secrets
import time
from collections.abc import Collection
from typing import TYPE_CHECKING, Any

from empiremuse.rules.errors import RuleError
from empiremuse.rules.radio import KIND_CUSTOM, Radio
from empiremuse.rules.radio_genres import GROUPS, Genre, shipped_catalog
from empiremuse.rules.radio_station import (
    CustomStation,
    check_station_doc,
    from_document,
    to_document,
    valid_station_id,
)
from empiremuse.utils.radio.premium import CAS_ATTEMPTS, GLOBAL_DB, RadioUnavailable
from empiremuse.utils.radio.store import RadioConflict, RadioInvalid, RadioNotFound, RadioStore

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool

logger = logging.getLogger(__name__)

STATIONS_DB = "radio_stations"  # nome da coleção
STATION_IN_USE = "station_in_use"


class StationConflict(RadioConflict):
    """O ``rev`` mudou (ou o id já existe): quem editava leu uma versão velha."""


class StationNotFound(RadioNotFound):
    """A estação não existe neste servidor (também quando é de outro servidor)."""


class StationInvalid(RadioInvalid):
    """``check_station_doc`` recusou o pedido; ``error`` leva o ``RuleError`` (código estável e o campo em ``key``)."""


class StationInUse(RadioInvalid):
    """A estação está em uso; ``radios`` = os ids das rádios (``'<bot_id>:<guild_id>'``) que a usam."""

    def __init__(self, radios: list[str]) -> None:
        super().__init__(RuleError(STATION_IN_USE, "station"))
        self.radios = radios


class StationStore:
    def __init__(self, pool: BotPool, radios: RadioStore) -> None:
        self.pool = pool
        self.radios = radios
        self._locks: dict[int, asyncio.Lock] = {}

    @property
    def _database(self) -> Any:
        return self.pool.database

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(int(guild_id), asyncio.Lock())

    # ------------------------------------------------------------------ banco

    async def _find(self, station_id: str) -> dict[str, Any] | None:
        try:
            return await self._database.find_doc(station_id, db_name=STATIONS_DB, collection=GLOBAL_DB)
        except Exception as error:  # noqa: BLE001 - só o tipo vai ao log (R-12)
            logger.warning("Estação não lida (%s)", type(error).__name__)
            raise RadioUnavailable from error

    async def _save(self, station_id: str, doc: dict[str, Any], expected_rev: int | None) -> bool:
        try:
            return bool(
                await self._database.save_doc_cas(
                    station_id, doc, expected_rev=expected_rev, db_name=STATIONS_DB, collection=GLOBAL_DB
                )
            )
        except Exception as error:  # noqa: BLE001
            logger.warning("Estação não gravada (%s)", type(error).__name__)
            raise RadioUnavailable from error

    async def _documents(self, guild_id: int) -> list[dict[str, Any]]:
        try:
            return [
                doc async for doc in self._database.iter_data(STATIONS_DB, GLOBAL_DB, {"guild_id": str(int(guild_id))})
            ]
        except Exception as error:  # noqa: BLE001
            logger.warning("Estações não listadas (%s)", type(error).__name__)
            raise RadioUnavailable from error

    @staticmethod
    def _station(doc: dict[str, Any] | None, guild_id: int) -> CustomStation | None:
        """A estação de um documento; documento inválido, ou de OUTRO servidor, é ``None`` (R-6: o id vindo de fora nunca basta)."""
        if not doc:
            return None
        station = from_document(doc)
        if isinstance(station, RuleError):
            logger.warning("Estação com documento inválido ignorada (%s)", station.code)
            return None
        return station if station.guild_id == int(guild_id) else None

    # ------------------------------------------------------------------ leitura

    @staticmethod
    def genre_groups() -> tuple[tuple[str, tuple[str, ...]], ...]:
        """``(grupo, (ids de gênero...))`` na ordem do seletor de dois níveis (o nome mostrado vem do i18n)."""
        catalog = shipped_catalog()
        return tuple((group, tuple(g.id for g in catalog if g.group == group)) for group in GROUPS)

    @staticmethod
    def genres() -> tuple[Genre, ...]:
        return shipped_catalog()

    async def list_for_guild(self, guild_id: int) -> list[CustomStation]:
        """As estações do servidor, por nome (sem diferenciar a caixa)."""
        found = (self._station(doc, guild_id) for doc in await self._documents(guild_id))
        return sorted((s for s in found if s is not None), key=lambda s: s.name_norm)

    async def get(self, guild_id: int, station_id: object) -> CustomStation | None:
        if not valid_station_id(station_id):
            return None
        return self._station(await self._find(station_id), guild_id)  # type: ignore[arg-type]

    async def radios_using(self, guild_id: int, station_id: str) -> list[Radio]:
        """As rádios personalizadas do servidor que tocam esta estação."""
        return [
            r
            for r in await self.radios.list_for_guild(guild_id)
            if r.kind == KIND_CUSTOM and r.source.get("station") == station_id
        ]

    # ------------------------------------------------------------------ escrita

    async def create(
        self, guild_id: int, raw: object, *, playlists: Collection[str] = (), updated_by: str = ""
    ) -> CustomStation:
        """Cria uma estação. ``StationInvalid`` se o pedido não passa nas regras (nome repetido, 11ª estação, fonte ruim), ``RadioUnavailable`` sem banco."""
        async with self._lock(guild_id):
            current = await self.list_for_guild(guild_id)
            checked = check_station_doc(
                raw, playlists=playlists, taken={s.name_norm for s in current}, count=len(current)
            )
            if isinstance(checked, RuleError):
                raise StationInvalid(checked)
            station = CustomStation(
                id=secrets.token_hex(12),
                guild_id=int(guild_id),
                name=checked.name,
                kind=checked.kind,
                source=checked.source,
                updated_by=str(updated_by)[:40],
                updated_at=time.time(),
            )
            if not await self._save(station.id, to_document(station), None):
                raise StationConflict
            return station

    async def update(
        self,
        guild_id: int,
        station_id: str,
        raw: object,
        *,
        expected_rev: int,
        playlists: Collection[str] = (),
        updated_by: str = "",
    ) -> CustomStation:
        """Troca nome, tipo e fonte. ``StationNotFound``, ``StationConflict`` (``rev`` velho) ou ``StationInvalid``."""
        async with self._lock(guild_id):
            current = await self.get(guild_id, station_id)
            if current is None:
                raise StationNotFound
            if current.rev != expected_rev:
                raise StationConflict
            others = [s for s in await self.list_for_guild(guild_id) if s.id != station_id]
            checked = check_station_doc(
                raw, playlists=playlists, taken={s.name_norm for s in others}, count=0
            )  # editar não conta para o teto: a estação já existe
            if isinstance(checked, RuleError):
                raise StationInvalid(checked)
            unknown = (
                current.unknown if checked.source == current.source else ()
            )  # a lista de ⚠️ vale só para os mesmos artistas
            updated = dataclasses.replace(
                current,
                name=checked.name,
                kind=checked.kind,
                source=checked.source,
                rev=current.rev + 1,
                updated_by=str(updated_by)[:40],
                updated_at=time.time(),
                unknown=unknown,
            )
            if not await self._save(station_id, to_document(updated), expected_rev):
                raise StationConflict
            return updated

    async def delete(self, guild_id: int, station_id: str, *, expected_rev: int) -> None:
        """Apaga a estação. ``StationInUse`` (com as rádios) enquanto alguma rádio a usa; ``StationNotFound``; ``StationConflict``."""
        async with self._lock(guild_id):
            current = await self.get(guild_id, station_id)
            if current is None:
                raise StationNotFound
            if current.rev != expected_rev:
                raise StationConflict
            using = await self.radios_using(guild_id, station_id)
            if using:
                raise StationInUse([r.id for r in using])
            try:
                await self._database.delete_data(station_id, STATIONS_DB, GLOBAL_DB)
            except Exception as error:  # noqa: BLE001
                logger.warning("Estação não apagada (%s)", type(error).__name__)
                raise RadioUnavailable from error

    async def set_unknown(self, guild_id: int, station_id: str, unknown: Collection[str]) -> None:
        """Grava os artistas que o Last.fm não conhece (os ⚠️ das duas telas). Só quando muda, e com CAS; perder a corrida é desistir em silêncio."""
        wanted = tuple(sorted(unknown))
        for _ in range(CAS_ATTEMPTS):
            current = await self.get(guild_id, station_id)
            if current is None or tuple(sorted(current.unknown)) == wanted:
                return
            updated = dataclasses.replace(current, unknown=wanted, rev=current.rev + 1)
            if await self._save(station_id, to_document(updated), current.rev):
                return
