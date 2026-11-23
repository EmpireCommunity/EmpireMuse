"""Sessão do player em JSON (v1): o que o ``PlayerSession.save_info`` monta e o ``resume_player`` consome, validado.

``encode_session`` recebe o dicionário do ``save_info`` (``time`` ``datetime`` com fuso, ``dj`` ``set``, ``command_log_list``
``deque``) e devolve o JSON (UTF-8 compacto, até 8 MiB); ``decode_session`` e ``validate_session`` devolvem o que o consumidor lê
(``time`` ``datetime`` UTC, ``dj`` e ``command_log_list`` listas, sem ``v``), com os valores padrão do player nos campos que o
``resume_player`` indexa e que faltarem. ``voice_state``, ``extra_info`` e qualquer chave desconhecida nunca são gravados nem lidos.

Faixa que o esquema recusa (ou de fonte ``local``) é descartada e a sessão segue; URL inválida de faixa vira ``""`` (``None``
quebra a construção da faixa); campo obrigatório ruim (``_id``, ``voice_channel``, ``time``, ``queue``) recusa a sessão. ``time``
fora de 2000-01-01 até agora + 5 min é recusado (no futuro a sessão nunca expiraria). Mensagens e logs levam só caminho e código.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from empiremuse.rules.urls import clean_url

from .codec import dumps, loads_bounded
from .schema import (
    OMIT,
    PATTERN,
    RANGE,
    SNOWFLAKE,
    TOO_BIG,
    TYPE,
    Bool,
    Dict,
    Float,
    Int,
    List,
    Optional,
    SchemaError,
    Str,
    _Schema,
    validate,
)
from .skins import validate_skin
from .tracks import BASE64, MAX_ENCODED_TRACK, MAX_TEXT, clean_track, optional, plain, text

logger = logging.getLogger(__name__)

VERSION = 1
MAX_BYTES = 8 * 1024 * 1024
MAX_NUMBER = 10**12
MAX_DJ = 100
MAX_LASTFM_ARTISTS = 100
MAX_COMMAND_LOG = 10
MAX_SKIN_NAME = 256
ID_MISMATCH = "id_mismatch"
LISTS = {"queue": 5000, "played": 1000, "queue_autoplay": 200, "failed_tracks": 500}
MIN_TIME = datetime(2000, 1, 1, tzinfo=timezone.utc)
FUTURE_TOLERANCE = timedelta(minutes=5)

_ISO_WITH_OFFSET = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?([+-][0-9]{2}:[0-9]{2})"
_LOOPS = ("current", "queue")


# ============================================================================ URLs das faixas (condição 3 do 5.1)


def _valid_url(value: Any) -> bool:
    return type(value) is str and len(value) <= MAX_TEXT and clean_url(value) == value


def _url_or_empty(value: Any) -> str:
    return value if value == "" or _valid_url(value) else ""


def _fix_playlist(info: dict, fixed: dict) -> None:
    playlist = info.get("playlist")
    if type(playlist) is dict:
        fixed["playlist"] = {
            key: _url_or_empty(item) if key in ("url", "thumb") and item is not None else item
            for key, item in playlist.items()
        }


def _fix_album(info: dict, fixed: dict) -> None:
    extra = info.get("extra")
    album = extra.get("album") if type(extra) is dict else None
    if type(album) is dict and "url" in album:
        fixed["extra"] = {**extra, "album": {**album, "url": _url_or_empty(album["url"])}}


def _fix_plugin_info(info: dict, fixed: dict) -> None:
    plugin = info.get("pluginInfo")
    if type(plugin) is not dict:
        return
    keep = {
        key: item
        for key, item in plugin.items()
        if not (type(key) is str and key.endswith(("Url", "url"))) or item is None or item == "" or _valid_url(item)
    }
    if len(keep) != len(plugin):
        fixed["pluginInfo"] = keep


def _fix_urls(info: dict) -> dict:
    """Cópia da faixa com as URLs conferidas pela régua de ``rules/urls.py``: a inválida vira ``""`` (ou a chave sai)."""
    fixed = dict(info)
    fixed["uri"] = _url_or_empty(info.get("uri"))
    if info.get("artworkUrl") is not None:
        fixed["artworkUrl"] = _url_or_empty(info["artworkUrl"])
    _fix_playlist(info, fixed)
    _fix_album(info, fixed)
    _fix_plugin_info(info, fixed)
    return fixed


def _session_track(info: Any, path: str) -> dict | None:
    if type(info) is not dict or info.get("sourceName") == "local":
        return None
    try:
        return clean_track(_fix_urls(info), path)
    except SchemaError:
        return None


# ============================================================================ tipos do esquema


class _Defaulted(Optional):
    """Campo que o ``resume_player`` indexa: ausente vira ``default``; presente tem de valer (``None`` não vale)."""

    __slots__ = ()

    def check(self, value: Any, path: str) -> Any:
        return self.inner.check(value, path)


class _Tracks(_Schema):
    """Lista de faixas: o teto de itens vale antes de olhar os itens; faixa ruim é descartada (com contagem no log)."""

    __slots__ = ("limit",)

    def __init__(self, limit: int):
        self.limit = limit

    def check(self, value: Any, path: str) -> list:
        if type(value) is not list:
            raise SchemaError(path, TYPE)
        if len(value) > self.limit:
            raise SchemaError(path, "length")
        kept = [track for index, info in enumerate(value) if (track := _session_track(info, f"{path}[{index}]"))]
        if len(kept) != len(value):
            logger.warning("Sessão: %d faixa(s) descartada(s) em %s", len(value) - len(kept), path)
        return kept


class _Loop(_Schema):
    __slots__ = ()

    def check(self, value: Any, path: str) -> Any:
        if type(value) is bool or (type(value) is str and value in _LOOPS):
            return value
        raise SchemaError(path, PATTERN if type(value) is str else TYPE)


class _Dj(_Schema):
    __slots__ = ()
    _IDS = List(SNOWFLAKE, MAX_DJ)

    def check(self, value: Any, path: str) -> list:
        return sorted(set(self._IDS.check(value, path)))


class _SkinMap(_Schema):
    """``{nome: skin}`` com no máximo uma entrada; o que não valer vira ``{}`` (a skin padrão assume) e a sessão segue."""

    __slots__ = ()
    _NAME = Str(MAX_SKIN_NAME, allow_controls=True)

    def check(self, value: Any, path: str) -> dict:
        if type(value) is not dict or len(value) != 1:
            return {}
        ((name, skin),) = value.items()
        try:
            return {self._NAME.check(name, path): validate_skin(skin)}
        except SchemaError as error:
            logger.warning("Skin da sessão ignorada (formato incompatível): motivo %s", error.code)
            return {}


class _Invite(Str):
    """Convite de ``listen along``: URL válida ou o campo sai (a sessão fica sem convite)."""

    __slots__ = ()

    def check(self, value: Any, path: str) -> Any:
        invite = super().check(value, path)
        return invite if not invite or _valid_url(invite) else OMIT


class _Time(_Schema):
    """Instante ISO-8601 com offset (ou ``datetime`` com fuso) entre 2000-01-01 e agora + 5 min, devolvido em UTC."""

    __slots__ = ()
    _TEXT = Str(64, pattern=_ISO_WITH_OFFSET)

    def check(self, value: Any, path: str) -> datetime:
        if type(value) is datetime:
            if value.tzinfo is None:
                raise SchemaError(path, TYPE)
            moment = value
        else:
            text_ = self._TEXT.check(value, path)
            try:
                moment = datetime.fromisoformat(text_)
            except ValueError:
                raise SchemaError(path, PATTERN) from None
        try:
            moment = moment.astimezone(timezone.utc)
        except OverflowError:  # perto de 0001-01-01 ou 9999-12-31 com offset: não cabe em UTC
            raise SchemaError(path, RANGE) from None
        if not MIN_TIME <= moment <= datetime.now(timezone.utc) + FUTURE_TOLERANCE:
            raise SchemaError(path, RANGE)
        return moment


def _number(maximum: float = MAX_NUMBER) -> Float:
    return Float(0, maximum)


_LOG_ITEM = Dict(
    {
        "text": Str(500, multiline=True, allow_format=True),
        "emoji": Str(64, allow_format=True),
        "timestamp": _number(),
    },
    extra="ignore",
)

_SESSION = Dict(
    {
        "_id": SNOWFLAKE,
        "voice_channel": SNOWFLAKE,
        "time": _Time(),
        "queue": _Tracks(LISTS["queue"]),
        "played": _Defaulted(_Tracks(LISTS["played"]), []),
        "queue_autoplay": _Defaulted(_Tracks(LISTS["queue_autoplay"]), []),
        "failed_tracks": _Defaulted(_Tracks(LISTS["failed_tracks"]), []),
        "version": _Defaulted(_number(), 1),
        "volume": _Defaulted(Int(0, 1000), 100),
        "skin": _Defaulted(Str(256, allow_format=True), "default"),
        "skin_static": _Defaulted(Str(256, allow_format=True), "default"),
        "loop": _Defaulted(_Loop(), False),
        "dj": _Defaulted(_Dj(), []),
        "custom_skin_data": _Defaulted(_SkinMap(), {}),
        "custom_skin_static_data": _Defaulted(_SkinMap(), {}),
        "text_channel_id": Optional(SNOWFLAKE, default=None),
        "message_id": Optional(SNOWFLAKE, default=None),
        "player_creator": Optional(SNOWFLAKE, default=None),
        "static": Optional(Bool(), default=False),
        "restrict_mode": Optional(Bool(), default=False),
        "prefix_info": Optional(Str(32), default=""),
        "last_voice_channel_id": optional(SNOWFLAKE),
        "nightcore": optional(Bool()),
        "paused": optional(Bool()),
        "keep_connected": optional(Bool()),
        "autoplay": optional(Bool()),
        "radio_session": optional(Bool()),  # gravada no modo rádio da Jukebox: a retomada fica ociosa, sem autoplay
        "stage_title_event": optional(Bool()),
        "mini_queue_enabled": optional(Bool()),
        "live_lyrics_status": optional(Bool()),
        "position": optional(_number()),
        "uptime": optional(_number()),
        "start_timestamp": optional(_number()),
        "stage_title_template": optional(Str(500, multiline=True, allow_format=True)),
        "listen_along_invite": optional(_Invite(MAX_TEXT)),
        "current_encoded": optional(Str(MAX_ENCODED_TRACK, pattern=BASE64)),
        "lastfm_artists": optional(List(text(256), MAX_LASTFM_ARTISTS)),
        "command_log_list": optional(List(_LOG_ITEM, MAX_COMMAND_LOG)),
    },
    extra="ignore",
)


# ============================================================================ validar, ler


def _prepare(data: Any) -> Any:
    """Da forma do ``save_info`` para a do JSON: ``set`` e ``deque`` viram lista; tupla das faixas também."""
    if type(data) is not dict:
        return data
    prepared = dict(data)
    if type(prepared.get("dj")) in (set, frozenset):
        prepared["dj"] = sorted(prepared["dj"], key=lambda item: (type(item).__name__, item))
    if type(prepared.get("command_log_list")) is deque:
        prepared["command_log_list"] = list(prepared["command_log_list"])
    for name in LISTS:
        if name in prepared:
            prepared[name] = plain(prepared[name])
    return prepared


def _check_identity(clean: dict, expected_id: int | None) -> dict:
    if expected_id is not None and clean["_id"] != expected_id:
        raise SchemaError("_id", ID_MISMATCH)
    return clean


def validate_session(data: Any, expected_id: int | None = None) -> dict:
    """A sessão validada (cópia limpa, no formato do consumidor) ou ``SchemaError``; ``expected_id`` confere o ``_id``."""
    if expected_id is not None:
        SNOWFLAKE.check(expected_id, "expected_id")
    return _check_identity(validate(_SESSION, _prepare(data)), expected_id)


def decode_session(raw: bytes | str, expected_id: int | None = None) -> dict:
    """Lê o JSON (até 8 MiB, versão 1), valida e devolve o que o ``resume_player`` consome."""
    if expected_id is not None:
        SNOWFLAKE.check(expected_id, "expected_id")
    document = loads_bounded(raw, max_bytes=MAX_BYTES, version=VERSION)
    del document["v"]
    return _check_identity(validate(_SESSION, document), expected_id)


# ============================================================================ gravar (com redução por tamanho)


class _Parts:
    """O JSON da sessão em pedaços: a base (campos pequenos) e, por lista, o JSON de cada faixa (para medir e cortar)."""

    def __init__(self, clean: dict):
        self.tracks = {name: clean.pop(name) for name in LISTS}
        clean["time"] = clean["time"].isoformat()
        clean["v"] = VERSION
        self.base = dumps(clean)
        self.pieces = {name: deque(dumps(track) for track in tracks) for name, tracks in self.tracks.items()}

    def size(self) -> int:
        total = len(self.base)
        for name, pieces in self.pieces.items():
            total += len(name) + 6 + sum(map(len, pieces)) + max(len(pieces) - 1, 0)
        return total

    def assemble(self) -> bytes:
        lists = b"".join(b',"%s":[%s]' % (name.encode(), b",".join(pieces)) for name, pieces in self.pieces.items())
        return self.base[:-1] + lists + b"}"

    def without_lyrics(self) -> int:
        removed = 0
        for name, tracks in self.tracks.items():
            for index, track in enumerate(tracks):
                extra = track.get("extra")
                if type(extra) is dict and "lyrics" in extra:
                    reduced = {**track, "extra": {key: item for key, item in extra.items() if key != "lyrics"}}
                    tracks[index] = reduced
                    self.pieces[name][index] = dumps(reduced)
                    removed += 1
        return removed

    def trim(self, name: str, from_end: bool, keep: int = 0) -> int:
        """Tira faixas de ``name`` (do começo ou do fim) até caber, deixando pelo menos ``keep``."""
        pieces, size, removed = self.pieces[name], self.size(), 0
        while size > MAX_BYTES and len(pieces) > keep:
            piece = pieces.pop() if from_end else pieces.popleft()
            size -= len(piece) + (1 if pieces else 0)
            removed += 1
        return removed


def _reduce(parts: _Parts) -> None:
    """Reduz até caber em 8 MiB, na ordem: letras, ``played``, ``queue_autoplay`` e ``failed_tracks``, cauda da ``queue``."""
    steps = (
        ("lyrics", parts.without_lyrics),
        ("played", lambda: parts.trim("played", from_end=False)),
        ("queue_autoplay", lambda: parts.trim("queue_autoplay", from_end=True)),
        ("failed_tracks", lambda: parts.trim("failed_tracks", from_end=True)),
        ("queue", lambda: parts.trim("queue", from_end=True, keep=1)),
    )
    done = []
    for label, step in steps:
        if parts.size() <= MAX_BYTES:
            break
        if removed := step():
            done.append(f"{label}: {removed}")
    if done:
        logger.warning("Sessão reduzida para caber em 8 MiB (%s)", "; ".join(done))
    if parts.size() > MAX_BYTES:
        raise SchemaError("", TOO_BIG)


def encode_session(data: dict) -> bytes:
    """O JSON da sessão (UTF-8 compacto, até 8 MiB) ou ``SchemaError``; só recusa por tamanho depois de reduzir."""
    parts = _Parts(validate_session(data))
    _reduce(parts)
    return parts.assemble()
