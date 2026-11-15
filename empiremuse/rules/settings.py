"""Regras das configurações de servidor e de usuário: prefixo, status de voz, volume, cargos de DJ e skins."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Collection, Iterable, Optional, Sequence

from empiremuse.rules.errors import (
    PREFIX_CHARS,
    PREFIX_EMPTY,
    PREFIX_LENGTH,
    ROLE_DUPLICATE,
    ROLE_INVALID,
    ROLE_LIMIT,
    STATUS_CHARS,
    STATUS_NO_PLACEHOLDER,
    STATUS_TOO_LONG,
    RuleError,
)
from empiremuse.rules.limits import (
    MAX_DJ_ROLES,
    PREFIX_MAX_LENGTH,
    VOICE_PLACEHOLDERS,
    VOICE_STATUS_MAX_LENGTH,
    VOLUMES,
)
from empiremuse.rules.text import has_control_or_format, normalize_status

_VOLUME = re.compile(r"[0-9]{1,3}", re.ASCII)
_ROLE_ID = re.compile(r"[0-9]{1,20}", re.ASCII)


def allowed_names(names: Iterable[str], ignored: str) -> tuple[str, ...]:
    """Nomes de skin que não estão na lista ``ignored`` (texto da configuração, separado por espaços)."""
    skip = ignored.split()
    return tuple(name for name in names if name not in skip)


def validate_prefix(raw: Any) -> str | RuleError:
    """Prefixo: apara as pontas; depois 1 a 5 caracteres, sem espaço em branco nem controle/formatação."""
    if not isinstance(raw, str) or not raw.strip():
        return RuleError(PREFIX_EMPTY)
    prefix = raw.strip()
    if len(prefix) > PREFIX_MAX_LENGTH:
        return RuleError(PREFIX_LENGTH, limit=PREFIX_MAX_LENGTH)
    if any(char.isspace() for char in prefix) or has_control_or_format(prefix):
        return RuleError(PREFIX_CHARS)
    return prefix


def validate_voice_status(raw: str) -> str | RuleError:
    """Status automático do canal de voz: quebras de linha viram espaço, até 496 caracteres, sem controle/formatação
    (``has_control_or_format``) e, se não for vazio, com pelo menos um placeholder válido. Vazio desativa o status."""
    text = normalize_status(raw)
    if len(text) > VOICE_STATUS_MAX_LENGTH:
        return RuleError(STATUS_TOO_LONG, limit=VOICE_STATUS_MAX_LENGTH)
    if has_control_or_format(text):
        return RuleError(STATUS_CHARS)
    if text and not any(placeholder in text for placeholder in VOICE_PLACEHOLDERS):
        return RuleError(STATUS_NO_PLACEHOLDER)
    return text


def parse_volume(raw: str) -> Optional[int]:
    """Volume padrão enviado como texto: um dos valores que o select do comando oferece, ou ``None``."""
    volume = int(raw) if _VOLUME.fullmatch(raw) else None
    return volume if volume in VOLUMES else None


def check_dj_roles(sent: Sequence[str], valid_ids: Collection[str]) -> Optional[RuleError]:
    """Lista de cargos de DJ enviada pelo painel: até 25, sem repetição e só cargos que existem no servidor."""
    if len(sent) > MAX_DJ_ROLES:
        return RuleError(ROLE_LIMIT, limit=MAX_DJ_ROLES)
    if len(set(sent)) != len(sent):
        return RuleError(ROLE_DUPLICATE)
    if any(not _ROLE_ID.fullmatch(role_id) or role_id not in valid_ids for role_id in sent):
        return RuleError(ROLE_INVALID)
    return None


def order_dj_roles(stored: Sequence[str], sent: Sequence[str]) -> list[str]:
    """Ordem estável: o que já estava gravado mantém a posição; o novo entra no fim, na ordem enviada."""
    chosen = set(sent)
    ordered = [role_id for role_id in stored if role_id in chosen]
    ordered += [role_id for role_id in sent if role_id not in set(stored)]
    return ordered


def add_dj_role(current: Sequence[str], role_id: str) -> list[str] | RuleError:
    """``current`` com o cargo no fim; recusa o repetido e o 26º (dados já acima de 25 continuam como estão)."""
    if role_id in current:
        return RuleError(ROLE_DUPLICATE)
    if len(current) >= MAX_DJ_ROLES:
        return RuleError(ROLE_LIMIT, limit=MAX_DJ_ROLES)
    return [*current, role_id]
