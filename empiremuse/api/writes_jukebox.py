"""Corpos das escritas da Jukebox (B4, contrato T-456 §4 "Jukebox" e [r5-dto]): ``PUT /jukebox`` (as duas opções) e
``POST /jukebox/tasks`` (configurar ou reiniciar, com confirmação em dois passos quando o canal existente é limpo).

Só se converte o tipo (``writes.values_to_args``); as regras (canal do servidor, permissões do bot, título do fórum) ficam
em ``JukeboxService``. O usuário, o servidor e o bot nunca vêm do corpo.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from empiremuse.api.errors import validation_failed
from empiremuse.api.writes import BOOL, TEXT, Spec, optional, read_choice, read_version, required, values_to_args
from empiremuse.panel.services.jukebox_options import AUTO_DELETE, TEXT_REQUESTS

TASK_CONFIGURE, TASK_RESET = "configure", "reset"
TASK_ACTIONS = (TASK_CONFIGURE, TASK_RESET)
OPTIONS_SPEC: Spec = {AUTO_DELETE: required(BOOL), TEXT_REQUESTS: required(BOOL)}
CONFIGURE_SPEC: Spec = {
    "modo": required(TEXT),
    "canal": optional(TEXT),
    "titulo_forum": optional(TEXT),
    "tipo": optional(TEXT),
    "categoria": optional(TEXT),
}
MAX_TOKEN = 400


@dataclass(frozen=True)
class TaskRequest:
    action: str
    version: str
    args: dict[str, list[str]]  # os campos do formulário de configurar (vazio no reset)
    confirm_token: Optional[str]  # ``None`` = passo 1 (pede a confirmação); texto = passo 2


def parse_options_write(body: Mapping[str, Any]) -> tuple[str, dict[str, list[str]]]:
    """``PUT /jukebox``: ``{expected_version, values:{jukebox_auto_delete, jukebox_text_requests}}``."""
    return read_version(body), values_to_args(body.get("values"), OPTIONS_SPEC)


def _read_token(body: Mapping[str, Any]) -> Optional[str]:
    if "confirm_token" not in body or body["confirm_token"] is None:
        return None
    token = body["confirm_token"]
    if not isinstance(token, str) or not 0 < len(token) <= MAX_TOKEN or not token.isascii():
        raise validation_failed("confirm_token")
    return token


def parse_task_request(body: Mapping[str, Any]) -> TaskRequest:
    """``POST /jukebox/tasks``: ``{expected_version, action, values?, confirm_token?}``."""
    action = read_choice(body, "action", TASK_ACTIONS)
    version = read_version(body)
    args = values_to_args(body.get("values"), CONFIGURE_SPEC) if action == TASK_CONFIGURE else {}
    if action == TASK_RESET and body.get("values") not in (None, {}):
        raise validation_failed("values")
    return TaskRequest(action, version, args, _read_token(body))
