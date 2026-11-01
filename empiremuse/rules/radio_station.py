"""Regras puras das ESTAÇÕES do servidor para a rádio personalizada (item 8, R5, contrato §2): o documento, a validação e a ordem.

Uma estação tem nome (único no servidor, sem diferenciar a caixa) e UMA fonte: um gênero do catálogo, uma lista de artistas ou playlists do servidor.
A mesma validação serve ao chat e ao painel web (``check_station_doc``). Aqui não há banco nem Discord; o que depende deles (nomes já usados, quantas
estações o servidor tem, quais playlists existem) chega pronto em ``StationContext``. Nada aqui monta texto para o usuário (``RuleError.code`` estável).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from empiremuse.rules.errors import RuleError
from empiremuse.rules.radio_genres import Genre, find_genre, shipped_catalog
from empiremuse.rules.text import clean_title

KIND_GENRE = "genre"
KIND_ARTISTS = "artists"
KIND_PLAYLISTS = "playlists"
STATION_KINDS = (KIND_GENRE, KIND_ARTISTS, KIND_PLAYLISTS)

NAME_MAX = 32
MAX_STATIONS_PER_GUILD = 10
MAX_ARTISTS = 10
MAX_PLAYLISTS = 5
ARTIST_NAME_MAX = 64
POOL_MAX = 300  # faixas por estação
POOL_PER_ARTIST_MAX = 30

STATION_ID_INVALID = "station_id_invalid"
STATION_NAME_INVALID = "station_name_invalid"
STATION_NAME_TAKEN = "station_name_taken"
STATION_KIND_INVALID = "station_kind_invalid"
STATION_SOURCE_INVALID = "station_source_invalid"
STATION_GENRE_UNKNOWN = "station_genre_unknown"
STATION_PLAYLIST_MISSING = "station_playlist_missing"
STATION_LIMIT = "station_limit"
STATION_REV_INVALID = "station_rev_invalid"

# ``RuleError.key`` dos erros de quem cria ou edita: o campo do formulário.
FIELD_NAME, FIELD_KIND, FIELD_GENRE, FIELD_ARTISTS, FIELD_PLAYLISTS, FIELD_LIMIT = (
    "name",
    "kind",
    "genre",
    "artists",
    "playlists",
    "limit",
)

_STATION_ID = re.compile(r"[0-9a-f]{24}")
_SNOWFLAKE = re.compile(r"[0-9]{1,20}")


@dataclass(frozen=True)
class StationDoc:
    """O que o usuário pediu, já validado: nome limpo, a chave de unicidade, o tipo e a fonte. Sem id nem servidor (quem grava dá)."""

    name: str
    name_norm: str
    kind: str
    source: dict[str, Any]


@dataclass(frozen=True)
class CustomStation:
    """Uma estação do servidor como está gravada. ``id`` = 24 hex; ``unknown`` = artistas que o Last.fm não conhece (⚠️ nas duas telas)."""

    id: str
    guild_id: int
    name: str
    kind: str
    source: dict[str, Any]
    rev: int = 0
    updated_by: str = ""
    updated_at: float = 0.0
    unknown: tuple[str, ...] = ()

    @property
    def name_norm(self) -> str:
        return normalize_name(self.name)


def normalize_name(name: str) -> str:
    """A chave de unicidade: NFKC, espaços colapsados e caixa dobrada."""
    return " ".join(unicodedata.normalize("NFKC", name).split()).casefold()


def valid_station_id(value: object) -> bool:
    return isinstance(value, str) and _STATION_ID.fullmatch(value) is not None


def _names(raw: object, high: int, key: str) -> list[str] | RuleError:
    """Uma lista de 1 a ``high`` nomes de 1 a 64 caracteres; repetido (sem diferenciar a caixa) some em silêncio."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= high:
        return RuleError(STATION_SOURCE_INVALID, key, limit=high)
    found: list[str] = []
    for item in raw:
        name = clean_title(item, 1, ARTIST_NAME_MAX)
        if name is None:
            return RuleError(STATION_SOURCE_INVALID, key, limit=ARTIST_NAME_MAX)
        if normalize_name(name) not in {normalize_name(n) for n in found}:
            found.append(name)
    return found


def check_source(
    kind: str, raw: object, *, playlists: Collection[str], catalog: tuple[Genre, ...]
) -> dict[str, Any] | RuleError:
    """A fonte da estação: um dicionário com a UMA chave igual ao tipo; duas fontes, ou outra chave, é erro."""
    if not isinstance(raw, Mapping) or set(raw) != {kind}:
        return RuleError(STATION_SOURCE_INVALID, FIELD_KIND)
    value = raw[kind]
    if kind == KIND_GENRE:
        if find_genre(catalog, value) is None:
            return RuleError(STATION_GENRE_UNKNOWN, FIELD_GENRE)
        return {"genre": value}
    if kind == KIND_ARTISTS:
        names = _names(value, MAX_ARTISTS, FIELD_ARTISTS)
        return names if isinstance(names, RuleError) else {"artists": names}
    names = _names(value, MAX_PLAYLISTS, FIELD_PLAYLISTS)
    if isinstance(names, RuleError):
        return names
    known = set(playlists)
    for name in names:
        if name not in known:
            return RuleError(STATION_PLAYLIST_MISSING, FIELD_PLAYLISTS, name)
    return {"playlists": names}


def check_station_doc(
    raw: object,
    *,
    playlists: Collection[str] = (),
    taken: Collection[str] = (),
    count: int = 0,
    catalog: tuple[Genre, ...] | None = None,
) -> StationDoc | RuleError:
    """O pedido (``name``, ``kind``, ``source``) de criar ou editar uma estação; ``RuleError(code, campo)`` no primeiro defeito.

    ``playlists`` = nomes das playlists do servidor; ``taken`` = ``name_norm`` das OUTRAS estações do servidor; ``count`` = quantas estações o
    servidor tem SEM a que está sendo editada (``MAX_STATIONS_PER_GUILD`` ou mais recusa a criação). Campo desconhecido é ignorado.
    """
    if not isinstance(raw, Mapping):
        return RuleError(STATION_NAME_INVALID, FIELD_NAME, limit=NAME_MAX)
    name = clean_title(raw.get("name"), 1, NAME_MAX)
    if name is None:
        return RuleError(STATION_NAME_INVALID, FIELD_NAME, limit=NAME_MAX)
    name = " ".join(name.split())
    name_norm = normalize_name(name)
    if name_norm in set(taken):
        return RuleError(STATION_NAME_TAKEN, FIELD_NAME, name)
    if count >= MAX_STATIONS_PER_GUILD:
        return RuleError(STATION_LIMIT, FIELD_LIMIT, limit=MAX_STATIONS_PER_GUILD)
    kind = raw.get("kind")
    if kind not in STATION_KINDS:
        return RuleError(STATION_KIND_INVALID, FIELD_KIND)
    source = check_source(kind, raw.get("source"), playlists=playlists, catalog=catalog or shipped_catalog())
    if isinstance(source, RuleError):
        return source
    return StationDoc(name=name, name_norm=name_norm, kind=kind, source=source)


def _guild(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and _SNOWFLAKE.fullmatch(value) and int(value) > 0:
        return int(value)
    return None


def from_document(raw: object) -> CustomStation | RuleError:
    """Um documento do banco já gravado, relido: forma e tipos (a validação de playlist e gênero é de quando gravou). Documento ruim é ``RuleError``."""
    if not isinstance(raw, Mapping):
        return RuleError(STATION_ID_INVALID, "id")
    station_id, guild_id = raw.get("_id"), _guild(raw.get("guild_id"))
    if not isinstance(station_id, str) or not valid_station_id(station_id) or guild_id is None:
        return RuleError(STATION_ID_INVALID, "id")
    name = clean_title(raw.get("name"), 1, NAME_MAX)
    kind, source, rev = raw.get("kind"), raw.get("source"), raw.get("rev", 0)
    if name is None or kind not in STATION_KINDS or not isinstance(source, Mapping) or set(source) != {kind}:
        return RuleError(STATION_SOURCE_INVALID, "source")
    if isinstance(rev, bool) or not isinstance(rev, int) or rev < 0:
        return RuleError(STATION_REV_INVALID, "rev")
    unknown = raw.get("unknown")
    return CustomStation(
        id=station_id,
        guild_id=guild_id,
        name=name,
        kind=kind,
        source=dict(source),
        rev=rev,
        updated_by=str(raw.get("updated_by", ""))[:40],
        updated_at=float(raw["updated_at"]) if isinstance(raw.get("updated_at"), int | float) else 0.0,
        unknown=tuple(u for u in unknown if isinstance(u, str))[:MAX_ARTISTS] if isinstance(unknown, list) else (),
    )


def to_document(station: CustomStation) -> dict[str, Any]:
    """O documento do banco (ids como texto)."""
    return {
        "_id": station.id,
        "guild_id": str(station.guild_id),
        "name": station.name,
        "name_norm": station.name_norm,
        "kind": station.kind,
        "source": dict(station.source),
        "rev": station.rev,
        "updated_by": str(station.updated_by)[:40],
        "updated_at": station.updated_at,
        "unknown": list(station.unknown),
    }
