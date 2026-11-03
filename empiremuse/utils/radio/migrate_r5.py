"""A migração da R5 (item 8, contrato §2): a rádio personalizada que apontava para uma PLAYLIST passa a apontar para uma ESTAÇÃO do servidor.

Roda na subida, antes de retomar as rádios, e é idempotente: para cada rádio ``custom`` com ``{playlist: P}`` cria (ou reaproveita) a estação ``P`` do tipo
``playlists: [P]`` e troca a fonte para ``{station: <id>}`` com compare-and-set em ``rev``. Não apaga nada: a playlist continua onde estava e, se ela sumiu, a
estação nasce igual e a rádio mostra "estação sem faixas tocáveis". Servidor que já tem 10 estações fica com a rádio antiga (que segue tocando como antes) até
sobrar vaga; a rodada seguinte tenta de novo. O marcador ``global.migrations/radio_r5`` só é gravado quando nenhuma rádio ficou para trás.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from empiremuse.rules.radio import KIND_CUSTOM
from empiremuse.rules.radio_station import KIND_PLAYLISTS, NAME_MAX, normalize_name
from empiremuse.utils.radio.premium import GLOBAL_DB
from empiremuse.utils.radio.stations import StationConflict, StationInvalid
from empiremuse.utils.radio.store import RadioConflict, RadioInvalid, RadioNotFound

if TYPE_CHECKING:
    from empiremuse.utils.radio import RadioHub

logger = logging.getLogger(__name__)

MIGRATIONS_DB = "migrations"  # nome da coleção
MARKER_ID = "radio_r5"
NAME_TRIES = 9  # "Nome", "Nome 2" ... "Nome 9" quando o nome já é de outra estação


@dataclass(frozen=True)
class MigrationReport:
    migrated: int = 0
    already: int = 0
    left_behind: int = 0  # ficaram com a playlist (servidor com 10 estações ou erro): a próxima subida tenta de novo
    skipped_marker: bool = False


def _name_for(playlist: str, attempt: int) -> str:
    suffix = "" if attempt == 0 else f" {attempt + 1}"
    return playlist.strip()[: NAME_MAX - len(suffix)].rstrip() + suffix


async def _marker(hub: RadioHub) -> dict[str, Any] | None:
    return await hub.store.pool.database.find_doc(MARKER_ID, db_name=MIGRATIONS_DB, collection=GLOBAL_DB)


async def _station_for(hub: RadioHub, guild_id: int, playlist: str) -> str | None:
    """O id da estação de playlist ``[P]`` do servidor: a que já existe ou uma nova; ``None`` se não coube (10 estações) ou o nome não serve."""
    existing = await hub.stations.list_for_guild(guild_id)
    for station in existing:
        if station.kind == KIND_PLAYLISTS and station.source.get("playlists") == [playlist]:
            return station.id
    taken = {s.name_norm for s in existing}
    for attempt in range(NAME_TRIES):
        name = _name_for(playlist, attempt)
        if normalize_name(name) in taken:
            continue
        try:
            created = await hub.stations.create(
                guild_id,
                {"name": name, "kind": KIND_PLAYLISTS, "source": {"playlists": [playlist]}},
                playlists={playlist},  # a playlist pode ter sumido: a estação nasce igual (a rádio mostra "sem faixas")
                updated_by="migration",
            )
        except StationInvalid, StationConflict:
            return None
        return created.id
    return None


async def run(hub: RadioHub) -> MigrationReport:
    """Migra todas as rádios personalizadas de playlist. Nunca levanta por causa de UMA rádio; banco fora (``RadioUnavailable``) sobe para o chamador tentar depois."""
    database = hub.store.pool.database
    if await _marker(hub) is not None:
        return MigrationReport(skipped_marker=True)
    migrated = already = left_behind = 0
    for radio in await hub.store.list_all():
        if radio.kind != KIND_CUSTOM:
            continue
        playlist = radio.source.get("playlist")
        if not isinstance(playlist, str):
            already += 1
            continue
        station_id = await _station_for(hub, radio.guild_id, playlist)
        if station_id is None:
            left_behind += 1
            continue
        try:
            await hub.store.migrate_playlist_source(radio.id, station_id, expected_rev=radio.rev)
        except RadioConflict, RadioInvalid, RadioNotFound:
            left_behind += 1  # mudou no meio (outro comando ou a própria migração em outra instância): a próxima rodada confere de novo
            continue
        migrated += 1
    report = MigrationReport(migrated, already, left_behind)
    if left_behind == 0:
        try:
            await database.save_doc_cas(
                MARKER_ID,
                {"at": time.time(), "migrated": migrated, "rev": 1},
                expected_rev=None,
                db_name=MIGRATIONS_DB,
                collection=GLOBAL_DB,
            )
        except Exception as error:  # noqa: BLE001 - sem marcador a próxima subida só repete (idempotente): só o tipo vai ao log (R-12)
            logger.warning("Marcador da migração das rádios não gravado (%s)", type(error).__name__)
    logger.info("radio.migrate_r5: %d migradas, %d já em estação, %d para depois", migrated, already, left_behind)
    return report
