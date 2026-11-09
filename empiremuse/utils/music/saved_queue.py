"""Fila salva do usuário: caminhos, gravação e leitura em JSON.

O formato é ``local_database/saved_queues_json/users/<id>.json`` (ver ``empiremuse.serialization.queue``). O ``.pkl`` antigo de
``saved_queues_v1/users/`` não é lido (o aviso da subida está em ``legacy_notice``), mas ``delete_saved_queues`` o apaga junto com o JSON
quando o membro apaga os próprios dados. Fila ilegível vira
``GenericError`` ("incompatível") e o arquivo é mantido; nada do conteúdo vai ao log.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

import aiofiles

from empiremuse import paths
from empiremuse.rules.erasure import saved_queue_file_names, saved_queue_tmp_matches
from empiremuse.serialization.files import write_private_atomic
from empiremuse.serialization.queue import MAX_BYTES, decode_saved_queue, encode_saved_queue
from empiremuse.serialization.schema import SchemaError
from empiremuse.utils.music.guild_text import guild_text_sync
from empiremuse.utils.music.errors import GenericError

logger = logging.getLogger(__name__)


def _users_dir(folder: str) -> Path:
    return paths.RUNTIME_DIR / "local_database" / folder / "users"


def saved_queue_path(user_id: Union[int, str]) -> Path:
    return _users_dir("saved_queues_json") / f"{int(user_id)}.json"


def has_saved_queue(user_id: Union[int, str]) -> bool:
    return saved_queue_path(user_id).is_file()


def _is_file_or_link(path: Path) -> bool:
    """Arquivo regular ou link simbólico (sem segui-lo, nem quebrado). Só a ausência é "não"; qualquer outro ``OSError`` sobe (``is_file`` o engoliria)."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return False
    return stat.S_ISREG(mode) or stat.S_ISLNK(mode)


def saved_queue_files(user_id: Union[int, str]) -> list[Path]:
    """Os arquivos do membro que ``delete_saved_queues`` apaga e que existem agora: o JSON, o ``.pkl`` antigo e o temporário órfão.

    Os nomes saem só do inteiro (``rules.erasure``), nunca por glob ou prefixo. Só arquivo regular ou link simbólico (um link conta como link e
    nunca é seguido); pasta é ignorada. Pasta ausente não é erro; outro ``OSError`` sobe.
    """
    uid = int(user_id)
    json_name, pkl_name = saved_queue_file_names(uid)
    json_dir = _users_dir("saved_queues_json")
    candidates = [json_dir / json_name, _users_dir("saved_queues_v1") / pkl_name]
    try:
        candidates += [json_dir / name for name in os.listdir(json_dir) if saved_queue_tmp_matches(name, uid)]
    except FileNotFoundError:
        pass
    return [path for path in candidates if _is_file_or_link(path)]


def delete_saved_queues(user_id: Union[int, str]) -> int:
    """Apaga a fila salva do membro (§2 linha 23 do contrato de apagar dados); devolve quantos arquivos saíram. Bloqueia: rode num executor.

    Arquivo ausente não é erro; qualquer outro ``OSError`` sobe ao serviço, que o converte em ``PARTIAL``.
    """
    removed = 0
    for path in saved_queue_files(user_id):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        removed += 1
    return removed


def _incompatible(guild=None) -> GenericError:
    return GenericError(guild_text_sync(guild, "legacy.saved_queue.incompatible"))


async def _read_limited(path: Path, limit: int) -> Optional[bytes]:
    """Até ``limit + 1`` bytes do arquivo (o excedente acusa tamanho acima do teto sem ler o arquivo todo), ou ``None``."""
    try:
        async with aiofiles.open(path, "rb") as file:
            return await file.read(limit + 1)
    except FileNotFoundError:
        return None


async def load_saved_queue(user_id: Union[int, str], guild_id: Union[int, str], guild=None) -> dict:
    """``{"tracks": [info...], "created_at", "guild_id"}`` da fila salva do usuário, ou ``GenericError``."""
    loop = asyncio.get_running_loop()
    raw = await _read_limited(saved_queue_path(user_id), MAX_BYTES)
    if raw is not None:
        try:
            return await loop.run_in_executor(None, decode_saved_queue, raw)
        except SchemaError as error:
            logger.warning("Fila salva ignorada (formato incompatível): servidor %s, motivo %s", guild_id, error.code)
            raise _incompatible(guild) from None

    raise GenericError(guild_text_sync(guild, "legacy.saved_queue.deleted"))


async def store_saved_queue(
    user_id: Union[int, str], tracks: list, guild_id: int, created_at: datetime, guild=None
) -> None:
    """Grava a fila em JSON (atômico, 0600)."""
    try:
        raw = encode_saved_queue(tracks, created_at, guild_id)
    except SchemaError as error:
        logger.warning(
            "Fila não salva (dados incompatíveis): servidor %s, campo %s, motivo %s", guild_id, error.path, error.code
        )
        raise GenericError(guild_text_sync(guild, "legacy.saved_queue.save_incompatible")) from None
    try:
        await asyncio.get_running_loop().run_in_executor(None, write_private_atomic, saved_queue_path(user_id), raw)
    except OSError as error:
        logger.warning("Fila não salva (falha ao gravar): servidor %s, motivo %s", guild_id, type(error).__name__)
        raise GenericError(guild_text_sync(guild, "legacy.saved_queue.save_failed")) from None
