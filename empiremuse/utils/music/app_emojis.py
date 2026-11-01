# -*- coding: utf-8 -*-
"""Emojis de aplicação: o mapa de reserva Unicode, ``emoji(bot, nome)`` e o ``sync_app_emojis``.

Contrato em ``.local/specs/app-emojis.md``. O formato ``<:nome:id>`` só é montado aqui, com o NOSSO nome constante e o id convertido
para ``int``; um id vindo da API que não é inteiro é ignorado.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import stat
import struct
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import disnake

logger = logging.getLogger(__name__)

# nome do emoji -> Unicode de reserva (o de cada lugar antes dos emojis de aplicação)
APP_EMOJIS: Mapping[str, str] = MappingProxyType(
    {
        "music_queue": "📑",
        "add_music": "➕",
        "play": "▶️",
        "play_source": "▶️",
        "playpause": "⏯️",
        "stop": "⏹️",
        "skip": "⏭️",
        "backward": "⏮️",
        "arrow_left": "⬅️",
        "arrow_right": "➡️",
        "leftwards_arrow_with_hook": "↩️",
        "help": "❓",
        "git": "🛠️",
        "stagechannel": "🎙️",
        "bandcamp": "💿",
        "youtube": "📺",
        "youtube_music": "🎶",
        "spotify": "🟢",
        "soundcloud": "☁️",
        "deezer": "🟣",
        "applemusic": "🍎",
        "twitch": "🎮",
        "jiosaavn": "📻",
        "tidal": "🌊",
        "lastfm": "🎧",
        # sem uso no código ainda (§12 do contrato)
        "loved": "❤️",
        "search": "🔍",
        "config": "📝",
        "gear": "⚙️",
        "replay": "🔁",
        "tools": "🧰",
        "pinned": "📌",
        "bot": "🤖",
        "voice_event": "🔊",
        "community_server": "🏘️",
        "plus": "➕",
        "headset": "🎧",
        # ícones do player (mnauliady): a reserva é o Unicode que cada lugar usava antes
        "history": "⏰",
        "microphone": "💠",
        "user": "✋",
        "playlists": "📄",
        "speaker": "*️⃣",
        "notification": "💡",
        "fast_backward": "⏪",
        "volume": "🔊",
        "shuffle": "🔀",
        "playlist_add": "🎶",
        "repeat_all": "🔁",
        "sound_spectrum": "🇳",
        "refresh": "🔄",
        "lock": "🔐",
        "drag": "↪️",
        "like": "👍",
        "music_notes": "🎶",
        "music_note": "🎵",
        "playlist_play": "🎶",
        "cancel": "❌",
        "star": "⭐",
        "lyrics": "📃",
        "comment": "💬",
        "disc": "💽",
        "record": "🔴",
        "repeat": "🔂",
        "pause": "⏸️",
    }
)

# chaves do ``music_sources`` do ``emojis.json`` do dono que não coincidem com o nome do emoji
_OWNER_KEYS = {"youtube_music": "youtubemusic", "lastfm": "last.fm"}

_APP_EMOJI_RE = re.compile(r"^<a?:[A-Za-z0-9_]{2,32}:\d+>$")

MAX_FILE_BYTES = 256 * 1024
EMOJI_SIDE = 128
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_HEADER_BYTES = 24
_EXTENSIONS = ("png", "gif")
_DEFAULT_RETRY_AFTER = 5.0
_MAX_RETRY_AFTER = 60.0
_NAME_EXISTS_CODE = 50035
_SUMS_NAME = "SHA256SUMS"


def _owner_emoji(bot: Any, name: str) -> str | None:
    """Valor do ``emojis.json`` do dono (``pool.emoji_data``) para este nome, se for um texto utilizável."""
    try:
        sources = bot.pool.emoji_data["music_sources"]
        if not isinstance(sources, Mapping):
            return None
        value = sources.get(name)
        if value is None and name in _OWNER_KEYS:
            value = sources.get(_OWNER_KEYS[name])
    except Exception:
        return None

    if isinstance(value, str) and value.strip() and "\n" not in value and "\r" not in value:
        return value

    return None


def _bot_emoji(bot: Any, name: str) -> str | None:
    """Emoji de aplicação DESTE bot, só se estiver no formato que o Discord renderiza."""
    try:
        value = bot.app_emojis.get(name)
    except Exception:
        return None

    return value if isinstance(value, str) and _APP_EMOJI_RE.match(value) else None


def emoji(bot: Any, name: str) -> str:
    """``emojis.json`` do dono > emoji de aplicação do bot que envia > Unicode de reserva. Nome fora da lista é erro de programação."""
    fallback = APP_EMOJIS[name]

    return _owner_emoji(bot, name) or _bot_emoji(bot, name) or fallback


def _format_emoji(name: str, emoji_id: object, animated: object) -> str | None:
    if isinstance(emoji_id, bool) or not isinstance(emoji_id, (int, str)):
        return None

    if isinstance(emoji_id, str) and not (emoji_id.isascii() and emoji_id.isdigit()):
        return None

    return f"<{'a' if animated else ''}:{name}:{int(emoji_id)}>"


def _index_remote(remote: object) -> dict[str, str]:
    """Os emojis da aplicação que são NOSSOS (nome exato entre os 25), já no formato de uso."""
    found: dict[str, str] = {}

    for item in remote or ():
        name = getattr(item, "name", None)

        if not isinstance(name, str) or name not in APP_EMOJIS or name in found:
            continue

        value = _format_emoji(name, getattr(item, "id", None), getattr(item, "animated", False))

        if value:
            found[name] = value

    return found


def _read_sums(emojis_dir: Path) -> dict[str, str]:
    """Linhas do ``SHA256SUMS``; arquivo ausente, ilegível ou que não é arquivo dá ``{}`` (nada é enviado: falha fechada)."""
    sums_file = emojis_dir.parent / _SUMS_NAME

    try:
        if not sums_file.is_file():
            return {}

        pairs = (line.split(None, 1) for line in sums_file.read_text(encoding="utf-8").splitlines() if line.strip())
        return {path.strip().lstrip("*"): digest for digest, path in (p for p in pairs if len(p) == 2)}
    except OSError, UnicodeDecodeError:
        return {}


def _header_size(header: bytes) -> tuple[int, int] | None:
    if header.startswith(_PNG_SIGNATURE) and len(header) >= _HEADER_BYTES and header[12:16] == b"IHDR":
        return struct.unpack(">II", header[16:24])

    if header[:6] in (b"GIF87a", b"GIF89a") and len(header) >= 10:
        return struct.unpack("<HH", header[6:10])

    return None


def _load_image(emojis_dir: Path, name: str, sums: dict[str, str]) -> tuple[bytes | None, str]:
    """``(bytes, "")`` do arquivo do emoji, ou ``(None, motivo)``. Valida tudo ANTES de ler o arquivo inteiro."""
    if not any(key.startswith("emojis/") for key in sums):
        return None, "SHA256SUMS ausente"

    candidates = [emojis_dir / f"{name}.{ext}" for ext in _EXTENSIONS]
    path = next((p for p in candidates if p.is_symlink() or p.exists()), None)

    if path is None:
        return None, "arquivo ausente"

    try:
        info = path.lstat()

        if not stat.S_ISREG(info.st_mode):
            return None, "não é arquivo regular"

        if path.resolve().parent != emojis_dir.resolve():
            return None, "fora da pasta"

        if not 0 < info.st_size <= MAX_FILE_BYTES:
            return None, "tamanho inválido"

        with open(path, "rb") as handle:
            size = _header_size(handle.read(_HEADER_BYTES))

        if size is None:
            return None, "tipo inválido"

        if size != (EMOJI_SIDE, EMOJI_SIDE):
            return None, "dimensão inválida"

        data = path.read_bytes()
    except OSError:
        return None, "erro de leitura"

    if len(data) > MAX_FILE_BYTES:
        return None, "tamanho inválido"

    expected = sums.get(f"emojis/{path.name}")

    if expected is None:
        return None, "sem linha no SHA256SUMS"

    if expected.lower() != hashlib.sha256(data).hexdigest():
        return None, "SHA256SUMS diverge"

    return data, ""


def _is_global_limit(error: disnake.HTTPException) -> bool:
    try:
        headers = getattr(getattr(error, "response", None), "headers", None) or {}
        return any(str(k).lower() == "x-ratelimit-global" and str(v).lower() == "true" for k, v in headers.items())
    except Exception:
        return False


def _retry_delay(error: disnake.HTTPException) -> float:
    value = getattr(error, "retry_after", None)

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _DEFAULT_RETRY_AFTER

    return min(max(float(value), 0.0), _MAX_RETRY_AFTER)


def _kind(error: BaseException) -> str:
    status = getattr(error, "status", None)
    return f"{type(error).__name__} {status}" if status else type(error).__name__


async def _create_with_retry(bot: Any, name: str, data: bytes, sleep: Callable[[float], Awaitable[Any]]):
    """Um 429 espera ``min(retry_after, 60)`` UMA vez e tenta de novo (o disnake já repete por conta própria); o 2º 429 ou o global sobe."""
    try:
        return await bot.create_application_emoji(name=name, image=data)
    except disnake.HTTPException as error:
        if error.status != 429 or _is_global_limit(error):
            raise

        await sleep(_retry_delay(error))

    return await bot.create_application_emoji(name=name, image=data)


async def sync_app_emojis(
    bot: Any,
    *,
    emojis_dir: Path,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> dict[str, str]:
    """Garante os 25 emojis na aplicação do ``bot`` e devolve ``nome -> "<:nome:id>"``. Nunca apaga nem sobrescreve; nunca levanta."""
    try:
        remote = await bot.fetch_application_emojis()
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.warning("Emojis de aplicação: não foi possível listar os existentes (%s)", _kind(error))
        return {}

    mapping = _index_remote(remote)
    reused = sorted(mapping)
    created: list[str] = []
    failures: list[tuple[str, str]] = []
    stopped = False
    refetch_left = True
    emojis_dir = Path(emojis_dir)
    folder_ok = not emojis_dir.is_symlink() and emojis_dir.is_dir()
    sums = _read_sums(emojis_dir) if folder_ok else {}

    for name in APP_EMOJIS:
        if name in mapping or stopped:
            continue

        data, reason = _load_image(emojis_dir, name, sums) if folder_ok else (None, "arquivo ausente")

        if data is None:
            failures.append((name, reason))
            continue

        try:
            item = await _create_with_retry(bot, name, data, sleep)
        except asyncio.CancelledError:
            raise
        except disnake.HTTPException as error:
            if error.status == 400 and error.code == _NAME_EXISTS_CODE and refetch_left:
                refetch_left = False
                try:
                    found = _index_remote(await bot.fetch_application_emojis())
                except asyncio.CancelledError:
                    raise
                except Exception:
                    found = {}
                if name in found:
                    mapping[name] = found[name]
                    reused.append(name)
                    continue

            failures.append((name, _kind(error)))
            stopped = error.status in (401, 403, 429)
            continue
        except Exception as error:
            failures.append((name, _kind(error)))
            continue

        value = _format_emoji(name, getattr(item, "id", None), getattr(item, "animated", False))

        if value:
            mapping[name] = value
            created.append(name)
        else:
            failures.append((name, "resposta inválida"))

    if created or reused:
        logger.info(
            "Emojis de aplicação: criados %s; reaproveitados %s", created or "nenhum", sorted(reused) or "nenhum"
        )

    if failures:
        logger.warning(
            "Emojis de aplicação sem sincronizar (%d)%s: %s",
            len(failures),
            " [sync interrompido]" if stopped else "",
            ", ".join(f"{name} ({reason})" for name, reason in failures),
        )

    return {name: mapping[name] for name in APP_EMOJIS if name in mapping}
