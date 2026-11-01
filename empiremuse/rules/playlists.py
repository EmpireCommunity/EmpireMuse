"""Regras puras das playlists do servidor (item 17): item, validação, arquivo de importação, importação só-acrescenta e relatório.

As playlists são uma lista por servidor (seção ``playlists`` do documento único), a mesma para todos os bots. Aqui só há dados e
decisões; o banco, a trava e os efeitos são do ``utils/server_playlists``. Texto de terceiros nunca vira markdown sem escape.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from empiremuse.rules.errors import (
    DESCRIPTION_INVALID,
    DESCRIPTION_TOO_LONG,
    FILE_FORMAT,
    FILE_TOO_LARGE,
    FILE_TOO_MANY,
    GENRES_INVALID,
    IMPORT_URL_REFUSED,
    ITEM_FORMAT,
    NAME_INVALID,
    NAME_TAKEN,
    NAME_TOO_LONG,
    ORDER_INVALID,
    PLAYLIST_NOT_FOUND,
    URL_INVALID,
    URL_TAKEN,
    RuleError,
)
from empiremuse.rules.errors import QUOTA_FULL as QUOTA_FULL_CODE
from empiremuse.rules.limits import (
    MAX_SERVER_PLAYLISTS,
    PLAYLIST_DESCRIPTION_MAX,
    PLAYLIST_DESCRIPTION_MIN,
    PLAYLIST_FILE_MAX_BYTES,
    PLAYLIST_FILE_MAX_ITEMS,
    PLAYLIST_GENRE_MAX,
    PLAYLIST_GENRE_MIN,
    PLAYLIST_GENRES_MAX,
    PLAYLIST_NAME_MAX,
    PLAYLIST_NAME_MIN,
    PLAYLIST_ORDER_MAX,
    PLAYLIST_PASTE_MAX,
    PLAYLIST_REPORT_MAX,
    PLAYLIST_SELECT_DESCRIPTION_MAX,
    PLAYLIST_URL_MAX,
    PLAYLIST_URL_MIN,
)
from empiremuse.rules.emoji import check_emoji, split_leading_icon
from empiremuse.rules.text import has_control_or_format
from empiremuse.rules.urls import check_url
from empiremuse.rules.usage import escape_markdown, render_text

# Motivos de um item pulado na importação.
INVALID = "invalid"
DUP_NAME = "dup_name"
DUP_URL = "dup_url"
DUP_IN_FILE = "dup_in_file"
QUOTA_FULL = "quota_full"

_TRACKING_PARAMS = frozenset({"si", "feature", "pp"})  # mais todo ``utm_*``
_IMPORT_FILE = re.compile(r"[a-z0-9-]{1,64}\.json")
_ELLIPSIS = "…"

# Importar por URL (P5): só estes hosts, por https na porta 443. Qualquer outro nome, IP literal, ponto final ou ``%`` fica de fora.
IMPORT_URL_HOSTS = frozenset(
    {"raw.githubusercontent.com", "gist.githubusercontent.com", "cdn.discordapp.com", "media.discordapp.net"}
)
IMPORT_URL_MAX = 2000
_HTTPS_PORT = "443"
_NAT64_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")
_SIIT_PREFIX = ipaddress.IPv6Network("::ffff:0:0:0/96")  # RFC 2765, diferente do mapeado ``::ffff:0:0/96``
_GLOBAL_UNICAST_V6 = ipaddress.IPv6Network("2000::/3")
_REASON_TEXT = {
    INVALID: "inválida",
    DUP_NAME: "nome já existe",
    DUP_URL: "link já existe",
    DUP_IN_FILE: "repetida no arquivo",
    QUOTA_FULL: "limite de playlists do servidor",
}
_ITEM_KEYS = frozenset({"order", "name", "description", "genres", "url", "icon"})


class Playlist(NamedTuple):
    order: int
    name: str
    description: str
    genres: tuple[str, ...]
    url: str
    icon: str | None = None


class Skip(NamedTuple):
    name: str
    reason: str


class ImportReport(NamedTuple):
    added: tuple[Playlist, ...]
    skipped: tuple[Skip, ...]


@dataclass(frozen=True)
class ParsedFile:
    items: tuple[Playlist, ...]
    invalid: tuple[Skip, ...]


@dataclass(frozen=True)
class MigrationPlan:
    items: tuple[Playlist, ...]
    skipped: int
    invalid: int


# ---------------------------------------------------------------------- permissão


def may_manage(is_guild_owner: bool, administrator: bool, manage_guild: bool) -> bool:
    """Quem gerencia as playlists: dono do servidor, Administrador ou Gerenciar Servidor (sem atalho do dono do bot)."""
    return bool(is_guild_owner or administrator or manage_guild)


# ---------------------------------------------------------------------- URL


def _is_tracking(key: str) -> bool:
    return key in _TRACKING_PARAMS or key.startswith("utm_")


def clean_stored_url(url: str) -> str:
    """A URL sem parâmetros de rastreio (``si``, ``utm_*``, ``feature``, ``pp``); mantém ``list``, ``drli`` e os demais, na ordem."""
    parts = urlsplit(url)
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(k)])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def normalize_url(url: str) -> str:
    """Forma de comparação: esquema e host minúsculos, sem ``www.``, sem fragmento, sem ``/`` final, sem rastreio, resto ordenado."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(k)))
    return urlunsplit((parts.scheme.lower(), host, parts.path.rstrip("/"), query, ""))


# ---------------------------------------------------------------------- item


# U+200D junta os emojis compostos (👮‍♂️). Só vale entre dois caracteres visíveis; os demais ``Cf`` (U+200B, U+202E…) seguem recusados.
_JOINER_BETWEEN_VISIBLE = re.compile("(?<=[^\\s\u200d])\u200d(?=[^\\s\u200d])")


def _bad_text(text: str) -> bool:
    return has_control_or_format(_JOINER_BETWEEN_VISIBLE.sub("", text)) or "\n" in text or "\r" in text


def check_name(raw: Any) -> str | RuleError:
    if not isinstance(raw, str):
        return RuleError(NAME_INVALID, value=raw)
    name = raw.strip()
    if len(name) > PLAYLIST_NAME_MAX:
        return RuleError(NAME_TOO_LONG, value=name, limit=PLAYLIST_NAME_MAX)
    if len(name) < PLAYLIST_NAME_MIN or _bad_text(name):
        return RuleError(NAME_INVALID, value=name)
    return name


def check_description(raw: Any) -> str | RuleError:
    if not isinstance(raw, str):
        return RuleError(DESCRIPTION_INVALID, value=raw)
    text = raw.strip()
    if len(text) > PLAYLIST_DESCRIPTION_MAX:
        return RuleError(DESCRIPTION_TOO_LONG, value=text, limit=PLAYLIST_DESCRIPTION_MAX)
    if (text and len(text) < PLAYLIST_DESCRIPTION_MIN) or _bad_text(text):
        return RuleError(DESCRIPTION_INVALID, value=text)
    return text


def check_genres(raw: Any) -> tuple[str, ...] | RuleError:
    """Até 8 gêneros de 2 a 32 caracteres, sem repetir (``casefold``) nem controle/quebra de linha."""
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)) or len(raw) > PLAYLIST_GENRES_MAX:
        return RuleError(GENRES_INVALID)
    genres: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            return RuleError(GENRES_INVALID)
        genre = item.strip()
        if not PLAYLIST_GENRE_MIN <= len(genre) <= PLAYLIST_GENRE_MAX or _bad_text(genre):
            return RuleError(GENRES_INVALID, value=genre)
        if genre.casefold() in {g.casefold() for g in genres}:
            return RuleError(GENRES_INVALID, value=genre)
        genres.append(genre)
    return tuple(genres)


def check_url_field(raw: Any) -> str | RuleError:
    """URL pura de 10 a 250 caracteres, gravada sem os parâmetros de rastreio."""
    url = check_url(raw, PLAYLIST_URL_MAX)
    if isinstance(url, RuleError):
        return url
    if len(url) < PLAYLIST_URL_MIN:
        return RuleError(URL_INVALID, value=url)
    cleaned = clean_stored_url(url)
    return cleaned if len(cleaned) >= PLAYLIST_URL_MIN else RuleError(URL_INVALID, value=url)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def check_playlist(raw: Any, *, default_order: int = 0) -> Playlist | RuleError:
    """Um item (dicionário) validado e aparado. ``icon`` é UM emoji Unicode (``check_emoji``); qualquer outra coisa é descartada (``None``), nunca erro."""
    if not isinstance(raw, Mapping):
        return RuleError(ITEM_FORMAT)
    name = check_name(raw.get("name"))
    if isinstance(name, RuleError):
        return name
    description = check_description(raw.get("description", ""))
    if isinstance(description, RuleError):
        return RuleError(description.code, key=name, value=description.value, limit=description.limit)
    genres = check_genres(raw.get("genres"))
    if isinstance(genres, RuleError):
        return RuleError(genres.code, key=name, value=genres.value)
    url = check_url_field(raw.get("url"))
    if isinstance(url, RuleError):
        return RuleError(url.code, key=name, value=url.value, limit=url.limit)
    order = raw.get("order", default_order)
    if not _is_int(order) or not (0 <= order <= PLAYLIST_ORDER_MAX):
        return RuleError(ORDER_INVALID, key=name, value=order)
    return Playlist(order, name, description, genres, url, check_emoji(raw.get("icon")))


# ---------------------------------------------------------------------- lista


def sorted_by_order(playlists: Iterable[Playlist]) -> tuple[Playlist, ...]:
    return tuple(sorted(playlists, key=lambda p: (p.order, p.name.casefold())))


def renumber(playlists: Iterable[Playlist]) -> tuple[Playlist, ...]:
    """``order`` de 1 a n na ordem em que vieram."""
    return tuple(p._replace(order=position) for position, p in enumerate(playlists, start=1))


def find_index(playlists: Sequence[Playlist], name: str) -> int:
    wanted = name.casefold()
    return next((i for i, p in enumerate(playlists) if p.name.casefold() == wanted), -1)


def find_item(playlists: Sequence[Playlist], name: str) -> Playlist | None:
    index = find_index(playlists, name)
    return playlists[index] if index >= 0 else None


def find_selected(playlists: Sequence[Playlist], value: str) -> Playlist | None:
    """A playlist que um select escolheu: pelo nome exato (``casefold``) e, se nada casar, pelo nome sem o emoji do início.

    O valor de uma opção fica gravado na mensagem do Discord. Uma mensagem montada antes da migração dos ícones traz o nome antigo
    (``🔴 Red Line``), e hoje o item se chama ``Red Line`` com o emoji no ``icon``.
    """
    found = find_item(playlists, value)
    if found is not None:
        return found
    icon, bare = split_leading_icon(value)
    return find_item(playlists, bare) if icon else None


def _clash(playlists: Sequence[Playlist], item: Playlist, *, ignore: int = -1) -> RuleError | None:
    url = normalize_url(item.url)
    for index, other in enumerate(playlists):
        if index == ignore:
            continue
        if other.name.casefold() == item.name.casefold():
            return RuleError(NAME_TAKEN, key=other.name)
        if normalize_url(other.url) == url:
            return RuleError(URL_TAKEN, key=other.name)
    return None


def add_item(
    existing: Sequence[Playlist], item: Playlist, quota: int = MAX_SERVER_PLAYLISTS
) -> tuple[Playlist, ...] | RuleError:
    """A lista com ``item`` na posição ``item.order`` (0 ou fora do intervalo = no fim), renumerada. Cota e duplicata recusam."""
    if len(existing) >= quota:
        return RuleError(QUOTA_FULL_CODE, limit=quota)
    if clash := _clash(existing, item):
        return clash
    ordered = list(sorted_by_order(existing))
    position = item.order - 1 if 1 <= item.order <= len(ordered) else len(ordered)
    ordered.insert(position, item)
    return renumber(ordered)


def edit_item(existing: Sequence[Playlist], name: str, item: Playlist) -> tuple[Playlist, ...] | RuleError:
    """Troca a playlist ``name`` por ``item`` (pode renomear, sem colidir com outra) e a reposiciona por ``item.order``.

    O ``icon`` é o de ``item`` (``None`` apaga o ícone): quem edita sempre envia o campo.
    """
    ordered = list(sorted_by_order(existing))
    index = find_index(ordered, name)
    if index < 0:
        return RuleError(PLAYLIST_NOT_FOUND, key=name)
    if clash := _clash(ordered, item, ignore=index):
        return clash
    del ordered[index]
    position = item.order - 1 if 1 <= item.order <= len(ordered) + 1 else index
    ordered.insert(position, item)
    return renumber(ordered)


def remove_item(existing: Sequence[Playlist], name: str) -> tuple[Playlist, ...] | RuleError:
    ordered = list(sorted_by_order(existing))
    index = find_index(ordered, name)
    if index < 0:
        return RuleError(PLAYLIST_NOT_FOUND, key=name)
    del ordered[index]
    return renumber(ordered)


# ---------------------------------------------------------------------- origens da importação


def check_import_filename(name: Any, listed: Collection[str]) -> str | RuleError:
    """Nome de um arquivo do bot (``data/playlists``): ``[a-z0-9-]{1,64}.json`` E presente na lista dos arquivos que existem.

    Nada de ``/``, ``..`` ou caminho: o valor precisa ser um dos nomes da lista, nunca montado a partir do que foi digitado.
    """
    if not isinstance(name, str) or not _IMPORT_FILE.fullmatch(name) or name not in listed:
        return RuleError(NAME_INVALID, value=name if isinstance(name, str) else None)
    return name


def check_attachment(filename: Any, size: Any) -> RuleError | None:
    """Anexo do Discord: ``.json`` e no máximo 64 KiB, conferido pelo ``size`` informado ANTES de baixar (``None`` = pode ler)."""
    if not isinstance(filename, str) or not filename.lower().endswith(".json"):
        return RuleError(FILE_FORMAT)
    if not _is_int(size) or not 0 <= size <= PLAYLIST_FILE_MAX_BYTES:
        return RuleError(FILE_TOO_LARGE, limit=PLAYLIST_FILE_MAX_BYTES)
    return None


def check_import_url(raw: Any) -> str | RuleError:
    """Link de importação: ``https``, host EXATO da lista, sem credencial, porta ausente ou 443, sem IP literal nem barra invertida.

    Devolve a URL normalizada (host em minúsculas, sem porta e sem ``#``) para ser enviada; ``RuleError(IMPORT_URL_REFUSED)`` no resto.
    O texto da recusa nunca repete o link: ele pode trazer ``?token=`` e vale como credencial.
    """
    refused = RuleError(IMPORT_URL_REFUSED)
    if not isinstance(raw, str):
        return refused
    text = raw.strip()
    if (
        len(text) >= 2 and text.startswith("<") and text.endswith(">")
    ):  # ``<link>``: o Discord usa para esconder a prévia
        text = text[1:-1].strip()
    if not text or len(text) > IMPORT_URL_MAX or not text.isascii() or not text.isprintable() or " " in text:
        return refused
    if "\\" in text:
        return refused
    try:
        parts = urlsplit(text)
    except ValueError:
        return refused
    netloc = parts.netloc
    if parts.scheme != "https" or not netloc or "@" in netloc or "[" in netloc or not parts.path.startswith("/"):
        return refused
    host, separator, port = netloc.partition(":")
    if separator and port != _HTTPS_PORT:
        return refused
    host = host.lower()
    if host not in IMPORT_URL_HOSTS:
        return refused
    return urlunsplit(("https", host, parts.path, parts.query, ""))


def _embedded_ipv4(address: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    """Os IPv4 escondidos num IPv6 (mapeado, 6to4, Teredo, NAT64 e o antigo compatível): cada um também precisa ser público."""
    found: list[ipaddress.IPv4Address] = []
    if address.ipv4_mapped is not None:
        found.append(address.ipv4_mapped)
    if address.sixtofour is not None:
        found.append(address.sixtofour)
    if address.teredo is not None:
        found.extend(address.teredo)
    if address in _NAT64_PREFIX or address in _SIIT_PREFIX or int(address) < 2**32:
        found.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    return found


def _is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if not address.is_global or address.is_multicast:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(address)
        if embedded:
            return all(_is_public(inner) for inner in embedded)
        return (
            address in _GLOBAL_UNICAST_V6
        )  # sem IPv4 dentro: só endereço unicast global (nada de site-local, SRv6, etc.)
    return True


def is_public_address(text: Any) -> bool:
    """O endereço é de um servidor público? Recusa privado, loopback, link-local, reservado, multicast e não especificado, inclusive
    quando o IPv4 vem dentro de um IPv6 (mapeado, 6to4, Teredo, NAT64). Texto que não é IP, ou com escopo (``%``), não vale."""
    if not isinstance(text, str) or "%" in text:
        return False
    try:
        return _is_public(ipaddress.ip_address(text.strip()))
    except ValueError:
        return False


def check_paste(text: Any) -> str | RuleError:
    """Texto colado no modal: até 4.000 caracteres (conferido de novo no servidor, o limite do modal não basta)."""
    if not isinstance(text, str) or not text.strip():
        return RuleError(FILE_FORMAT)
    if len(text) > PLAYLIST_PASTE_MAX:
        return RuleError(FILE_TOO_LARGE, limit=PLAYLIST_PASTE_MAX)
    return text


# ---------------------------------------------------------------------- arquivo


def _no_constant(name: str) -> Any:
    raise ValueError(name)  # NaN, Infinity e -Infinity são recusados


def _decode(raw: str | bytes, max_bytes: int) -> tuple[Any, list[str]] | RuleError:
    try:
        data = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    except UnicodeEncodeError:  # surrogate solto no texto colado
        return RuleError(FILE_FORMAT)
    if len(data) > max_bytes:  # o limite vale ANTES de decodificar
        return RuleError(FILE_TOO_LARGE, limit=max_bytes)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return RuleError(FILE_FORMAT)
    repeated: list[str] = []

    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                repeated.append(key)
            else:
                result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=hook, parse_constant=_no_constant), repeated
    except ValueError, RecursionError:
        return RuleError(FILE_FORMAT)


def parse_file(raw: str | bytes, *, max_bytes: int) -> ParsedFile | RuleError:
    """Lê um arquivo de playlists, no formato novo ``{"playlists": [...]}`` ou no antigo ``{nome: {url, description}}``.

    Estrutura ruim, tamanho ou itens demais: erro e nada é gravado. Item ruim vai para ``invalid`` com o motivo; nome repetido
    no formato antigo vira ``DUP_IN_FILE``; chaves desconhecidas são ignoradas; ``icon`` inválido é descartado.
    """
    decoded = _decode(raw, max_bytes)
    if isinstance(decoded, RuleError):
        return decoded
    document, repeated = decoded
    if not isinstance(document, dict):
        return RuleError(FILE_FORMAT)

    invalid: list[Skip] = []
    new_format = isinstance(document.get("playlists"), list)
    if new_format:
        if repeated:
            return RuleError(FILE_FORMAT)  # chave repetida dentro de um item
        entries: list[tuple[str, Any]] = [("", item) for item in document["playlists"]]
    else:
        if any(key in _ITEM_KEYS for key in repeated):
            return RuleError(FILE_FORMAT)
        entries = [
            (str(name), {**value, "name": name} if isinstance(value, dict) else value)
            for name, value in document.items()
        ]
        invalid.extend(Skip(str(key), DUP_IN_FILE) for key in repeated)
    if len(entries) > PLAYLIST_FILE_MAX_ITEMS:
        return RuleError(FILE_TOO_MANY, limit=PLAYLIST_FILE_MAX_ITEMS)

    items: list[Playlist] = []
    for position, (label, entry) in enumerate(entries, start=1):
        checked = check_playlist(entry, default_order=0 if new_format else position)
        if isinstance(checked, RuleError):
            invalid.append(Skip(checked.key or label or f"#{position}", INVALID))
        else:
            items.append(checked)
    return ParsedFile(tuple(items), tuple(invalid))


def to_export(playlists: Iterable[Playlist]) -> dict[str, Any]:
    """O formato novo, na ordem de exibição e com ``icon`` quando houver (exportar e importar no servidor vazio dá a mesma lista)."""
    return {"playlists": [to_doc(p) for p in sorted_by_order(playlists)]}


# ---------------------------------------------------------------------- importação e migração


def plan_import(
    existing: Sequence[Playlist], incoming: Iterable[Playlist], quota: int = MAX_SERVER_PLAYLISTS
) -> ImportReport:
    """Só acrescenta: nunca altera nem remove existente. Pula (com motivo) nome ou link repetido, repetido no arquivo e cota cheia.

    As aceitas entram depois da maior ``order``, na ordem do arquivo.
    """
    names = {p.name.casefold() for p in existing}
    urls = {normalize_url(p.url) for p in existing}
    added: list[Playlist] = []
    skipped: list[Skip] = []
    last = max((p.order for p in existing), default=0)
    seen_names: set[str] = set()
    seen_urls: set[str] = set()
    for item in incoming:
        key, url = item.name.casefold(), normalize_url(item.url)
        if key in names:
            skipped.append(Skip(item.name, DUP_NAME))
        elif url in urls:
            skipped.append(Skip(item.name, DUP_URL))
        elif key in seen_names or url in seen_urls:
            skipped.append(Skip(item.name, DUP_IN_FILE))
        elif len(existing) + len(added) >= quota:
            skipped.append(Skip(item.name, QUOTA_FULL))
        else:
            last += 1
            added.append(item._replace(order=last))
            seen_names.add(key)
            seen_urls.add(url)
    return ImportReport(tuple(added), tuple(skipped))


def plan_migration(legacy: Mapping[str, Any] | None) -> MigrationPlan:
    """Converte o ``fav_links`` antigo ``{nome: {url, description}}`` (na ordem do dicionário). Nome repetido (``casefold``) ou
    link repetido pula; item inválido pula. NÃO obedece à cota (a migração mantém tudo)."""
    if not isinstance(legacy, Mapping):
        return MigrationPlan((), 0, 0)
    items: list[Playlist] = []
    names: set[str] = set()
    urls: set[str] = set()
    skipped = invalid = 0
    for name, value in legacy.items():
        entry = {**value, "name": name} if isinstance(value, Mapping) else None
        checked = check_playlist(entry, default_order=len(items) + 1)
        if isinstance(checked, RuleError):
            invalid += 1
            continue
        key, url = checked.name.casefold(), normalize_url(checked.url)
        if key in names or url in urls:
            skipped += 1
            continue
        names.add(key)
        urls.add(url)
        items.append(checked._replace(order=len(items) + 1))
    return MigrationPlan(tuple(items), skipped, invalid)


# ---------------------------------------------------------------------- gravação (documento)


def to_doc(playlist: Playlist) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "order": playlist.order,
        "name": playlist.name,
        "description": playlist.description,
        "genres": list(playlist.genres),
        "url": playlist.url,
    }
    if playlist.icon:
        doc["icon"] = playlist.icon
    return doc


def from_doc(doc: Any) -> Playlist | None:
    """O item gravado, relido com as mesmas regras (``icon`` gravado que não seja um emoji vira ``None``); ``None`` se o dado gravado estiver ruim."""
    checked = check_playlist(doc)
    return None if isinstance(checked, RuleError) else checked


def move_leading_icons(stored: Sequence[Any]) -> tuple[list[Any], int]:
    """A lista gravada com o emoji do INÍCIO do nome movido para ``icon`` (migração única, idempotente) e quantos itens mudaram.

    Só mexe no item que ainda não tem um ``icon`` válido, cujo nome começa com UM emoji seguido de espaço e que continua com um nome
    válido, sem repetir o de outro item, depois de aparado. O emoji do meio ou do fim do nome fica. O resto da lista passa intacto.
    """
    taken = {
        entry["name"].strip().casefold()
        for entry in stored
        if isinstance(entry, Mapping) and isinstance(entry.get("name"), str)
    }  # como o ``from_doc`` lê: aparados
    result: list[Any] = []
    moved = 0
    for entry in stored:
        if isinstance(entry, Mapping) and isinstance(entry.get("name"), str) and check_emoji(entry.get("icon")) is None:
            icon, rest = split_leading_icon(entry["name"])
            name = check_name(rest) if icon else None
            if icon and isinstance(name, str) and name.casefold() not in taken:
                taken.discard(entry["name"].strip().casefold())
                taken.add(name.casefold())
                result.append({**entry, "name": name, "icon": icon})
                moved += 1
                continue
        result.append(entry)
    return result, moved


# ---------------------------------------------------------------------- exibição


def genres_of(playlists: Iterable[Playlist]) -> tuple[str, ...]:
    """Os gêneros distintos (``casefold``), com a 1ª grafia vista, em ordem alfabética."""
    seen: dict[str, str] = {}
    for playlist in playlists:
        for genre in playlist.genres:
            seen.setdefault(genre.casefold(), genre)
    return tuple(seen[key] for key in sorted(seen))


def filter_by_genre(playlists: Iterable[Playlist], genre: str | None) -> tuple[Playlist, ...]:
    """Todas se ``genre`` é vazio; senão as que têm o gênero (sem diferenciar a caixa)."""
    if not genre:
        return tuple(playlists)
    wanted = genre.casefold()
    return tuple(p for p in playlists if any(g.casefold() == wanted for g in p.genres))


def display_name(playlist: Playlist) -> str:
    """O nome para listar, com o ícone antes dele quando houver."""
    return f"{playlist.icon} {playlist.name}" if playlist.icon else playlist.name


def select_description(description: str) -> str:
    """Descrição para o select do Discord: cortada em 100 com reticências."""
    if len(description) <= PLAYLIST_SELECT_DESCRIPTION_MAX:
        return description
    return description[: PLAYLIST_SELECT_DESCRIPTION_MAX - 1].rstrip() + _ELLIPSIS


def _report_label(playlist: Playlist) -> str:
    """O nome para o relatório: o ícone (já validado, só escapado: o ``render_text`` tiraria o ZWJ) e o nome de terceiros escapado."""
    name = render_text(playlist.name, PLAYLIST_NAME_MAX)
    return f"{escape_markdown(playlist.icon)} {name}" if playlist.icon else name


def format_report(report: ImportReport, limit: int = PLAYLIST_REPORT_MAX) -> str:
    """Relatório da importação em pt-BR, com os nomes de terceiros escapados e no máximo ``limit`` caracteres (o resto vira ``+N``)."""
    text = f"**Importação concluída:** {len(report.added)} adicionada(s), {len(report.skipped)} ignorada(s)."
    if report.added:
        text += "\n**Adicionadas:** " + ", ".join(_report_label(p) for p in report.added)
    skipped = [
        f"• {render_text(s.name, PLAYLIST_NAME_MAX)}: {_REASON_TEXT.get(s.reason, s.reason)}" for s in report.skipped
    ]
    shown = 0
    for line in skipped:
        more = len(skipped) - shown - 1
        tail = f"\n+{more}" if more else ""
        if len(text) + 1 + len(line) + len(tail) > limit:
            break
        text += "\n" + line
        shown += 1
    if shown < len(skipped):
        text += f"\n+{len(skipped) - shown}"
    return text[:limit]
