"""Gêneros persistidos viram refs virtuais; referências primeiro, exclusão por id por último.

Sem backup (decisão do dono). CAS perdido ou documento alterado deixa a estação
para a próxima subida; só uma rodada completa grava o marcador. Logs só contêm contagens.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from empiremuse.rules.radio import KIND_CUSTOM
from empiremuse.rules.radio_station import valid_station_id
from empiremuse.utils.radio.custom_favorites import CUSTOM_FAVORITES_DB, MAX_CUSTOM_FAVORITES
from empiremuse.utils.radio.premium import GLOBAL_DB
from empiremuse.utils.radio.stations import STATIONS_DB, StationStore
from empiremuse.utils.radio.store import RadioConflict, RadioInvalid, RadioNotFound

if TYPE_CHECKING:
    from empiremuse.utils.radio import RadioHub

logger = logging.getLogger(__name__)
MIGRATIONS_DB = "migrations"
MARKER_ID = "radio_genres_builtin"


@dataclass(frozen=True)
class MigrationReport:
    migrated: int = 0
    removed: int = 0
    inert: int = 0
    left_behind: int = 0
    skipped_marker: bool = False


def guild_id(value: object) -> int | None:
    """Snowflake canônico (contrato rev. 3.1); bool nunca é inteiro aqui."""
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,19}", value):
        value = int(value)
    if type(value) is int and 0 < value < 2**64:
        return value
    return None


def _identity(doc: Any) -> tuple[int, str] | None:
    if not isinstance(doc, dict) or doc.get("kind") != "genre":
        return None
    sid, gid = doc.get("_id"), guild_id(doc.get("guild_id"))
    if not isinstance(sid, str) or re.fullmatch(r"[0-9a-f]{24}", sid) is None or gid is None:
        return None
    return gid, sid


def _ref(doc: dict[str, Any]) -> str | None:
    source = doc.get("source")
    genre = source.get("genre") if isinstance(source, dict) else None
    ref = "g:" + genre if isinstance(genre, str) else None
    return ref if StationStore.builtin(0, ref) is not None else None


async def _favorites(database: Any, mapping: dict[tuple[int, str], str]) -> set[int]:
    failed: set[int] = set()
    async for doc in database.iter_data(CUSTOM_FAVORITES_DB, GLOBAL_DB):
        if not isinstance(doc, dict):
            continue
        key = doc.get("_id")
        parts = key.split(":") if isinstance(key, str) else []
        gid = guild_id(parts[0]) if len(parts) == 2 and guild_id(parts[1]) is not None else None
        raw = doc.get("stations")
        if gid is None or not isinstance(raw, list):
            continue
        if not any(isinstance(s, str) and (gid, s) in mapping for s in raw):
            continue
        ids: list[str] = []
        for sid in raw:
            if not valid_station_id(sid):
                continue
            ref = mapping.get((gid, sid), sid)
            if ref not in ids:
                ids.append(ref)
        rev = doc.get("rev", 0)
        if type(rev) is not int or rev < 0:
            failed.add(gid)
            continue
        saved = await database.save_doc_cas(
            key,
            {**doc, "stations": ids[:MAX_CUSTOM_FAVORITES], "rev": rev + 1},
            expected_rev=rev,
            db_name=CUSTOM_FAVORITES_DB,
            collection=GLOBAL_DB,
        )
        if not saved:
            failed.add(gid)
    return failed


async def run(hub: RadioHub) -> MigrationReport:
    """Idempotente; documentos inválidos ficam inertes, indisponibilidade de banco sobe ao boot."""
    database = hub.store.pool.database
    if await database.find_doc(MARKER_ID, db_name=MIGRATIONS_DB, collection=GLOBAL_DB) is not None:
        return MigrationReport(skipped_marker=True)
    docs = [doc async for doc in database.iter_data(STATIONS_DB, GLOBAL_DB, {"kind": "genre"})]
    accepted = [(identity, doc) for doc in docs if (identity := _identity(doc)) is not None]
    inert = len(docs) - len(accepted)
    if docs and not accepted:
        logger.error("radio.migrate_genres: %d documentos inertes; marcador não gravado", inert)
        return MigrationReport(inert=inert, left_behind=inert)
    mapping = {identity: ref for identity, doc in accepted if (ref := _ref(doc)) is not None}
    failed: set[int] = set()
    migrated = removed = 0
    for radio in await hub.store.list_all():
        old = radio.source.get("station")
        ref = mapping.get((radio.guild_id, old)) if isinstance(old, str) else None
        if radio.kind != KIND_CUSTOM or ref is None:
            continue
        try:
            await hub.store.replace_station_ref(radio.id, ref, radio.rev)
        except RadioConflict, RadioNotFound, RadioInvalid:
            failed.add(radio.guild_id)
        else:
            migrated += 1
    failed.update(await _favorites(database, mapping))
    left = 0
    for identity, original in accepted:
        gid, sid = identity
        if gid in failed:
            left += 1
            continue
        current = await database.find_doc(sid, db_name=STATIONS_DB, collection=GLOBAL_DB)
        if current is None:
            continue  # outra instância já terminou
        if _identity(current) != identity or current != original:
            left += 1  # editada/trocada durante a migração: não apaga
            continue
        await database.delete_data(sid, STATIONS_DB, GLOBAL_DB)
        if await database.find_doc(sid, db_name=STATIONS_DB, collection=GLOBAL_DB) is not None:
            left += 1  # o adaptador local pode recusar apagar sem levantar
        else:
            removed += 1
    if not left:
        await database.save_doc_cas(
            MARKER_ID,
            {"at": time.time(), "migrated": migrated, "removed": removed, "rev": 1},
            expected_rev=None,
            db_name=MIGRATIONS_DB,
            collection=GLOBAL_DB,
        )
    logger.info(
        "radio.migrate_genres: %d migradas, %d apagadas, %d fora do catálogo, %d inertes, %d para depois",
        migrated,
        removed,
        len(accepted) - len(mapping),
        inert,
        left,
    )
    return MigrationReport(migrated, removed, inert, left)
