"""Regras puras do banimento de usuário (E5, contrato §7): quem é bloqueado, a isenção do apagamento, o registro e o limite de avisos.

Só biblioteca padrão e sem I/O: o relógio entra por parâmetro. Todo canal (interação, mensagem, painel, RPC, scrobble) chama ``is_blocked``;
nenhum decide por conta própria. O armazenamento e a recarga ficam em ``empiremuse/utils/user_ban.py``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Container, Mapping
from dataclasses import dataclass
from typing import Any

from empiremuse.rules.erasure import ERASE_COMMAND_NAMES, ERASE_COMPONENT_PREFIXES, SNOWFLAKE_LIMIT
from empiremuse.rules.usage import clean_text

REASON_LIMIT = 200
USER_BANS_BOOT_WAIT_S = 10.0  # tempo-limite da carga antes de conectar ao gateway (D2)
USER_BANS_RETRY_S = 60.0  # nova tentativa da carga enquanto o banco estiver fora (D2)
BLOCKED_WARNING_WINDOW_S = 3600.0  # Q13: no máximo 1 aviso por usuário por hora
BLOCKED_WARNING_MAX_USERS = 1024  # memória do limitador: limitada
RPC_BLOCKED_REASON = "Esta conta não pode usar o bot."  # texto fixo (o relé RPC não tem catálogo); sem motivo (D4)

INTERACTION_APPLICATION_COMMAND = 2
INTERACTION_MESSAGE_COMPONENT = 3
INTERACTION_AUTOCOMPLETE = 4
INTERACTION_MODAL_SUBMIT = 5
GATED_INTERACTION_TYPES = frozenset(
    {INTERACTION_APPLICATION_COMMAND, INTERACTION_MESSAGE_COMPONENT, INTERACTION_AUTOCOMPLETE, INTERACTION_MODAL_SUBMIT}
)
CHAT_INPUT_COMMAND = 1

BAN_ACTION = "ban"
UNBAN_ACTION = "unban"
BAN_COMMAND_NAMES = {BAN_ACTION: "banuser", UNBAN_ACTION: "unbanuser"}

_ASCII_DIGITS = frozenset("0123456789")
_MAX_ID_DIGITS = 20


def as_user_id(value: object) -> int | None:
    """O ID como inteiro positivo de até 64 bits, ou ``None``: ``None``, ``bool``, zero, negativo, texto não numérico (só dígitos ASCII)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        if not value or len(value) > _MAX_ID_DIGITS or not set(value) <= _ASCII_DIGITS:
            return None
        number = int(value)
    else:
        return None
    return number if 0 < number < SNOWFLAKE_LIMIT else None


def is_blocked(user_id: object, banned: Container[int], owners: Container[int], *, exempt: bool = False) -> bool:
    """O usuário está bloqueado? Regra única do portão (``str`` vira ``int`` aqui: ``PanelUser.id`` é texto, MJ-E3).

    ID que não é snowflake nega (``True``); dono nunca é bloqueado; ``exempt`` (isenção do apagamento, D1) libera; senão vale a lista.
    """
    uid = as_user_id(user_id)
    if uid is None:
        return True
    if uid in owners or exempt:
        return False
    return uid in banned


def erasure_exempt(interaction_type: object, command_type: object, command_name: object, custom_id: object) -> bool:
    """A interação é o autoatendimento de apagar dados (D1)? Só (a) o slash com nome exato ou (b) botão com um dos prefixos com ``:``.

    ``erasure_owner_*`` (pedido do dono), nome parecido e ``custom_id`` sem os dois-pontos NÃO entram.
    """
    if interaction_type == INTERACTION_APPLICATION_COMMAND:
        return (
            command_type == CHAT_INPUT_COMMAND
            and isinstance(command_name, str)
            and command_name in ERASE_COMMAND_NAMES.values()
        )
    if interaction_type == INTERACTION_MESSAGE_COMPONENT:
        return isinstance(custom_id, str) and custom_id.startswith(ERASE_COMPONENT_PREFIXES)
    return False


@dataclass(frozen=True)
class UserBanRecord:
    user_id: int
    banned_at: int  # milissegundos, UTC
    reason: str
    by: int  # ID do dono


def new_user_ban_record(user_id: int, reason: object, by: int, now_ms: int) -> UserBanRecord:
    """O registro com o motivo já limpo (``clean_text``, até 200); sem nome do usuário."""
    return UserBanRecord(user_id, now_ms, clean_text(reason, REASON_LIMIT), by)


def user_ban_to_doc(record: UserBanRecord) -> dict[str, Any]:
    """O documento (``_id`` é o ID do usuário, posto pelo banco): só ``banned_at``, ``reason`` e ``by``."""
    return {"banned_at": record.banned_at, "reason": record.reason, "by": record.by}


def _int_or_zero(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def user_ban_from_doc(doc: Mapping[str, Any]) -> UserBanRecord | None:
    """O banimento lido do banco; ``None`` se o ``_id`` não for um ID de usuário (dado de fora nunca é confiável)."""
    user_id = as_user_id(doc.get("_id"))
    if user_id is None:
        return None
    return UserBanRecord(
        user_id,
        _int_or_zero(doc.get("banned_at")),
        clean_text(doc.get("reason"), REASON_LIMIT),
        _int_or_zero(doc.get("by")),
    )


TARGET_OWNER = "owner"
TARGET_BOT = "bot"
TARGET_INVALID = "invalid"


def ban_target_error(target_id: object, owners: Container[int], bot_ids: Container[int]) -> str | None:
    """Por que este alvo não pode ser banido: ``invalid`` (não é ID), ``owner`` (``env_owner_ids``) ou ``bot`` (bot do pool); senão ``None``."""
    uid = as_user_id(target_id)
    if uid is None:
        return TARGET_INVALID
    if uid in owners:
        return TARGET_OWNER
    if uid in bot_ids:
        return TARGET_BOT
    return None


@dataclass(frozen=True)
class BanCommand:
    action: str  # BAN_ACTION | UNBAN_ACTION
    target: str  # argumento ainda por validar (``owner_target_id``)
    reason: str  # só no ban; o resto da linha, sem validar


def parse_ban_command(content: str, prefix: str) -> BanCommand | None:
    """``<prefixo>banuser <id> [motivo]`` e ``<prefixo>unbanuser <id>``: nome exato; outro nome, outro prefixo ou separador estranho = ``None``.

    O alvo é a primeira palavra (espaço ou tabulação ASCII); o motivo é o resto, aparado. Quem valida o ID é ``owner_target_id``.
    """
    if not prefix or not content.startswith(prefix):
        return None
    body = content[len(prefix) :]
    for action, name in BAN_COMMAND_NAMES.items():
        if not body.startswith(name):
            continue
        rest = body[len(name) :]
        if rest and rest[0] not in " \t":
            continue
        target, _, reason = rest.strip(" \t").partition(" ")
        return BanCommand(action, target, reason.strip(" \t") if action == BAN_ACTION else "")
    return None


class WarningLimiter:
    """Q13: no máximo 1 aviso por usuário por janela, com memória limitada (vence os mais antigos). Relógio injetável."""

    def __init__(
        self,
        *,
        window_s: float = BLOCKED_WARNING_WINDOW_S,
        max_users: int = BLOCKED_WARNING_MAX_USERS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window_s = window_s
        self._max_users = max_users
        self._clock = clock
        self._last: dict[int, float] = {}

    def allow(self, user_id: int) -> bool:
        """Pode avisar este usuário agora? Se sim, já registra o aviso."""
        now = self._clock()
        last = self._last.get(user_id)
        if last is not None and now - last < self._window_s:
            return False
        self._last.pop(user_id, None)
        if len(self._last) >= self._max_users:
            self._prune(now)
        self._last[user_id] = now
        return True

    def _prune(self, now: float) -> None:
        for uid in [uid for uid, at in self._last.items() if now - at >= self._window_s]:
            del self._last[uid]
        while len(self._last) >= self._max_users:  # ainda cheio: sai o mais antigo (ordem de inserção)
            del self._last[next(iter(self._last))]

    def __len__(self) -> int:
        return len(self._last)
