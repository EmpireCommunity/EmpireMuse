# -*- coding: utf-8 -*-
"""Portão dos comandos de administração (``shell``, ``hardreset``, painel do dono e afins): só ``OWNER_IDS``.

``BotCore.is_owner`` não muda (serve aos desvios de limite e cota e deixa a equipe da aplicação passar); este portão nunca o chama.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from disnake.ext import commands

from empiremuse.rules.owner_gate import OwnerGateMode, admin_owner_allowed, resolve_owner_gate

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore

logger = logging.getLogger(__name__)


def is_admin_owner(bot: BotCore, user: Any) -> bool:
    """Se ``user`` (só precisa de ``.id``) passa no portão deste bot. Síncrono; sem ``owner_gate`` o portão fica fechado."""
    mode = getattr(bot, "owner_gate", OwnerGateMode.DENY)
    app_owner = getattr(bot, "owner", None)
    return admin_owner_allowed(mode, user.id, bot.env_owner_ids, getattr(app_owner, "id", None))


def admin_owner_only():
    """``commands.check`` no lugar de ``commands.is_owner()``; recusa com ``commands.NotOwner`` (tratado como antes)."""

    async def predicate(ctx) -> bool:
        if not is_admin_owner(ctx.bot, ctx.author):
            raise commands.NotOwner("You do not own this bot.")
        return True

    return commands.check(predicate)


def log_owner_gate_boot(config: dict) -> OwnerGateMode:
    """O ERROR do modo ``DENY`` e o WARNING do ``FALLBACK``, uma vez (o valor de ``OWNER_IDS`` nunca entra no texto)."""
    from empiremuse.utils.client import parse_owner_ids  # import tardio: ``client`` importa este módulo

    raw = config.get("OWNER_IDS")
    mode = resolve_owner_gate(raw, parse_owner_ids(raw))
    if mode is OwnerGateMode.DENY:
        logger.error(
            "OWNER_IDS está preenchido, mas não tem nenhum ID válido (o separador é ||): nenhum comando de dono vai funcionar."
        )
    elif mode is OwnerGateMode.FALLBACK:
        logger.warning("OWNER_IDS vazio: só o dono da aplicação de cada bot usa os comandos de dono.")
    return mode
