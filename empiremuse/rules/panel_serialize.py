"""Serialização da API do painel externo (contrato T-456 §4 m5): dados puros por lista de permissão de campos.

Só biblioteca padrão. Lê o que os serviços do painel já devolvem (``key``, ``versao``, ``valores``, ``extras``,
``erros``) sem importar nem alterar o painel genérico, e produz JSON puro: só sai o campo que está no registro
abaixo; campo novo no serviço não vaza até alguém decidir expô-lo. ``RpcTokenSection.values`` e ``sessionkey`` não
têm entrada aqui e portanto nunca saem. O token novo do RPC só sai por ``new_rpc_token``, uma vez, com ``no-store``.

Textos de usuário final (``mensagem``, texto dos erros) não saem: a API devolve o código e os nomes dos campos.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Optional, Protocol

GUILD = "guild"
ACCOUNT = "account"

MAX_DEPTH = 8
FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}", re.ASCII)
NO_STORE = {"Cache-Control": "no-store"}
RPC_KEY = "rpc"
NEW_TOKEN_EXTRA = "token_novo"  # onde o serviço põe o token recém-gerado (só na resposta da geração)

# Forma permitida: nome do campo -> ``None`` (valor simples ou lista de valores simples) ou outra forma (objeto, ou
# lista de objetos). Em objetos a leitura é por atributo (``NamedTuple``), por chave (``dict``) ou, em pares puros, por
# posição, na ordem em que a forma foi escrita.
Shape = Mapping[str, "Shape | None"]


class FormLike(Protocol):
    key: str
    versao: str
    valores: Mapping[str, Any]
    extras: Mapping[str, Any]
    erros: Mapping[str, str]


class UnknownForm(LookupError):
    """Formulário (ou escopo) fora do registro: falha fechada, nada sai."""


class SerializationError(ValueError):
    """Valor que não é dado puro (objeto, ``dict`` sem forma, profundidade demais)."""


class FormShape:
    __slots__ = ("values", "extras")

    def __init__(self, values: Shape | None = None, extras: Shape | None = None) -> None:
        self.values: Shape = values or {}
        self.extras: Shape = extras or {}


_SKINS: Shape = {"skins": None, "skins_estaticas": None}
_COUNTED: Shape = {"total": None, "maximo": None, "ilimitado": None, "cheio": None}

_REGISTRY: Mapping[str, Mapping[str, FormShape]] = {
    GUILD: {
        "player": FormShape(
            {
                name: None
                for name in ("default_player_volume", "autoplay", "check_other_bots_in_vc", "enable_restrict_mode")
            },
            {"volumes": None},
        ),
        "dj": FormShape({"djroles": None}, {"cargos": {"id": None, "name": None}}),
        "prefixo": FormShape({"prefix": None}),
        "status_voz": FormShape(
            {"voice_status_permanent": None, "voice_channel_status": None},
            {"placeholders": None, "modelo_padrao": None},
        ),
        "idioma": FormShape({"language": None}, {"idiomas": {"code": None, "name": None}}),
        "ouvir_junto": FormShape(
            {}, {"itens": {"canal_id": None, "canal_nome": None, "convite": None}, "pode_remover": None}
        ),
        "skin_servidor": FormShape({"skin": None, "static_skin": None}, _SKINS),
        "skin_global": FormShape({"global_skin": None, "player_skin": None, "player_skin_static": None}, _SKINS),
        "links": FormShape(
            {},
            {
                "escopos": {
                    "escopo": None,
                    "total": None,
                    "maximo": None,
                    "pode_adicionar": None,
                    "itens": {
                        "ordem": None,
                        "nome": None,
                        "url": None,
                        "descricao": None,
                        "generos": None,
                        "icone": None,
                        "escopo": None,
                    },
                },
                "escopo_atual": None,
                "relatorio": {"adicionadas": None, "ignoradas": {"nome": None}},
            },
        ),
    },
    ACCOUNT: {
        "prefixo": FormShape({"custom_prefix": None}),
        "favoritos": FormShape({}, {"itens": {"nome": None, "url": None, "link": None}, **_COUNTED}),
        "radios": FormShape(
            {},
            {
                "disponivel": None,
                "indisponivel": None,
                "estacoes": {"uuid": None, "nome": None},
                "filtros": {"node": None, "rotulo": None},
            },
        ),
        "integracoes": FormShape(
            {}, {"itens": {"titulo": None, "url": None, "avatar": None, "link": None}, **_COUNTED}
        ),
        "lastfm": FormShape({"scrobble": None}, {"vinculada": None, "usuario": None}),
        RPC_KEY: FormShape({}, {"disponivel": None, "configurado": None}),
    },
}

_FORBIDDEN_FRAGMENTS = ("token", "secret", "sessionkey", "session_key", "password", "senha", "authorization", "cookie")


def registered_forms() -> list[tuple[str, str]]:
    return [(scope, key) for scope, forms in _REGISTRY.items() for key in forms]


def _names(shape: Shape) -> list[str]:
    found: list[str] = []
    for name, sub in shape.items():
        found.append(name)
        if sub is not None:
            found.extend(_names(sub))
    return found


def all_field_names() -> list[tuple[str, str]]:
    """Todo nome de campo permitido, com ``escopo/formulário`` no primeiro item (testes e checagem de import)."""
    out: list[tuple[str, str]] = []
    for scope, forms in _REGISTRY.items():
        for key, form in forms.items():
            where = f"{scope}/{key}"
            out.extend((where, name) for name in _names(form.values) + _names(form.extras))
    return out


def _check_registry() -> None:
    for where, name in all_field_names():
        if not FIELD_NAME.fullmatch(name) or any(part in name for part in _FORBIDDEN_FRAGMENTS):
            raise RuntimeError(f"campo não permitido no registro da API ({where}): {name}")


_check_registry()


def _read(item: Any, name: str, position: int) -> tuple[bool, Any]:
    if hasattr(item, "_fields"):  # NamedTuple: por nome
        return (True, getattr(item, name)) if name in item._fields else (False, None)
    if isinstance(item, Mapping):
        return (True, item[name]) if name in item else (False, None)
    if isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
        return (True, item[position]) if position < len(item) else (False, None)
    return False, None


def _leaf(value: Any, depth: int) -> Any:
    """Valor simples ou lista (de listas) de valores simples; qualquer outra coisa não é dado puro."""
    if depth > MAX_DEPTH:
        raise SerializationError("profundidade acima do limite")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)) and not hasattr(value, "_fields"):
        return [_leaf(item, depth + 1) for item in value]
    raise SerializationError(f"valor que não é dado puro: {type(value).__name__}")


def _object(item: Any, shape: Shape, depth: int) -> dict[str, Any]:
    if depth > MAX_DEPTH:
        raise SerializationError("profundidade acima do limite")
    out: dict[str, Any] = {}
    for position, (name, sub) in enumerate(shape.items()):
        present, value = _read(item, name, position)
        if present:
            out[name] = _shaped(value, sub, depth + 1)
    return out


def _is_positional_object(value: Sequence[Any], shape: Shape) -> bool:
    """``True`` quando a própria sequência é UM objeto posicional (ex.: ``("pt-BR", "Português")``), não uma lista deles."""
    return bool(value) and len(value) <= len(shape) and all(isinstance(v, (str, int, float, bool)) for v in value)


def _shaped(value: Any, sub: Shape | None, depth: int) -> Any:
    if sub is None:
        return _leaf(value, depth)
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and not hasattr(value, "_fields") and not _is_positional_object(value, sub):
        return [_object(item, sub, depth) for item in value]
    return _object(value, sub, depth)


def _pick(source: Mapping[str, Any], shape: Shape) -> dict[str, Any]:
    return {name: _shaped(source[name], sub, 0) for name, sub in shape.items() if name in source}


def _shape_of(form: FormLike, scope: str) -> FormShape:
    try:
        return _REGISTRY[scope][form.key]
    except KeyError:
        raise UnknownForm(f"{scope}/{form.key}") from None


def serialize_form(form: FormLike, scope: str) -> dict[str, Any]:
    """O formulário como dado puro: ``{key, version, values, extras}``, só com os campos da lista de permissão."""
    shape = _shape_of(form, scope)
    return {
        "key": form.key,
        "version": form.versao,
        "values": _pick(form.valores, shape.values),
        "extras": _pick(form.extras, shape.extras),
    }


def error_fields(form: FormLike) -> list[str]:
    """Nomes (ordenados) dos campos com erro, para o ``fields`` do envelope de erro; nome fora do padrão é descartado."""
    return sorted(name for name in form.erros if isinstance(name, str) and FIELD_NAME.fullmatch(name))


def new_rpc_token(form: FormLike) -> Optional[tuple[dict[str, str], dict[str, str]]]:
    """O token novo do RPC, uma vez: ``(corpo, cabeçalhos)`` só para o formulário ``rpc`` recém-gerado; senão ``None``."""
    if form.key != RPC_KEY:
        return None
    token = form.extras.get(NEW_TOKEN_EXTRA)
    if not isinstance(token, str) or not token:
        return None
    return {"token": token}, dict(NO_STORE)
