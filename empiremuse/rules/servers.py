"""Regras puras do documento único por servidor (item 11, P1): modelo, validação, migração do principal e a visão no formato antigo.

O documento fica na coleção ``servers`` do banco global (``_id = str(gid)``). Aqui só há dados e decisões; o banco e a trava são do
``utils/servers``. A migração de ``settings`` copia só o documento antigo do bot principal (os das reservas são ignorados) e todo
valor inválido cai no padrão (contrato §2).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from empiremuse.rules.limits import CUSTOM_SKIN_PREFIX, MAX_DJ_ROLES, VOLUMES

DOC_VERSION = 1.0
SECTION_SETTINGS = "settings"
SECTION_PLAYLISTS = "playlists"
SECTION_REQUEST = "request"
REQUEST_FIELDS = ("channel", "message_id")  # o que o formato antigo ainda enxerga em player_controller
MIGRATED_SECTIONS = ("settings", "request", "playlists")

SETTINGS_FIELDS = (
    "djroles",
    "default_player_volume",
    "autoplay",
    "enable_restrict_mode",
    "check_other_bots_in_vc",
    "skin",
    "static_skin",
    "language",
    "request_channel_strict",
)
# Campos novos, só do documento único: o formato antigo nunca os enxerga nem grava (``save_legacy`` os recusa).
UNIFIED_ONLY_FIELDS = ("language", "request_channel_strict")
LEGACY_FIELDS = tuple(name for name in SETTINGS_FIELDS if name not in UNIFIED_ONLY_FIELDS)
LEGACY_LANGUAGE = "pt-BR"  # o que um servidor que já existia ganha na migração (a comunidade de hoje é brasileira)
FLAG_FIELDS = ("autoplay", "enable_restrict_mode", "check_other_bots_in_vc", "request_channel_strict")
SKIN_MAX_LENGTH = 100
MAIN_IDENTIFIERS = ("TOKEN", "TOKEN_1")  # o principal é o token principal, nunca o primeiro por ordem (contrato §3)

_ROLE_ID = re.compile(r"[0-9]{1,20}")
_LEGACY_CONTROLLER_KEYS = {"skin": "skin", "static_skin": "static_skin"}


class SettingError(ValueError):
    """Campo desconhecido ou valor inválido numa alteração de ``settings`` (erro de programação, nunca texto do usuário)."""


@dataclass(frozen=True)
class Allowed:
    """O que existe no mundo no momento: skins e ids de cargos do servidor."""

    skins: frozenset[str] = frozenset()
    static_skins: frozenset[str] = frozenset()
    role_ids: frozenset[str] = frozenset()
    languages: frozenset[str] = frozenset()  # os locales ligados no pool (``pool.i18n.enabled``)


# ---------------------------------------------------------------------- modelo


def default_settings() -> dict[str, Any]:
    return {
        "djroles": [],
        "default_player_volume": 100,
        "autoplay": False,
        "enable_restrict_mode": False,
        "check_other_bots_in_vc": False,
        "skin": None,
        "static_skin": None,
        "language": None,  # ``None`` = automático (o idioma do Discord do servidor)
        "request_channel_strict": False,  # Jukebox estrita: o canal de pedidos apaga tudo o que se escreve nele
    }


def request_channel_strict(settings: Mapping[str, Any]) -> bool:
    """O canal de pedidos do servidor é estrito? Documento antigo não tem a chave: continua como sempre foi (não estrito)."""
    return settings.get("request_channel_strict") is True


def new_doc() -> dict[str, Any]:
    return {
        "ver": DOC_VERSION,
        "rev": 0,
        "settings": default_settings(),
        "request": {"channel": None, "message_id": None, "panel_bot_id": None},
        "playlists": [],
        "migrated": {section: False for section in MIGRATED_SECTIONS},
    }


def is_main_identifier(identifier: object) -> bool:
    return identifier in MAIN_IDENTIFIERS


# ---------------------------------------------------------------------- validação


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def normalize_role_ids(raw: object) -> list[str]:
    """Lista de ids de cargo como texto, na ordem; levanta ``SettingError`` se não for lista de ids, tiver repetido ou passar de 25."""
    if not isinstance(raw, (list, tuple)):
        raise SettingError("djroles deve ser uma lista")
    ids: list[str] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, str)) or not _ROLE_ID.fullmatch(str(item)):
            raise SettingError("djroles tem um id de cargo inválido")
        if str(item) in ids:
            raise SettingError("djroles tem cargos repetidos")
        ids.append(str(item))
    if len(ids) > MAX_DJ_ROLES:
        raise SettingError(f"djroles passa de {MAX_DJ_ROLES} cargos")
    return ids


def clean_role_ids(raw: object, role_ids: Collection[str] | None = None) -> list[str]:
    """Versão tolerante para dados antigos: descarta o que não é id, o repetido, o cargo que não existe mais (se ``role_ids``) e o excedente."""
    ids: list[str] = []
    for item in raw if isinstance(raw, (list, tuple)) else ():
        text = str(item)
        if isinstance(item, bool) or not isinstance(item, (int, str)) or not _ROLE_ID.fullmatch(text):
            continue
        if text in ids or (role_ids is not None and text not in role_ids):
            continue
        ids.append(text)
    return ids[:MAX_DJ_ROLES]


def valid_skin(value: object, known: Collection[str]) -> bool:
    """``None`` (padrão), uma skin existente ou uma skin própria do servidor (prefixo fixo) de tamanho razoável."""
    if value is None:
        return True
    if not isinstance(value, str) or not value or len(value) > SKIN_MAX_LENGTH:
        return False
    return value in known or value.startswith(CUSTOM_SKIN_PREFIX)


def _field_error(name: str, value: object, allowed: Allowed) -> str | None:
    if name == "default_player_volume":
        return None if _is_int(value) and value in VOLUMES else "volume fora da lista"
    if name in FLAG_FIELDS:
        return None if isinstance(value, bool) else f"{name} deve ser booleano"
    if name == "skin":
        return None if valid_skin(value, allowed.skins) else "skin inexistente"
    if name == "static_skin":
        return None if valid_skin(value, allowed.static_skins) else "skin estática inexistente"
    if name == "language":
        # igualdade EXATA com um locale ligado (nunca vira caminho; ``pt-br``, ``en-US `` e ``../x`` não passam) ou ``None``
        return (
            None if value is None or (isinstance(value, str) and value in allowed.languages) else "idioma inexistente"
        )
    return None


def validate_changes(changes: Mapping[str, Any], allowed: Allowed) -> dict[str, Any]:
    """As alterações de ``settings`` validadas e normalizadas; levanta ``SettingError`` no primeiro campo desconhecido ou inválido."""
    clean: dict[str, Any] = {}
    for name, value in changes.items():
        if name not in SETTINGS_FIELDS:
            raise SettingError(f"campo desconhecido: {name}")
        if name == "djroles":
            clean[name] = normalize_role_ids(value)
        elif (error := _field_error(name, value, allowed)) is not None:
            raise SettingError(error)
        else:
            clean[name] = value
    return clean


# ---------------------------------------------------------------------- formato antigo


def _legacy_value(name: str, flat: Mapping[str, Any]) -> tuple[bool, Any]:
    source = (flat.get("player_controller") or {}) if name in _LEGACY_CONTROLLER_KEYS else flat
    key = _LEGACY_CONTROLLER_KEYS.get(name, name)
    return (True, source[key]) if key in source else (False, None)


def settings_from_legacy(flat: Mapping[str, Any]) -> dict[str, Any]:
    """Os campos de ``settings`` de um documento no formato antigo (ausente = padrão; ``djroles`` sempre como texto)."""
    settings = default_settings()
    for name in LEGACY_FIELDS:
        found, value = _legacy_value(name, flat)
        if found:
            settings[name] = deepcopy(value)
    settings["djroles"] = [str(role_id) for role_id in settings["djroles"] or []]
    return settings


def _id_text(value: Any) -> str | None:
    """Id de canal/mensagem do Discord como texto de dígitos; qualquer outra coisa (vazio, texto, número negativo) vira ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value)
    return text if _ROLE_ID.fullmatch(text) and int(text) > 0 else None


def request_from_legacy(flat: Mapping[str, Any]) -> dict[str, Any]:
    """A seção ``request`` a partir de um documento antigo (canal e mensagem do canal de pedidos; sem canal, sem mensagem)."""
    controller = flat.get("player_controller") or {}
    channel = _id_text(controller.get("channel"))
    return {"channel": channel, "message_id": _id_text(controller.get("message_id")) if channel else None}


def migrate_request(legacy: Mapping[str, Any] | None) -> dict[str, Any]:
    """``request`` migrado do documento antigo do principal (``None`` = ele não tinha): canal e mensagem válidos; o bot do painel
    ainda não foi eleito (``None``)."""
    request = request_from_legacy(legacy or {})
    return {**request, "panel_bot_id": None}


def changed_request(incoming: Mapping[str, Any], seen: Mapping[str, Any]) -> dict[str, Any]:
    """Os campos do canal de pedidos que o escritor MUDOU em relação ao que leu (valor inválido levanta ``SettingError``)."""
    controller = incoming.get("player_controller") or {}
    changes: dict[str, Any] = {}
    for name in REQUEST_FIELDS:
        if name not in controller or controller[name] == seen.get(name):
            continue
        value = controller[name]
        if value is not None and _id_text(value) is None:
            raise SettingError(f"{name} deve ser um id do Discord")
        changes[name] = None if value is None else _id_text(value)
    return changes


def to_legacy(
    base: Mapping[str, Any], settings: Mapping[str, Any], request: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """O documento no formato antigo: ``base`` (o documento por bot) com ``settings`` (e o canal de pedidos, se vier) por cima."""
    flat = deepcopy(dict(base))
    controller = flat.setdefault("player_controller", {})
    if request is not None:
        for name in REQUEST_FIELDS:
            controller[name] = request.get(name)
    for name in LEGACY_FIELDS:
        value = deepcopy(settings[name])
        if name in _LEGACY_CONTROLLER_KEYS:
            controller[_LEGACY_CONTROLLER_KEYS[name]] = value
        else:
            flat[name] = value
    return flat


def changed_settings(flat: Mapping[str, Any], seen: Mapping[str, Any]) -> dict[str, Any]:
    """Os campos de ``settings`` do documento antigo que diferem do que o escritor leu (vazio = ele não mexeu em ``settings``)."""
    now = settings_from_legacy(flat)
    return {name: now[name] for name in LEGACY_FIELDS if now[name] != seen.get(name)}


# ---------------------------------------------------------------------- migração


def migrate_settings(legacy: Mapping[str, Any] | None, allowed: Allowed) -> dict[str, Any]:
    """``settings`` a partir do documento antigo do principal (``None`` = ele não tinha). Campo ausente ou inválido fica no padrão,
    EXCETO ``enable_restrict_mode`` presente e inválido, que vira ligado (falha fechada, S-V12); ``djroles`` perde só os ids ruins
    (malformados, repetidos ou de cargo que não existe mais)."""
    settings = default_settings()
    if legacy is None:
        return settings
    for name in LEGACY_FIELDS:
        found, value = _legacy_value(name, legacy)
        if not found:
            continue
        if name == "djroles":
            settings[name] = clean_role_ids(value, allowed.role_ids)
        elif _field_error(name, value, allowed) is None:
            settings[name] = deepcopy(value)
        elif name == "enable_restrict_mode":
            settings[name] = True
    settings["language"] = (
        LEGACY_LANGUAGE  # já existia um documento antigo: fica no português de hoje (o novo nasce automático)
    )
    return settings
