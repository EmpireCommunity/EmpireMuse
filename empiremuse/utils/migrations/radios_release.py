# -*- coding: utf-8 -*-
"""Migração de subida da release de Rádios e Jukebox (contrato §3).

Roda no boot de forma idempotente, documento a documento na coleção ``servers`` do banco ``global``, com compare-and-set em ``rev``:
1. Lê os campos antigos uma única vez e grava os novos (``settings.jukebox_auto_delete``, ``settings.jukebox_text_requests = True``,
   seção ``jukebox`` e ``migrated.jukebox``), mantendo os campos antigos até o backup ser confirmado.
2. Salva o documento original em ``global.migration_backup`` com ``_id = "radios_release:servers:<id>"``, ``{old, at}`` e TTL de 90 dias.
3. Só então remove (``$unset``) os campos antigos (``settings.request_channel_strict``, ``request`` e ``migrated.request``) com CAS em ``rev``.

Se qualquer etapa falhar em um documento, ele mantém os campos antigos intactos e a próxima subida retoma. Quando todos os documentos
terminam sem pendências, grava o marcador em ``global.migrations`` com ``_id = "radios_release"``.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from empiremuse.rules.servers import SECTION_JUKEBOX, SECTION_SETTINGS
from empiremuse.utils.db import DBModel

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool

logger = logging.getLogger(__name__)

GLOBAL_COLLECTION = "global"
MIGRATIONS_DB = "migrations"
BACKUP_DB = "migration_backup"
SERVERS_COLLECTION = DBModel.servers
MARKER_ID = "radios_release"
BACKUP_TTL_SECONDS = 90 * 86400

OLD_SECTION = "request"
OLD_STRICT_KEY = "request_channel_strict"


@dataclass(frozen=True)
class MigrationReport:
    migrated: int = 0
    already: int = 0
    left_behind: int = 0
    skipped_marker: bool = False


def backup_id_for(collection: str, doc_id: str) -> str:
    return f"{MARKER_ID}:{collection}:{doc_id}"


def _needs_migration(doc: dict[str, Any]) -> bool:
    settings = doc.get(SECTION_SETTINGS)
    migrated = doc.get("migrated")
    if OLD_SECTION in doc:
        return True
    if isinstance(migrated, dict) and (OLD_SECTION in migrated or SECTION_JUKEBOX not in migrated):
        return True
    if isinstance(settings, dict) and (
        OLD_STRICT_KEY in settings or "jukebox_auto_delete" not in settings or "jukebox_text_requests" not in settings
    ):
        return True
    return SECTION_JUKEBOX not in doc


def _with_new_fields(doc: dict[str, Any]) -> dict[str, Any]:
    """Adiciona os campos novos a partir dos antigos (lidos uma única vez), preservando os antigos até o backup concluir."""
    updated = deepcopy(doc)
    settings = dict(updated.get(SECTION_SETTINGS) or {})
    if "jukebox_auto_delete" not in settings:
        settings["jukebox_auto_delete"] = settings.get(OLD_STRICT_KEY) is True
    elif not isinstance(settings["jukebox_auto_delete"], bool):
        settings["jukebox_auto_delete"] = False
    if "jukebox_text_requests" not in settings or not isinstance(settings["jukebox_text_requests"], bool):
        settings["jukebox_text_requests"] = True
    updated[SECTION_SETTINGS] = settings

    old_req = updated.get(OLD_SECTION) if isinstance(updated.get(OLD_SECTION), dict) else {}
    existing_jb = updated.get(SECTION_JUKEBOX)
    if not isinstance(existing_jb, dict) or (
        existing_jb.get("channel") is None
        and existing_jb.get("message_id") is None
        and existing_jb.get("panel_bot_id") is None
        and any(old_req.get(k) is not None for k in ("channel", "message_id", "panel_bot_id"))
    ):
        updated[SECTION_JUKEBOX] = {
            "channel": old_req.get("channel"),
            "message_id": old_req.get("message_id"),
            "panel_bot_id": old_req.get("panel_bot_id"),
        }

    migrated = dict(updated.get("migrated") or {})
    if not migrated.get(SECTION_JUKEBOX):
        migrated[SECTION_JUKEBOX] = bool(migrated.get(OLD_SECTION, False))
    updated["migrated"] = migrated
    return updated


def _without_old_fields(doc: dict[str, Any]) -> dict[str, Any]:
    """Remove os campos antigos (equivalente ao ``$unset`` de ``request``, ``migrated.request`` e ``settings.request_channel_strict``)."""
    cleaned = deepcopy(doc)
    cleaned.pop(OLD_SECTION, None)
    if isinstance(cleaned.get("migrated"), dict):
        cleaned["migrated"].pop(OLD_SECTION, None)
    if isinstance(cleaned.get(SECTION_SETTINGS), dict):
        cleaned[SECTION_SETTINGS].pop(OLD_STRICT_KEY, None)
    return cleaned


async def _ensure_backup_ttl_index(database: Any) -> None:
    mongo = getattr(database, "_mongo", None)
    if mongo is None:
        return
    try:
        await mongo[GLOBAL_COLLECTION][BACKUP_DB].create_index("at", expireAfterSeconds=BACKUP_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 - índice TTL é melhor esforço
        logger.debug("Índice TTL de migration_backup não criado (%s)", type(exc).__name__)


async def _save_backup(database: Any, doc_id: str, original: dict[str, Any]) -> None:
    key = backup_id_for(SERVERS_COLLECTION, doc_id)
    existing = await database.find_doc(key, db_name=BACKUP_DB, collection=GLOBAL_COLLECTION)
    if existing is not None:
        return
    snapshot = deepcopy(original)
    snapshot.pop("_id", None)
    payload = {
        "old": snapshot,
        "at": datetime.now(timezone.utc),
        "rev": 1,
    }
    saved = await database.save_doc_cas(
        key,
        payload,
        expected_rev=None,
        db_name=BACKUP_DB,
        collection=GLOBAL_COLLECTION,
    )
    if not saved:
        raise RuntimeError("Conflito ao gravar backup da migração")


async def _migrate_one(database: Any, doc_id: str, doc: dict[str, Any]) -> bool:
    """Migra um único documento com CAS em ``rev``. Devolve ``True`` se concluiu, ``False`` se conflitou ou falhou."""
    rev0 = doc.get("rev", 0)
    if not isinstance(rev0, int) or isinstance(rev0, bool):
        rev0 = 0

    step1_doc = _with_new_fields(doc)
    step1_doc.pop("_id", None)
    rev1 = rev0 + 1
    step1_doc["rev"] = rev1

    if not await database.save_doc_cas(
        doc_id,
        step1_doc,
        expected_rev=rev0 if "rev" in doc else None,
        db_name=SERVERS_COLLECTION,
        collection=GLOBAL_COLLECTION,
    ):
        return False

    await _save_backup(database, doc_id, doc)

    step2_doc = _without_old_fields(step1_doc)
    step2_doc.pop("_id", None)
    step2_doc["rev"] = rev1 + 1
    return await database.save_doc_cas(
        doc_id,
        step2_doc,
        expected_rev=rev1,
        db_name=SERVERS_COLLECTION,
        collection=GLOBAL_COLLECTION,
    )


async def run(pool: BotPool) -> MigrationReport:
    """Executa a migração ``radios_release`` de forma idempotente."""
    database = pool.database
    marker = await database.find_doc(MARKER_ID, db_name=MIGRATIONS_DB, collection=GLOBAL_COLLECTION)
    if marker is not None:
        return MigrationReport(skipped_marker=True)

    await _ensure_backup_ttl_index(database)

    docs: list[dict[str, Any]] = []
    async for raw in database.iter_data(db_name=SERVERS_COLLECTION, collection=GLOBAL_COLLECTION):
        if isinstance(raw, dict) and raw.get("_id") is not None:
            docs.append(raw)

    migrated = already = left_behind = 0
    servers_cache = getattr(getattr(pool, "servers", None), "_docs", None)

    for raw in docs:
        doc_id = str(raw["_id"])
        current = await database.find_doc(doc_id, db_name=SERVERS_COLLECTION, collection=GLOBAL_COLLECTION)
        if current is None:
            continue
        if not _needs_migration(current):
            already += 1
            continue
        try:
            ok = await _migrate_one(database, doc_id, current)
        except Exception as exc:  # noqa: BLE001 - falha em um documento não aborta os demais
            logger.warning("Documento [%s] não migrado em radios_release (%s)", doc_id, type(exc).__name__)
            ok = False
        if servers_cache is not None:
            servers_cache.pop(doc_id, None)
        if ok:
            migrated += 1
        else:
            left_behind += 1

    if left_behind == 0:
        try:
            await database.save_doc_cas(
                MARKER_ID,
                {"at": datetime.now(timezone.utc), "migrated": migrated, "rev": 1},
                expected_rev=None,
                db_name=MIGRATIONS_DB,
                collection=GLOBAL_COLLECTION,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Marcador radios_release não gravado (%s)", type(exc).__name__)

    if migrated or left_behind:
        logger.info("radios_release: %d migrados, %d já no formato novo, %d pendentes", migrated, already, left_behind)
    return MigrationReport(migrated=migrated, already=already, left_behind=left_behind)
