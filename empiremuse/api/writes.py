"""Corpos das escritas de conta e de servidor: JSON com tipo conferido -> os argumentos de formulário que os serviços do
painel já entendem (contrato T-456 §4, s4).

O corpo é validado quanto ao **tipo** antes de qualquer serviço (valor de tipo errado, campo que falta e campo
desconhecido em ``values`` dão ``validation_failed`` com os nomes em ``fields``, nunca ``TypeError``). Chave desconhecida
fora do ``values`` (um ``user_id``, por exemplo) é ignorada: o usuário e o servidor vêm sempre da sessão e do caminho. As regras de valor (tamanho,
formato, cota) continuam nos serviços: aqui só se converte. Uma escrita é sempre uma **substituição**: todo campo do
formulário vem no ``values`` (o checkbox desmarcado é ``false``, não a ausência).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, NamedTuple, Optional, Sequence

from empiremuse.api.errors import validation_failed
from empiremuse.panel.services.base import HEX16
from empiremuse.panel.services.listen_along import ACTION as LISTEN_ALONG_REMOVE
from empiremuse.rules.panel_serialize import FIELD_NAME
from empiremuse.rules.playlists import SCOPES

BOOL, INT, INT_OR_NULL, TEXT, TEXT_OR_NULL, IDS, GENRES, JSON_DOC, LINES, NAMES = (
    "bool",
    "int",
    "int_or_null",
    "text",
    "text_or_null",
    "ids",
    "genres",
    "json",
    "lines",  # lista de textos que o serviço lê como um texto de várias linhas (um item por linha)
    "names",  # lista de textos que o serviço lê como valores repetidos do mesmo campo
)
MAX_TEXT = 2000
MAX_INT_ABS = 10**9
MAX_LIST = 100
MAX_ITEM = 40
MAX_LINE_ITEM = 128
MAX_TARGET = 128
GENRE_SEPARATORS = (",", ";")

Spec = Mapping[str, tuple[str, bool]]  # campo -> (tipo, obrigatório)


def required(kind: str) -> tuple[str, bool]:
    return kind, True


def optional(kind: str) -> tuple[str, bool]:
    return kind, False


class _Invalid(Exception):
    """O valor não tem o tipo do campo."""


@dataclass(frozen=True)
class Write:
    """Uma escrita pronta para o serviço: a ação dele, a versão esperada (``None`` onde o serviço não usa) e os argumentos."""

    action: str
    version: Optional[str]
    args: dict[str, list[str]]

    def service_args(self) -> dict[str, list[str]]:
        return {**self.args, **({"versao": [self.version]} if self.version else {})}


# ---------------------------------------------------------------------------- tipos


def _text(value: Any) -> list[str]:
    if not isinstance(value, str) or len(value) > MAX_TEXT:
        raise _Invalid
    return [value]


def _integer(value: Any) -> list[str]:
    if not isinstance(value, int) or isinstance(value, bool) or abs(value) >= MAX_INT_ABS:
        raise _Invalid
    return [str(value)]


def _ids(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_LIST:
        raise _Invalid
    if not all(isinstance(item, str) and 0 < len(item) <= MAX_ITEM for item in value):
        raise _Invalid
    return list(value)


def _names(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_LIST:
        raise _Invalid
    if not all(isinstance(item, str) and 0 < len(item) <= MAX_LINE_ITEM and "\n" not in item for item in value):
        raise _Invalid
    return list(value)


def _lines(value: Any) -> list[str]:
    items = _names(value)
    return ["\n".join(items)] if items else []


def _genres(value: Any) -> list[str]:
    items = _ids(value)
    if any(sep in item for item in items for sep in GENRE_SEPARATORS):
        raise _Invalid
    return [", ".join(items)]


def _json_document(value: Any) -> list[str]:
    if not isinstance(value, (dict, list)):
        raise _Invalid
    return [json.dumps(value, ensure_ascii=False, separators=(",", ":"))]


def convert(kind: str, value: Any) -> list[str]:
    """O valor do campo como valores de formulário; ``[]`` é o checkbox desmarcado. Tipo errado: ``_Invalid``."""
    if kind == BOOL:
        if not isinstance(value, bool):
            raise _Invalid
        return ["on"] if value else []
    if value is None and kind in (INT_OR_NULL, TEXT_OR_NULL):
        return [""]
    converters = {
        INT: _integer,
        INT_OR_NULL: _integer,
        TEXT: _text,
        TEXT_OR_NULL: _text,
        IDS: _ids,
        GENRES: _genres,
        JSON_DOC: _json_document,
        LINES: _lines,
        NAMES: _names,
    }
    return converters[kind](value)


def values_to_args(values: Any, spec: Spec) -> dict[str, list[str]]:
    """O objeto ``values`` como argumentos de formulário, conferido contra ``spec``."""
    if values is None and not any(needed for _, needed in spec.values()):
        values = {}
    if not isinstance(values, dict):
        raise validation_failed("values")
    unknown = [name if FIELD_NAME.fullmatch(name) else "values" for name in values if name not in spec]
    missing = [name for name, (_, needed) in spec.items() if needed and name not in values]
    args: dict[str, list[str]] = {}
    wrong: list[str] = []
    for name, (kind, _) in spec.items():
        if name not in values:
            continue
        try:
            converted = convert(kind, values[name])
        except _Invalid:
            wrong.append(name)
            continue
        if converted:
            args[name] = converted
    problems = sorted({*unknown, *missing, *wrong})
    if problems:
        raise validation_failed(*problems)
    return args


# ---------------------------------------------------------------------------- envelope


def read_version(body: Mapping[str, Any]) -> str:
    version = body.get("expected_version")
    if not isinstance(version, str) or not HEX16.fullmatch(version):
        raise validation_failed("expected_version")
    return version


def read_choice(body: Mapping[str, Any], name: str, options: Sequence[str]) -> str:
    value = body.get(name)
    if not isinstance(value, str) or value not in options:
        raise validation_failed(name)
    return value


# ---------------------------------------------------------------------------- servidor

_PLAYLIST_FIELDS: Spec = {
    "nome": required(TEXT),
    "url": required(TEXT),
    "descricao": optional(TEXT),
    "generos": optional(GENRES),
    "icone": optional(TEXT_OR_NULL),
    "ordem": optional(INT_OR_NULL),
}
LINKS_ACTIONS: Mapping[str, tuple[str, Spec]] = {
    "add": ("adicionar", _PLAYLIST_FIELDS),
    "edit": ("editar", {**_PLAYLIST_FIELDS, "alvo": required(TEXT)}),
    "remove": ("remover", {"alvo": required(TEXT)}),
    "import": ("importar", {"dados": required(JSON_DOC)}),
}
GUILD_FORM_SPECS: Mapping[str, Spec] = {
    "player": {
        "default_player_volume": required(INT),
        "autoplay": required(BOOL),
        "check_other_bots_in_vc": required(BOOL),
        "enable_restrict_mode": required(BOOL),
    },
    "dj": {"djroles": required(IDS)},
    "prefixo": {"prefix": required(TEXT)},
    "status_voz": {"voice_status_permanent": required(BOOL), "voice_channel_status": required(TEXT)},
    "idioma": {"language": required(TEXT_OR_NULL)},
    "skin_servidor": {"skin": required(TEXT), "static_skin": required(TEXT)},
    "skin_global": {
        "global_skin": required(BOOL),
        "player_skin": required(TEXT_OR_NULL),
        "player_skin_static": required(TEXT_OR_NULL),
    },
}
LISTEN_ALONG_SPEC: Spec = {"canal": required(TEXT)}


@dataclass(frozen=True)
class GuildWrite:
    form_key: str
    write: Write


def parse_guild_write(form_keys: Sequence[str], body: Mapping[str, Any]) -> GuildWrite:
    """``PUT /guilds/{g}/settings/{category}``: ``{form?, expected_version, action?, scope?, values}``."""
    form_key = read_choice(body, "form", form_keys) if "form" in body or len(form_keys) != 1 else form_keys[0]
    version = read_version(body)
    if form_key == "links":
        return GuildWrite(form_key, _links_write(body, version))
    if form_key == "ouvir_junto":
        read_choice(body, "action", ("remove",))
        args = values_to_args(body.get("values"), LISTEN_ALONG_SPEC)
        return GuildWrite(form_key, Write(LISTEN_ALONG_REMOVE, version, {"acao": [LISTEN_ALONG_REMOVE], **args}))
    return GuildWrite(form_key, Write("", version, values_to_args(body.get("values"), GUILD_FORM_SPECS[form_key])))


def _links_write(body: Mapping[str, Any], version: str) -> Write:
    action = read_choice(body, "action", tuple(LINKS_ACTIONS))
    scope = read_choice(body, "scope", tuple(SCOPES))
    service_action, spec = LINKS_ACTIONS[action]
    args = values_to_args(body.get("values"), spec)
    return Write(service_action, version, {"acao": [service_action], "escopo": [scope], **args})


# ---------------------------------------------------------------------------- conta

ACCOUNT_ACTIONS: Mapping[str, Mapping[Optional[str], tuple[str, Spec]]] = {
    "prefixo": {None: ("prefixo", {"custom_prefix": required(TEXT)})},
    "lastfm": {None: ("lastfm_scrobble", {"scrobble": required(BOOL)})},
    "rpc": {None: ("rpc_token_gerar", {})},
    "favoritos": {
        "add": ("favorito_adicionar", {"nome": required(TEXT), "url": required(TEXT)}),
        "edit": ("favorito_editar", {"nome": required(TEXT), "url": required(TEXT)}),
        "import": ("favoritos_importar", {"dados": required(JSON_DOC)}),
    },
    "integracoes": {
        "remove": ("integracao_remover", {"nome": required(TEXT)}),
        "import": ("integracoes_importar", {"dados": required(JSON_DOC)}),
    },
}


def parse_account_write(section: str, body: Mapping[str, Any], *, actions: Sequence[str] = ()) -> Write:
    """Escrita numa seção da conta: ``{expected_version, action?, values}``; ``actions`` são as ações que a rota aceita."""
    action = read_choice(body, "action", actions) if actions else None
    service_action, spec = ACCOUNT_ACTIONS[section][action]
    return Write(service_action, read_version(body), values_to_args(body.get("values"), spec))


def _query_text(query: Mapping[str, str], name: str) -> str:
    value = query.get(name)
    if not value or len(value) > MAX_TARGET:
        raise validation_failed(name)
    return value


def parse_favorite_removal(query: Mapping[str, str]) -> Write:
    """``DELETE /account/favorites?name=&expected_version=``: o nome é texto livre, por isso vai na query e não no caminho."""
    version = read_version(query)
    return Write("favorito_remover", version, {"nome": [_query_text(query, "name")]})


class RadioRemoval(NamedTuple):
    action: str  # ação do serviço
    field: str  # nome do argumento que o serviço lê
    target: str


def parse_radio_removal(query: Mapping[str, str]) -> RadioRemoval:
    """``DELETE /account/radio-favorites?kind=station|filter&id=``. Não há versão: remover é idempotente."""
    kind = read_choice(query, "kind", ("station", "filter"))
    target = _query_text(query, "id")
    if kind == "station":
        return RadioRemoval("radio_estacao_remover", "estacao", target)
    return RadioRemoval("radio_filtro_remover", "filtro", target)
