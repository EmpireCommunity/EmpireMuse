# -*- coding: utf-8 -*-
"""Regra pura do portão dos comandos de administração: quem passa depende só de ``OWNER_IDS`` e, sem ele, do dono da aplicação."""

from __future__ import annotations

from collections.abc import Collection
from enum import Enum
from typing import Optional

OWNER_IDS_SEPARATOR = "||"


class OwnerGateMode(Enum):
    ENV = "env"  # há ao menos um ID válido: só eles passam
    DENY = "deny"  # variável preenchida sem nenhum ID válido: ninguém passa
    FALLBACK = "fallback"  # variável vazia ou ausente: só o dono da aplicação do bot


def _items(raw: Optional[str]) -> list[str]:
    """Os itens não brancos de ``OWNER_IDS`` (o único lugar que conhece o separador)."""
    return [item for item in (item.strip() for item in str(raw or "").split(OWNER_IDS_SEPARATOR)) if item]


def split_owner_ids(raw: Optional[str]) -> tuple[list[int], int]:
    """Os IDs numéricos de ``OWNER_IDS`` e quantos itens não brancos não eram um ID (descartados, sem o valor)."""
    ids: list[int] = []
    invalid = 0
    for item in _items(raw):
        try:
            ids.append(int(item))
        except ValueError:
            invalid += 1
    return ids, invalid


def parse_owner_ids(raw: Optional[str]) -> list[int]:
    """Só os IDs numéricos de ``OWNER_IDS`` (pura e sem log: o aviso dos descartados é um só, no boot)."""
    return split_owner_ids(raw)[0]


def resolve_owner_gate(raw: Optional[str], parsed_ids: Collection[int]) -> OwnerGateMode:
    """O modo do portão. "Preenchida" é algum item não branco entre os ``||`` (um valor inválido nunca vira ``FALLBACK``)."""
    if parsed_ids:
        return OwnerGateMode.ENV
    return OwnerGateMode.DENY if _items(raw) else OwnerGateMode.FALLBACK


def admin_owner_allowed(
    mode: OwnerGateMode, user_id: int, env_ids: Collection[int], app_owner_id: Optional[int]
) -> bool:
    """Se ``user_id`` passa no modo. Equipe da aplicação nunca passa; ``app_owner_id`` ``None`` (appinfo não lido) nega."""
    if mode is OwnerGateMode.ENV:
        return user_id in env_ids
    if mode is OwnerGateMode.FALLBACK:
        return app_owner_id is not None and user_id == app_owner_id
    return False
