"""Estações do Radio Browser e o fluxo delas (item 8, R3, R-7 e R-8): tudo que vem de fora é hostil até provar o contrário.

O ``add`` do Radio Browser é público e sem conta, então estação, DNS, corpo, redirecionamento e ``StreamTitle`` são controlados por terceiros.
Aqui só há decisões puras: a estação aceita (``check_station``), a URL do fluxo, o tipo de conteúdo, a assinatura dos primeiros bytes de
acordo com o codec, o parser de playlist (que o relay resolve, para o Lavalink NUNCA receber playlist) e o termo de busca. Rede e
endereços ficam em ``utils/radio`` (``is_public_address`` e o resolvedor fixado).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from empiremuse.rules.errors import RuleError
from empiremuse.rules.text import external_text
from empiremuse.rules.urls import is_clean_url

# Códigos de erro (estáveis).
STATION_INVALID = "station_invalid"
STATION_UUID = "station_uuid"
STATION_URL = "station_url"
STATION_CODEC = "station_codec"
STATION_HLS = "station_hls"
STATION_BITRATE = "station_bitrate"
SEARCH_TERM = "station_search_term"

MIRROR_PATTERN = re.compile(r"[a-z0-9-]+\.api\.radio-browser\.info")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

SEARCH_MIN, SEARCH_MAX = 2, 64
NAME_LIMIT = 100
COUNTRY_LIMIT = 40
MAX_BITRATE = 320
URL_LIMIT = 2048

ALLOWED_PORTS = frozenset({80, 443}) | frozenset(range(8000, 9000))
CODECS = {
    "MP3": "mp3",
    "AAC": "aac",
    "AAC+": "aac",
    "OGG": "ogg",
}  # o que o Radio Browser escreve -> a família da assinatura
ALLOWED_CONTENT_TYPES = frozenset({"audio/mpeg", "audio/aac", "audio/aacp", "audio/ogg", "application/ogg"})
PLAYLIST_EXTENSIONS = (".pls", ".m3u")
PLAYLIST_CONTENT_TYPES = frozenset({"audio/x-scpls", "application/pls+xml", "audio/x-mpegurl", "audio/mpegurl"})
PLAYLIST_MAX_BYTES = 16 * 1024
PLAYLIST_MAX_ENTRIES = 10


class Station(NamedTuple):
    """Uma estação aceita; os textos já passaram pela função única da R-11. A ``homepage`` nunca é guardada; o ``favicon`` só se for ``https`` e limpo."""

    uuid: str
    name: str
    url_resolved: str
    codec: str
    bitrate: int
    country: str
    favicon: str = ""


def valid_mirror(name: object) -> bool:
    """O nome usado no TLS de um espelho: só ``<algo>.api.radio-browser.info`` (o resto, inclusive o PTR de um IP, é descartado)."""
    return isinstance(name, str) and name.isascii() and MIRROR_PATTERN.fullmatch(name) is not None


def valid_uuid(value: object) -> bool:
    return isinstance(value, str) and _UUID.fullmatch(value) is not None


def check_search_term(raw: object) -> str | RuleError:
    """O termo da busca: 2 a 64 caracteres, sem controle nem quebra de linha (a URL é montada pelo ``yarl``, nunca por concatenação)."""
    if not isinstance(raw, str):
        return RuleError(SEARCH_TERM, "term")
    term = raw.strip()
    if not SEARCH_MIN <= len(term) <= SEARCH_MAX or any(ord(c) < 32 or ord(c) == 127 for c in term):
        return RuleError(SEARCH_TERM, "term", limit=SEARCH_MAX)
    return term


def check_stream_url(raw: object) -> str | None:
    """A URL de um fluxo: ``http``/``https``, sem credencial, com host, porta 80, 443 ou 8000-8999. Devolve a URL limpa ou ``None``.

    Não resolve DNS: o endereço público (``is_public_address``) é conferido na hora de conectar, com o IP fixado.
    """
    if not isinstance(raw, str) or not raw or len(raw) > URL_LIMIT or any(ord(c) <= 32 or ord(c) == 127 for c in raw):
        return None
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        return None
    effective = port if port is not None else (443 if parts.scheme == "https" else 80)
    if effective not in ALLOWED_PORTS:
        return None
    return raw


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdecimal():
        return int(value.strip())
    return None


def check_station(raw: object) -> Station | RuleError:
    """A estação do Radio Browser aceita, ou o primeiro defeito: ``uuid`` UUID, ``url_resolved`` http(s) na porta certa, codec MP3/AAC/AAC+/OGG,
    ``hls == 0`` e ``bitrate`` até 320. Campo desconhecido é ignorado; a ``homepage`` não entra e o ``favicon`` só como ``https`` limpo."""
    if not isinstance(raw, Mapping):
        return RuleError(STATION_INVALID)
    uuid = raw.get("stationuuid", raw.get("uuid"))
    if not valid_uuid(uuid):
        return RuleError(STATION_UUID, "uuid")
    url = check_stream_url(raw.get("url_resolved", raw.get("url")))
    if url is None:
        return RuleError(STATION_URL, "url")
    codec = raw.get("codec")
    if not isinstance(codec, str) or codec.strip().upper() not in CODECS:
        return RuleError(STATION_CODEC, "codec")
    hls = _int(raw.get("hls", 0))
    if hls != 0:
        return RuleError(STATION_HLS, "hls")
    bitrate = _int(raw.get("bitrate", 0))
    if bitrate is None or not 0 <= bitrate <= MAX_BITRATE:
        return RuleError(STATION_BITRATE, "bitrate", limit=MAX_BITRATE)
    name = external_text(raw.get("name"), NAME_LIMIT) or "?"
    country = external_text(raw.get("country"), COUNTRY_LIMIT)
    return Station(
        uuid=uuid,  # type: ignore[arg-type]  # valid_uuid já garantiu str
        name=name,
        url_resolved=url,
        codec=codec.strip().upper(),
        bitrate=bitrate,
        country=country,
        favicon=favicon_thumbnail(raw.get("favicon")) or "",
    )


def favicon_thumbnail(raw: object) -> str | None:
    """A ``favicon`` vira thumbnail só se for ``https`` e passar no ``is_clean_url``; a ``homepage`` nunca aparece."""
    if not isinstance(raw, str) or not raw.startswith("https://"):
        return None
    return raw if is_clean_url(raw) else None


# ---------------------------------------------------------------------- o fluxo (R-8)


def content_type_ok(header: object) -> bool:
    """O ``Content-Type`` é um dos de áudio aceitos? (Só o tipo: parâmetros como ``charset`` são ignorados.)"""
    if not isinstance(header, str):
        return False
    return header.split(";", 1)[0].strip().lower() in ALLOWED_CONTENT_TYPES


def signature_ok(codec: object, first_bytes: bytes) -> bool:
    """Os primeiros bytes combinam com o codec? MP3: sincronismo MPEG (``0xFFE``); AAC/AAC+: ADTS (``0xFFF``); OGG: ``OggS``.

    Qualquer outra coisa (PLS, M3U, XSPF, HTML, texto) encerra antes do primeiro byte ser repassado.
    """
    if not isinstance(codec, str) or len(first_bytes) < 2:
        return False
    family = CODECS.get(codec.strip().upper())
    if family == "mp3":
        return first_bytes[0] == 0xFF and (first_bytes[1] & 0xE0) == 0xE0
    if family == "aac":
        return first_bytes[0] == 0xFF and (first_bytes[1] & 0xF0) == 0xF0
    if family == "ogg":
        return first_bytes[:4] == b"OggS"
    return False


def looks_like_playlist(url: str, content_type: object = None) -> bool:
    """A URL (ou o tipo) é de playlist ``.pls``/``.m3u``? O relay a resolve; o Lavalink nunca recebe playlist. ``.m3u8`` é HLS (fora)."""
    path = urlsplit(url).path.lower()
    if path.endswith(PLAYLIST_EXTENSIONS):
        return True
    return isinstance(content_type, str) and content_type.split(";", 1)[0].strip().lower() in PLAYLIST_CONTENT_TYPES


def parse_playlist(body: bytes) -> list[str]:
    """As URLs de fluxo de uma playlist ``.pls`` (``FileN=``) ou ``.m3u``: até 16 KiB lidos e 10 entradas. Cada uma ainda tem de passar por
    ``check_stream_url`` e ``is_public_address`` (o relay faz). Entrada com aparência de HLS (``.m3u8``) é descartada."""
    text = body[:PLAYLIST_MAX_BYTES].decode("utf-8", errors="replace")
    found: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(("[", ";")):
            continue
        candidate = line.split("=", 1)[1].strip() if re.match(r"(?i)file\d+\s*=", line) else line
        if candidate.lower().split("?", 1)[0].endswith(".m3u8"):
            continue
        if check_stream_url(candidate) is not None:
            found.append(candidate)
        if len(found) >= PLAYLIST_MAX_ENTRIES:
            break
    return found


def safe_station_url_for_log(url: str) -> str:
    """O que pode ir ao log (R-12): só o host. A URL inteira pode ter token de ouvinte."""
    try:
        host = urlsplit(url).hostname or "?"
    except ValueError:
        host = "?"
    return host.lower()[:100]
