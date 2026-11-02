"""Texto do catálogo no idioma do servidor, para os avisos que ainda eram fixos em português."""

from __future__ import annotations

from typing import Any


async def guild_text(bot: Any, guild: Any | None, key: str, **values: str | int) -> str:
    """O texto ``key`` do catálogo no idioma do ``guild`` (``None``: DM, usa o idioma de reserva)."""
    translate = await bot.pool.i18n.for_guild(guild)
    return translate(key, **values)
