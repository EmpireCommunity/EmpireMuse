"""Porta única das favoritas do servidor; nunca confia no ator sintético do painel."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool

FETCH_TIMEOUT_S = 5.0


async def can_manage_guild(pool: BotPool, guild_id: int, user_id: int) -> bool:
    for bot in pool.get_all_bots():
        guild = bot.get_guild(guild_id)
        if guild is None:
            continue
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await asyncio.wait_for(guild.fetch_member(user_id), FETCH_TIMEOUT_S)
            except Exception:  # noqa: BLE001 - falha de consulta nega; nenhum dado pessoal no log
                continue
        if member is None:
            continue
        permissions = member.guild_permissions
        return bool(guild.owner_id == user_id or permissions.manage_guild or permissions.administrator)
    return False
