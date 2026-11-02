"""O catálogo FIXO de gêneros das estações personalizadas (item 8, R5, contrato §1): validação pura de um dicionário (o arquivo é ``utils/radio/data/genres.json``).

Cada gênero tem um ``id`` estável, um ``group`` (``international`` ou ``brazilian``, os dois níveis do seletor) e UMA receita:

- **tags** (1 a 3 tags do Last.fm, unidas): os gêneros internacionais, por ``tag.getTopTracks``;
- **artists** (12 a 20 artistas curados): os brasileiros, por ``artist.getTopTracks``, porque a tag é social e global e falha em gênero regional.

O nome mostrado vem do i18n (``radio.genre.<id>``) e o do grupo de ``radio.genre.group.<group>``. Cada grupo cabe num select (até 24 gêneros). Nada de gênero
livre: o ``id`` de uma estação só vale se estiver aqui. Nenhum Spotify nem YouTube Music: só o Last.fm (contrato §1).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any, NamedTuple

from empiremuse.rules.errors import RuleError

CATALOG_INVALID = "radio_catalog_invalid"
SCHEMA_VERSION = 1
GROUPS = ("international", "brazilian")
MAX_PER_GROUP = 24
MAX_TAGS = 3
MIN_ARTISTS, MAX_ARTISTS = 12, 20
NAME_LIMIT = 64

_ID = re.compile(r"[a-z][a-z0-9_]{1,23}")


class Genre(NamedTuple):
    id: str
    group: str
    tags: tuple[str, ...] = ()
    artists: tuple[str, ...] = ()


def _names(raw: object, low: int, high: int) -> tuple[str, ...] | None:
    """Uma lista de nomes (1 a 64 caracteres, sem controle, sem repetir ignorando a caixa) com entre ``low`` e ``high`` itens."""
    if not isinstance(raw, list) or not low <= len(raw) <= high:
        return None
    found: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            return None
        name = item.strip()
        if not 1 <= len(name) <= NAME_LIMIT or any(ord(c) < 32 or ord(c) == 127 for c in name):
            return None
        if name.casefold() in {n.casefold() for n in found}:
            return None
        found.append(name)
    return tuple(found)


def check_catalog(raw: object) -> tuple[Genre, ...] | RuleError:
    """O catálogo validado, na ordem do arquivo; o primeiro defeito vira ``RuleError`` (``key`` = o id do gênero ou ``catalog``)."""
    if (
        not isinstance(raw, Mapping)
        or raw.get("version") != SCHEMA_VERSION
        or list(raw.get("groups", ())) != list(GROUPS)
    ):
        return RuleError(CATALOG_INVALID, "catalog")
    items = raw.get("genres")
    if not isinstance(items, list):
        return RuleError(CATALOG_INVALID, "catalog")
    genres: list[Genre] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            return RuleError(CATALOG_INVALID, "catalog")
        gid, group = item.get("id"), item.get("group")
        if not isinstance(gid, str) or not _ID.fullmatch(gid) or gid in seen or group not in GROUPS:
            return RuleError(CATALOG_INVALID, str(gid)[:24])
        tags = _names(item["tags"], 1, MAX_TAGS) if "tags" in item else ()
        artists = _names(item["artists"], MIN_ARTISTS, MAX_ARTISTS) if "artists" in item else ()
        if tags is None or artists is None or bool(tags) == bool(artists):  # exatamente UMA receita
            return RuleError(CATALOG_INVALID, gid)
        seen.add(gid)
        genres.append(Genre(gid, group, tags, artists))
    for group in GROUPS:
        if not 1 <= sum(1 for g in genres if g.group == group) <= MAX_PER_GROUP:
            return RuleError(CATALOG_INVALID, group)
    return tuple(genres)


def genre_groups(catalog: tuple[Genre, ...]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """``(grupo, (ids...))`` na ordem do seletor: primeiro ``international``, depois ``brazilian``."""
    return tuple((group, tuple(g.id for g in catalog if g.group == group)) for group in GROUPS)


def find_genre(catalog: tuple[Genre, ...], genre_id: object) -> Genre | None:
    return next((g for g in catalog if g.id == genre_id), None) if isinstance(genre_id, str) else None


def recipe(genre: Genre) -> dict[str, Any]:
    """A receita no formato que o ``lastfm_feed`` usa: ``{"tags": [...]}`` ou ``{"artists": [...]}``."""
    return {"tags": list(genre.tags)} if genre.tags else {"artists": list(genre.artists)}


CATALOG_FILE = Path(__file__).resolve().parent.parent / "utils" / "radio" / "data" / "genres.json"


@cache
def shipped_catalog() -> tuple[Genre, ...]:
    """O catálogo que vai no pacote, lido e validado uma vez por processo. Arquivo ausente ou inválido é erro de empacotamento: levanta ``ValueError``."""
    checked = check_catalog(json.loads(CATALOG_FILE.read_text(encoding="utf-8")))
    if isinstance(checked, RuleError):
        raise ValueError(f"catálogo de gêneros inválido ({checked.key})")
    return checked
