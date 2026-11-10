# -*- coding: utf-8 -*-
"""Portão do banimento para TODA interação (E5, MJ-E1, MJ-E3, MJ-E5, contrato §7).

O disnake entrega interações de componente e de modal direto ao ``ViewStore``/``ModalStore`` (``ConnectionState.parse_interaction_create``),
sem passar pelo ``application_command_check``; o autocompletar não roda check e o modal não tem ``interaction_check``. Um ``if`` por view
não fecha (dezenas de views e ouvintes). Por isso o portão envolve o ``parsers["INTERACTION_CREATE"]`` de cada bot do pool: um ponto só.

É API privada do disnake: a versão fica fixada (``disnake==2.12.2``) e o teste R8.1 usa o parser real, para falhar numa atualização. Se o
parser não existir, a subida falha (nunca segue sem o portão).

O ID vem do JSON bruto (``member.user.id`` ou ``user.id``) e passa por ``is_blocked`` (ID inválido nega). Banido: o parser original NÃO roda
(nenhum callback), a resposta é efêmera, curta e sem motivo (D4); o autocompletar recebe lista vazia. A recusa não loga o ID (flood, m2).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from empiremuse.rules.user_ban import (
    GATED_INTERACTION_TYPES,
    INTERACTION_AUTOCOMPLETE,
    erasure_exempt,
)

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore
    from empiremuse.utils.user_ban import UserBans

logger = logging.getLogger(__name__)

PARSER_KEY = "INTERACTION_CREATE"
RESPONSE_CHANNEL_MESSAGE = 4
RESPONSE_AUTOCOMPLETE_RESULT = 8
EPHEMERAL_FLAG = 64
BLOCKED_TEXT_KEY = "erasure.ban.blocked"


def raw_author_id(data: dict[str, Any]) -> object:
    """O ID de quem interagiu, como veio no JSON: ``member.user.id`` (servidor) ou ``user.id`` (DM); ``None`` se faltar."""
    member = data.get("member")
    user = member.get("user") if isinstance(member, dict) else None
    if not isinstance(user, dict):
        user = data.get("user")
    return user.get("id") if isinstance(user, dict) else None


class InteractionGate:
    def __init__(self, bot: BotCore, bans: UserBans, original: Callable[[dict[str, Any]], None]) -> None:
        self.bot = bot
        self.bans = bans
        self.original = original
        self._tasks: set[asyncio.Task] = set()

    def __call__(self, data: dict[str, Any]) -> None:
        interaction_type = data.get("type")
        if interaction_type not in GATED_INTERACTION_TYPES:
            return self.original(data)
        payload = data.get("data")
        payload = payload if isinstance(payload, dict) else {}
        exempt = erasure_exempt(interaction_type, payload.get("type"), payload.get("name"), payload.get("custom_id"))
        if not self.bans.is_blocked(raw_author_id(data), exempt=exempt):
            return self.original(data)
        self._refuse(data, interaction_type)
        return None

    def _refuse(self, data: dict[str, Any], interaction_type: object) -> None:
        interaction_id, token = data.get("id"), data.get("token")
        if not interaction_id or not isinstance(token, str):
            return
        if interaction_type == INTERACTION_AUTOCOMPLETE:
            response_type, body = RESPONSE_AUTOCOMPLETE_RESULT, {"choices": []}
        else:
            pool = self.bot.pool
            tr = pool.i18n.for_locale(pool.i18n.resolve(None, data.get("locale")))
            response_type, body = RESPONSE_CHANNEL_MESSAGE, {"content": tr(BLOCKED_TEXT_KEY), "flags": EPHEMERAL_FLAG}
        task = asyncio.ensure_future(self._respond(interaction_id, token, response_type, body))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _respond(self, interaction_id: object, token: str, response_type: int, body: dict[str, Any]) -> None:
        try:
            await self.bot.http.create_interaction_response(interaction_id, token, type=response_type, data=body)
        except Exception as exc:  # noqa: BLE001 - a resposta do bloqueio é só cortesia; o callback já foi barrado
            logger.debug("Resposta ao usuário bloqueado não enviada (%s)", type(exc).__name__)


def install_interaction_gate(bot: BotCore, bans: UserBans) -> InteractionGate:
    """Envolve o parser de ``INTERACTION_CREATE`` do bot. Parser ausente = erro na subida (API privada do disnake fixado)."""
    parsers = getattr(getattr(bot, "_connection", None), "parsers", None)
    original = parsers.get(PARSER_KEY) if parsers is not None else None
    if not callable(original):
        raise RuntimeError(
            "disnake mudou: parser de INTERACTION_CREATE não encontrado; o portão do banimento não pode ser instalado"
        )
    gate = InteractionGate(bot, bans, original)
    parsers[PARSER_KEY] = gate
    return gate
