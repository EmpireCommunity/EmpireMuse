"""Corpos das escritas de rádio e de estação (B4, contrato T-456 §4 "Rádios" e [r5-dto]), sobre ``writes.py``.

Convenções: o objeto ``values`` carrega **todos** os campos do formulário (a escrita é substituição); campo desconhecido,
faltando ou de tipo errado = ``validation_failed`` com os nomes. A versão de uma rádio ou de uma estação é o ``rev``
decimal do documento (1 a 12 dígitos), não os 16 hex dos formulários de configuração: é o que a store confere (CAS). Os
nomes dos campos são os do painel genérico (``kind``, ``channel_id``, ``bot_id``, ``estacao``, ``reserva``, ``name``…).
O servidor, a rádio e o usuário nunca vêm do corpo.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from empiremuse.api.errors import validation_failed
from empiremuse.api.writes import (
    INT,
    LINES,
    NAMES,
    TEXT,
    TEXT_OR_NULL,
    Spec,
    Write,
    optional,
    required,
    values_to_args,
)
from empiremuse.rules.radio import CHAT_FIELDS

REV = re.compile(r"[0-9]{1,12}", re.ASCII)
MAX_TOKEN = 400

SETUP_SPEC: Spec = {"kind": required(TEXT), "channel_id": required(TEXT), "bot_id": required(TEXT)}
CHAT_SPEC: Spec = {name: required(INT) for name in CHAT_FIELDS}
STATION_SPEC: Spec = {
    "estacao": required(TEXT),
    "node": optional(TEXT),
}  # ``node``: o nó de onde a estação foi escolhida
RESERVE_SPEC: Spec = {"reserva": required(TEXT_OR_NULL)}
CUSTOM_STATION_SPEC: Spec = {
    "name": required(TEXT),
    "kind": required(TEXT),
    "genre": optional(TEXT),
    "artists": optional(LINES),
    "playlists": optional(NAMES),
}


def read_rev(source: Mapping[str, Any]) -> str:
    """``expected_version`` de uma rádio ou estação: o ``rev`` decimal que a leitura mostrou."""
    version = source.get("expected_version")
    if not isinstance(version, str) or not REV.fullmatch(version):
        raise validation_failed("expected_version")
    return version


def parse_versioned(body: Mapping[str, Any], spec: Spec) -> Write:
    """``PUT``: ``{expected_version, values}``; a ``versao`` segue com os campos para o serviço."""
    return Write("", read_rev(body), values_to_args(body.get("values"), spec))


def parse_create(body: Mapping[str, Any], spec: Spec) -> Write:
    """``POST`` de criação: ``{values}``, sem versão (não há o que perder; a unicidade é a idempotência natural)."""
    return Write("", None, values_to_args(body.get("values"), spec))


def parse_reset_token(body: Mapping[str, Any]) -> str:
    """``POST /radios/{r}/reset``: ``{confirm_token}``; sem versão (o reset apaga sem recusar tela velha, T-422b/R1)."""
    token = body.get("confirm_token")
    if not isinstance(token, str) or not 0 < len(token) <= MAX_TOKEN or not token.isascii():
        raise validation_failed("confirm_token")
    return token


def read_station_version(query: Mapping[str, str]) -> Optional[str]:
    """``DELETE /radio-stations/{id}?expected_version=``."""
    return read_rev(query)
