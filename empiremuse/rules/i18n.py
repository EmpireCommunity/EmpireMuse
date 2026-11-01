# -*- coding: utf-8 -*-
"""Regras puras da localização (item 21): validação dos catálogos, formatação segura, resolução do idioma e o ``Translator``.

Os catálogos (``empiremuse/locales/<locale>/{commands,bot,panel}.json``) vêm da comunidade e saem com o nome do bot em todo servidor do
idioma. Por isso nada entra sem passar por ``validate_catalog``: um catálogo é JSON plano de ``str`` para ``str``, as chaves seguem
``área.assunto.mensagem`` e cada texto repete os placeholders, as menções, os links e as marcas de markdown da referência (``en-US``).
Aqui só há dados e decisões; a leitura dos arquivos e o ``pool.i18n`` são de ``empiremuse/i18n``. O log leva a chave e o motivo, nunca o
texto do catálogo nem o da exceção.
"""

from __future__ import annotations

import json
import re
import string
import unicodedata
from collections import Counter
from collections.abc import Callable, Collection, Mapping
from typing import Any, NamedTuple

from empiremuse.rules.owner_shell import is_secret_name

# O idioma de reserva do resolvedor (os testes o fixam em ``pt-BR``) e a referência dos catálogos (nunca muda).
FALLBACK_LOCALE = "en-US"
REFERENCE_LOCALE = "en-US"
REQUIRED_LOCALES = ("en-US", "pt-BR")
MIN_COVERAGE = 0.90
NAMESPACES = ("commands", "bot", "panel")

MAX_FILE_BYTES = 512 * 1024
MAX_KEY_LENGTH = 128
KEY_PATTERN = re.compile(r"[a-z0-9_]+(?:\.[a-z0-9_]+){1,6}")
OPTION_BASE = re.compile(r"[a-z0-9_]{1,32}")
OPTION_NAME_EXTRA = frozenset("-_'")
# Opções cujo VALOR é credencial mesmo sem o nome dizer (espelha ``logs.errors._SECRET_PARAMS``; um teste confere). O log mascara pela BASE.
SECRET_OPTION_NAMES = frozenset({"link"})
FIELD_PATTERN = re.compile(r"[a-z_][a-z0-9_]*")
PLURAL_CATEGORIES = ("zero", "one", "two", "few", "many", "other")
_PLURAL_SUFFIX = re.compile(r"(.+)_(zero|one|two|few|many|other)")

# Limites do Discord por tipo de chave (último segmento da chave, ou ``choice`` em qualquer segmento), em unidades UTF-16.
LIMITS: Mapping[str, int] = {
    "description": 100,
    "choice": 100,
    "label": 80,
    "placeholder": 150,
    "modal_title": 45,
    "title": 256,
    "name": 32,  # só ``cmd.<caminho>.opt.<base>.name``: o nome de uma opção de comando
}
DEFAULT_LIMIT = 2000
NO_NEWLINE_KINDS = frozenset(LIMITS)

# Hosts que um texto pode citar (a referência também passa por aqui, porque o CI não é fronteira: I-8).
ALLOWED_DOMAINS = frozenset({"github.com", "last.fm", "www.last.fm"})

DESC_PREFIX_FORMAT = "[{emoji} {name}] | "
# Emoji da legenda de cada grupo de comandos (``cmd.<grupo>._label`` guarda só o nome, no idioma).
GROUP_EMOJI: dict[str, str] = {"music": "🎶", "settings": "🔧", "lastfm": "🎧", "playlists": "📚", "misc": "🔰"}

# O grupo (``cmd.<grupo>._label``) da legenda de cada comando: a descrição é medida com a legenda do PRÓPRIO grupo.
COMMAND_GROUPS: Mapping[str, str] = {
    "247": "music",
    "about": "misc",
    "add_dj": "music",
    "add_dj_role": "settings",
    "autoplay": "music",
    "back": "music",
    "change_node": "music",
    "change_skin": "settings",
    "command_log": "music",
    "connect": "music",
    "controller": "music",
    "custom_skin": "settings",
    "fav_manager": "music",
    "invite": "misc",
    "language": "settings",
    "lastfm": "lastfm",
    "loop_amount": "music",
    "loop_mode": "music",
    "move": "music",
    "nightcore": "music",
    "nodeinfo": "settings",
    "now_playing": "music",
    "pause": "music",
    "play": "music",
    "play_music_file": "music",
    "player_settings": "settings",
    "playlists": "playlists",
    "playlists.exportar": "playlists",
    "playlists.importar": "playlists",
    "radio": "music",
    "radio.desligar": "music",
    "radio.ligar": "music",
    "radio.status": "music",
    "queue.clear": "music",
    "queue.display": "music",
    "queue.reverse": "music",
    "queue.shuffle": "music",
    "readd_songs": "music",
    "remove": "music",
    "remove_dj": "music",
    "remove_dj_role": "settings",
    "reset": "settings",
    "restrict_mode": "music",
    "resume": "music",
    "rich_presence": "settings",
    "rotate": "music",
    "save_queue": "music",
    "search": "music",
    "seek": "music",
    "set_voice_status": "music",
    "setup": "settings",
    "skip": "music",
    "skipto": "music",
    "song_request_thread": "music",
    "stop": "music",
    "volume": "music",
    "voteskip": "music",
}

_BIDI = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
_INVISIBLE = frozenset("\u200b\u2060\ufeff\u00ad\u2800\u3164\u115f\u1160\uffa0\u180e")
_LINE_BREAKS = frozenset("\u2028\u2029")
_JOINERS = frozenset("\u200d\u200c")
_MARK_PATTERNS = ("@everyone", "@here", "<@&", "<@", "<#", "</", "<id:")
_FULL_URL = re.compile(r"https?://[^\s<>)\]`\"']{1,512}")
_BARE_DOMAIN = re.compile(r"[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63}){0,8}\.[a-z]{2,24}/")
_HOST = re.compile(r"(?:://|www\.)([^\s/\\?#<>]{0,255})")  # a barra invertida também encerra o host, como no navegador
_ANGLE_TOKEN = re.compile(r"<[^<>]*>")
_LINE_START = re.compile(r"^(-# |#{1,6} |>{1,3} ?|- )", re.MULTILINE)
_FIELD_REMOVER = re.compile(r"\{[a-z_][a-z0-9_]*\}")
_MAX_COMBINING_RUN = 4
_LABEL_KEY = re.compile(r"cmd\.([a-z0-9_]+)\._label")
_MARKDOWN_MARKERS = ("`", "*", "_", "~~", "||", "\\", "](")

# Motivos de uma entrada descartada (estáveis: vão ao log e aos testes).
KEY_INVALID = "key"
KEY_UNKNOWN = "unknown_key"
EMPTY = "empty"
CHARS = "chars"
FORMAT = "format"
PLACEHOLDERS = "placeholders"
MARKS = "marks"
LINKS = "links"
MARKDOWN = "markdown"
NEWLINE = "newline"
SIZE = "size"
NAME = "name"
DUPLICATE = "duplicate"


class CatalogReport(NamedTuple):
    """``entries`` são só as entradas aceitas; ``dropped`` são ``(chave, motivo)``; ``fatal`` desliga o locale inteiro."""

    entries: dict[str, str]
    dropped: tuple[tuple[str, str], ...]
    fatal: str | None


# ---------------------------------------------------------------------- texto


def loggable_key(key: str) -> str:
    """A chave como pode ir ao log/CI: só se tiver o formato de chave; qualquer outra coisa (texto da comunidade) vira um marcador."""
    return (
        key
        if len(key) <= MAX_KEY_LENGTH and KEY_PATTERN.fullmatch(key)
        else f"<chave inválida de {len(key)} caracteres>"
    )


def utf16_length(text: str) -> int:
    """O tamanho que o Discord conta: unidades UTF-16 (o emoji fora do plano básico vale 2)."""
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


def _normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def kind_of(key: str) -> str | None:
    """O tipo da chave para os limites: ``choice`` (em qualquer segmento antes do último, que é o VALOR) ou o último segmento."""
    segments = key.split(".")
    if "choice" in segments[:-1]:
        return "choice"
    last = segments[-1].lstrip("_")
    return last if last in LIMITS else None


def plural_parts(key: str) -> tuple[str, str] | None:
    """``(base, categoria)`` de ``queue.count_one``, ou ``None`` se a chave não termina em categoria de plural."""
    match = _PLURAL_SUFFIX.fullmatch(key)
    return (match.group(1), match.group(2)) if match else None


def _visible(char: str) -> bool:
    if not char or char in _JOINERS:
        return False
    return not unicodedata.category(char).startswith(("Z", "C"))


def _bad_chars(value: str) -> bool:
    run = 0
    previous = ""
    for index, char in enumerate(value):
        category = unicodedata.category(char)
        if category in ("Cs", "Co", "Cn") or (category == "Cc" and char != "\n"):
            return True
        if char in _LINE_BREAKS or char in _BIDI or char in _INVISIBLE:
            return True
        if category == "Cf" and char not in _JOINERS:
            return True
        if char in _JOINERS:
            following = value[index + 1] if index + 1 < len(value) else ""
            if not (_visible(previous) and _visible(following)):
                return True
        run = run + 1 if category in ("Mn", "Me") else 0
        if run > _MAX_COMBINING_RUN:
            return True
        previous = char
    return False


# ---------------------------------------------------------------------- placeholders


def parse_template(template: str) -> tuple[tuple[str, ...], int] | None:
    """``(campos em ordem, chaves literais)`` ou ``None`` se o formato não for o permitido (I-1).

    Só ``{nome}`` com ``nome`` em ``[a-z_][a-z0-9_]*``: sem conversão, sem especificação, sem ``{}``, ``{0}``, ``{x.attr}`` ou ``{x[0]}``.
    """
    fields: list[str] = []
    braces = 0
    try:
        for literal, name, spec, conversion in string.Formatter().parse(template):
            braces += literal.count("{") + literal.count("}")
            if name is None:
                continue
            if conversion is not None or spec or not FIELD_PATTERN.fullmatch(name):
                return None
            fields.append(name)
    except ValueError:
        return None
    return tuple(fields), braces


def safe_format(template: str, values: Mapping[str, str | int]) -> str:
    """Preenche ``{nome}`` com valores LITERAIS (nunca reinterpretados). ``ValueError``/``KeyError``/``TypeError`` se algo não bate."""
    if parse_template(template) is None:
        raise ValueError("formato inválido")
    parts: list[str] = []
    for literal, name, _spec, _conversion in string.Formatter().parse(template):
        parts.append(literal)
        if name is None:
            continue
        value = values[name]
        if type(value) not in (str, int):
            raise TypeError("valor deve ser str ou int")
        parts.append(str(value))
    return "".join(parts)


def fit(text: str, limit: int) -> str:
    """Corta ``text`` no MEIO (``…``) para caber em ``limit`` unidades UTF-16, sem partir ``<...>`` (menção, emoji, data)."""
    if limit <= 0:
        return ""
    if utf16_length(text) <= limit:
        return text
    if limit == 1:
        return "…"
    budget = limit - 1
    head_budget = (budget + 1) // 2
    tail_budget = budget - head_budget
    head: list[str] = []
    used = 0
    for char in text:
        size = utf16_length(char)
        if used + size > head_budget:
            break
        head.append(char)
        used += size
    tail: list[str] = []
    used = 0
    for char in reversed(text):
        size = utf16_length(char)
        if used + size > tail_budget:
            break
        tail.append(char)
        used += size
    start = "".join(head)
    end = "".join(reversed(tail))
    if start.rfind("<") > start.rfind(">"):
        start = start[: start.rfind("<")]
    close = end.find(">")
    if close != -1 and (end.find("<") == -1 or close < end.find("<")):
        end = end[close + 1 :]
    return f"{start}…{end}"


# ---------------------------------------------------------------------- validação


def description_prefix(group: str, label: str) -> str:
    """A legenda ``[🎶 Música] | `` de uma descrição de comando, montada no idioma."""
    emoji = GROUP_EMOJI.get(group, "")
    return DESC_PREFIX_FORMAT.format(emoji=emoji, name=label) if emoji else f"[{label}] | "


def command_group(key: str) -> str | None:
    """O grupo da legenda de ``cmd.<caminho>.description`` (``None`` se o comando não está em ``COMMAND_GROUPS``)."""
    if not is_command_description(key):
        return None
    return COMMAND_GROUPS.get(key[len("cmd.") : -len(".description")])


def is_command_description(key: str) -> bool:
    """``cmd.<caminho>.description``: a descrição do comando em si (a única que leva a legenda do grupo; a de uma opção não)."""
    parts = key.split(".")
    return parts[0] == "cmd" and parts[-1] == "description" and "opt" not in parts


def option_parts(key: str) -> tuple[str, str] | None:
    """``(caminho do comando, base da opção)`` de ``cmd.<caminho>.opt.<base>.<...>``, ou ``None`` se a chave não é de uma opção."""
    parts = key.split(".")
    if parts[0] != "cmd" or "opt" not in parts[2:]:
        return None
    index = parts.index("opt", 2)
    return ".".join(parts[1:index]), parts[index + 1] if index + 1 < len(parts) else ""


def is_option_name(key: str) -> bool:
    """``cmd.<caminho>.opt.<base>.name``: o nome (traduzido) de uma opção. Uma choice cujo valor é ``name`` não conta."""
    parts = key.split(".")
    return (
        len(parts) >= 5 and parts[0] == "cmd" and parts[-1] == "name" and parts[-3] == "opt" and "choice" not in parts
    )


def option_name_ok(value: str) -> bool:
    """O nome de uma opção traduzido: 1 a 32 unidades UTF-16, minúsculo, só letras e dígitos (de qualquer escrita) ou ``-``, ``_`` e ``'``."""
    return (
        1 <= utf16_length(value) <= LIMITS["name"]
        and value == value.lower()
        and all(char.isalnum() or char in OPTION_NAME_EXTRA for char in value)
    )


def is_secret_option(name: str) -> bool:
    """O nome diz (ou o parâmetro é) credencial: ``is_secret_name`` mais ``SECRET_OPTION_NAMES``."""
    return is_secret_name(name) or name.casefold() in SECRET_OPTION_NAMES


def _link_tokens(value: str) -> Counter:
    text = _normalized(value)
    found: Counter = Counter()
    for marker in ("discord.gg", "discord.com/invite", "dsc.gg", "xn--", "]("):
        if marker in text:
            found[marker] += text.count(marker)
    hosts = _HOST.findall(text)
    for authority in hosts:
        host = authority.rsplit("@", 1)[-1].split(":")[0].rstrip(".")  # o host de verdade vem depois do userinfo
        found["host:" + host] += 1
    for url in _FULL_URL.findall(text):
        found["url:" + url.rstrip(".,;:!?")] += 1  # a URL INTEIRA: o mesmo host com outro caminho é outro link
    for domain in _BARE_DOMAIN.findall(text):
        found["bare:" + domain] += 1
    if not hosts:
        for marker in ("://", "www."):
            if marker in text:
                found[marker] += text.count(marker)
    return found


def _links_ok(value: str, reference_value: str | None) -> bool:
    tokens = _link_tokens(value)
    if reference_value is None:
        return all(
            token.startswith("url:")
            or (token.startswith("host:") and token[5:] in ALLOWED_DOMAINS)
            or (token.startswith("bare:") and token[5:-1] in ALLOWED_DOMAINS)
            for token in tokens
        )
    return not (tokens - _link_tokens(reference_value))


def _marks_ok(value: str, reference_value: str | None) -> bool:
    text = _normalized(value)
    if reference_value is None:
        return "@everyone" not in text and "@here" not in text
    ref = _normalized(reference_value)
    if any(text.count(p) > ref.count(p) for p in _MARK_PATTERNS):
        return False
    if value.count("<") != reference_value.count("<"):
        return False
    return not (Counter(_ANGLE_TOKEN.findall(value)) - Counter(_ANGLE_TOKEN.findall(reference_value)))


def _markdown_signature(value: str) -> tuple:
    plain = _FIELD_REMOVER.sub("", value)
    starts = Counter(marker.rstrip() for marker in _LINE_START.findall(plain))
    return (
        tuple(plain.count(marker) for marker in _MARKDOWN_MARKERS),
        tuple(sorted(starts.items())),
        plain.count("\n"),
    )


def _limit_for(key: str, reference_value: str | None) -> int:
    ceiling = LIMITS.get(kind_of(key) or "", DEFAULT_LIMIT)
    if reference_value is None:
        return ceiling
    size = utf16_length(reference_value)
    return min(ceiling, max(4 * size, size + 40))


def _reference_value(key: str, reference: Mapping[str, str]) -> str | None | bool:
    """O texto da referência para ``key``; para plural fora da referência, o ``_other``. ``False`` se a chave não existe nela."""
    if key in reference:
        return reference[key]
    parts = plural_parts(key)
    if parts and f"{parts[0]}_other" in reference:
        return reference[f"{parts[0]}_other"]
    return False


def check_entry(
    key: str, value: str, reference: Mapping[str, str] | None, prefixes: Mapping[str, int] | None = None
) -> str | None:
    """O motivo de ``(key, value)`` ser descartada, ou ``None`` se passa. ``reference`` ``None`` = validando a própria referência."""
    if len(key) > MAX_KEY_LENGTH or not KEY_PATTERN.fullmatch(key):
        return KEY_INVALID
    if key.endswith("._label") and (not (match := _LABEL_KEY.fullmatch(key)) or match.group(1) not in GROUP_EMOJI):
        return KEY_INVALID
    ref_value: str | None = None
    if reference is not None:
        found = _reference_value(key, reference)
        if found is False:
            return KEY_UNKNOWN
        ref_value = found  # type: ignore[assignment]
    if is_option_name(key):
        parts = option_parts(key)
        if parts is None or not OPTION_BASE.fullmatch(parts[1]) or not option_name_ok(value):
            return NAME
        if reference is None and value != parts[1]:  # na referência o nome é a própria base
            return NAME
        if is_secret_option(value) and not is_secret_option(parts[1]):  # um nome de segredo só vale na base
            return NAME
        return None  # o conjunto de caracteres já exclui menção, link, markdown e placeholder
    if not value.strip():
        return EMPTY
    if utf16_length(value) > _limit_for(key, ref_value):
        return SIZE  # antes de qualquer regex: nenhuma delas vê mais do que o teto da chave
    if _bad_chars(value):
        return CHARS
    parsed = parse_template(value)
    if parsed is None:
        return FORMAT
    if ref_value is not None:
        ref_parsed = parse_template(ref_value)
        if ref_parsed is None or Counter(parsed[0]) != Counter(ref_parsed[0]) or parsed[1] != ref_parsed[1]:
            return PLACEHOLDERS
    elif parsed[1]:
        return PLACEHOLDERS
    if not _marks_ok(value, ref_value):
        return MARKS
    if not _links_ok(value, ref_value):
        return LINKS
    if ref_value is not None and _markdown_signature(value) != _markdown_signature(ref_value):
        return MARKDOWN
    if "\n" in value and kind_of(key) in NO_NEWLINE_KINDS:
        return NEWLINE
    size = utf16_length(value)
    if size > _limit_for(key, ref_value):
        return SIZE
    if is_command_description(key):
        group = command_group(key)
        prefix = (prefixes or {}).get(group) if group else max((prefixes or {}).values(), default=0)
        if (prefix or 0) + size > LIMITS["description"]:
            return SIZE
    return None


def _decode(raw: bytes) -> tuple[dict[str, str] | None, str | None]:
    """O JSON como dicionário plano ``str -> str``, ou ``(None, motivo)`` (fatal). Chave repetida e estrutura errada são fatais."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, item in items:
            if name in result:
                raise ValueError("chave repetida")
            result[name] = item
        return result

    try:
        text = raw.decode("utf-8", errors="strict")
        data = json.loads(text, object_pairs_hook=pairs)
    except RecursionError:
        return None, "profundidade"
    except UnicodeDecodeError:
        return None, "utf8"
    except ValueError as error:
        return None, "chave repetida" if str(error) == "chave repetida" else "json"
    except Exception:  # noqa: BLE001 - qualquer falha na carga desliga o locale, nunca derruba a subida
        return None, "carga"
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        return None, "estrutura"
    return data, None


def validate_catalog(locale: str, namespace: str, raw: bytes, reference: Mapping[str, str] | None) -> CatalogReport:
    """Valida um catálogo. ``reference`` é o ``en-US`` do mesmo arquivo (``None`` ao validar o próprio ``en-US``). Nunca levanta."""
    try:
        return _validate(namespace, raw, reference)
    except Exception:  # noqa: BLE001 - o validador não pode derrubar a subida
        return CatalogReport({}, (), "validador")


def _validate(namespace: str, raw: bytes, reference: Mapping[str, str] | None) -> CatalogReport:
    if len(raw) > MAX_FILE_BYTES:
        return CatalogReport({}, (), "tamanho")
    data, fatal = _decode(raw)
    if data is None:
        return CatalogReport({}, (), fatal)
    if reference is not None and len(data) > 2 * max(len(reference), 1):
        return CatalogReport({}, (), "chaves demais")
    accepted = {key: value for key, value in _labels(data).items() if check_entry(key, value, reference) is None}
    prefixes = _prefix_units({**_labels(reference or {}), **accepted})
    entries: dict[str, str] = {}
    dropped: list[tuple[str, str]] = []
    for key, value in data.items():
        is_command = key.startswith("cmd.")
        reason = (
            KEY_UNKNOWN if is_command != (namespace == "commands") else check_entry(key, value, reference, prefixes)
        )
        if reason is None:
            entries[key] = value
        else:
            dropped.append((key, reason))
    for key in _conflicting_levels(entries, reference):
        del entries[key]
        dropped.append((key, DUPLICATE))
    return CatalogReport(entries, tuple(dropped), None)


def _labels(source: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in source.items() if key.startswith("cmd.") and key.endswith("._label")}


def _prefix_units(labels: Mapping[str, str]) -> dict[str, int]:
    """O tamanho (UTF-16) da legenda ``[🎶 Música] | `` de cada grupo, no idioma do catálogo."""
    return {
        key.split(".")[1]: utf16_length(description_prefix(key.split(".")[1], value)) for key, value in labels.items()
    }


def _conflicting_levels(entries: Mapping[str, str], reference: Mapping[str, str] | None) -> list[str]:
    """Os ``cmd.<caminho>.opt.<base>.name`` aceitos de cada nível (comando, subcomando ou grupo) em que o nome EFETIVO se repete.

    O nome efetivo é o localizado ou, se faltar ou tiver sido descartado, a base. Qualquer colisão derruba TODA a localização de nome do
    nível naquele locale: descartar só os repetidos deixaria uma base voltar e colidir com o que sobrou (o sync global falharia).
    """
    levels: dict[str, dict[str, str]] = {}
    for key, value in (reference if reference is not None else entries).items():
        parts = option_parts(key) if is_option_name(key) else None
        if parts is not None:
            levels.setdefault(parts[0], {})[parts[1]] = value
    doomed: list[str] = []
    for path, bases in levels.items():
        effective = [entries.get(f"cmd.{path}.opt.{base}.name", base_name) for base, base_name in bases.items()]
        if len(set(effective)) != len(effective):
            doomed.extend(key for base in bases if (key := f"cmd.{path}.opt.{base}.name") in entries)
    return doomed


# ---------------------------------------------------------------------- cobertura e resolução


def coverage(
    entries: Mapping[str, str], reference: Mapping[str, str], categories: Collection[str] = ("one", "other")
) -> float:
    """A fração das chaves-base da referência que ``entries`` cobre; o plural conta 1, com todas as ``categories`` do locale."""
    bases: dict[str, bool] = {}
    for key in reference:
        parts = plural_parts(key)
        if parts and f"{parts[0]}_other" in reference:
            bases[parts[0]] = True
        else:
            bases[key] = False
    if not bases:
        return 1.0
    covered = 0
    for base, plural in bases.items():
        if plural:
            covered += all(f"{base}_{category}" in entries for category in categories)
        else:
            covered += base in entries
    return covered / len(bases)


_SIBLINGS = {"es-419": "es-ES", "es-ES": "es-419", "en-GB": "en-US"}


def resolve_locale(saved: str | None, preferred: str | None, enabled: Collection[str], fallback: str) -> str:
    """``saved`` se ligado → ``preferred`` se ligado → o irmão do ``preferred`` → ``fallback``. ``zh-*`` nunca tem irmão."""
    for candidate in (saved, preferred):
        if candidate and candidate in enabled:
            return candidate
    sibling = _SIBLINGS.get(preferred or "")
    if sibling and sibling in enabled:
        return sibling
    return fallback


# ---------------------------------------------------------------------- tradutor


def _default_plural(count: int) -> str:
    return "one" if count == 1 else "other"


class Translator:
    """Tradutor imutável de um locale: locale → ``en-US`` → a própria chave. Nunca levanta; a saída é texto final (jamais reformatado)."""

    __slots__ = ("locale", "_entries", "_reference", "_plural", "_missing")

    locale: str
    _entries: dict[str, str]
    _reference: dict[str, str]
    _plural: Callable[[int], str]
    _missing: Callable[[str, str], None] | None

    def __init__(
        self,
        locale: str,
        entries: Mapping[str, str],
        reference: Mapping[str, str],
        *,
        plural: Callable[[int], str] | None = None,
        on_missing: Callable[[str, str], None] | None = None,
    ) -> None:
        object.__setattr__(self, "locale", locale)
        object.__setattr__(self, "_entries", dict(entries))
        object.__setattr__(self, "_reference", dict(reference))
        object.__setattr__(self, "_plural", plural or _default_plural)
        object.__setattr__(self, "_missing", on_missing)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Translator é imutável")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("Translator é imutável")

    def __call__(self, key: str, /, *, count: int | None = None, **values: str | int) -> str:
        if count is not None:
            values.setdefault("count", count)
        for source, plural in ((self._entries, self._plural), (self._reference, _default_plural)):
            template = self._lookup(source, key, count, plural)
            if template is None:
                continue
            try:
                return safe_format(template, values)
            except Exception:  # noqa: BLE001 - formato ou valor ruim cai para a próxima fonte
                continue
        if self._missing is not None:
            try:
                self._missing(self.locale, key)
            except Exception:  # noqa: BLE001
                pass
        return key

    @staticmethod
    def _lookup(source: Mapping[str, str], key: str, count: int | None, plural: Callable[[int], str]) -> str | None:
        if count is None:
            return source.get(key)
        try:
            category = plural(count)
        except Exception:  # noqa: BLE001
            category = _default_plural(count)
        return source.get(f"{key}_{category}") or source.get(f"{key}_other")
