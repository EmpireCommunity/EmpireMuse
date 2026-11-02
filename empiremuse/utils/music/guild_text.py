"""Texto do catálogo no idioma do servidor, para os avisos que ainda eram fixos em português."""

from __future__ import annotations

from typing import Any


async def guild_text(bot: Any, guild: Any | None, key: str, **values: str | int) -> str:
    """O texto ``key`` do catálogo no idioma do ``guild`` (``None``: DM, usa o idioma de reserva)."""
    translate = await bot.pool.i18n.for_guild(guild)
    return translate(key, **values)


def guild_text_sync(guild: Any | None, key: str, **values: str | int) -> str:
    """Igual a ``guild_text`` para código síncrono: segue o idioma do Discord do servidor.

    Não lê o idioma escolhido em ``settings.language`` (isso exige I/O assíncrono); onde houver ``await``, prefira ``guild_text``.
    """
    from empiremuse.i18n.service import get_i18n

    i18n = get_i18n()
    locale = getattr(guild, "preferred_locale", None)
    locale = getattr(locale, "value", locale)
    return i18n.for_locale(i18n.resolve(None, locale if isinstance(locale, str) else None))(key, **values)
