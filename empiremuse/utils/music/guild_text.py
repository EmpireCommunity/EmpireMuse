"""Texto do catálogo no idioma do servidor, para os avisos que ainda eram fixos em português."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from empiremuse.rules.servers import SECTION_SETTINGS


async def guild_text(bot: Any, guild: Any | None, key: str, **values: str | int) -> str:
    """O texto ``key`` do catálogo no idioma do ``guild`` (``None``: DM, usa o idioma de reserva)."""
    translate = await bot.pool.i18n.for_guild(guild)
    return translate(key, **values)


def _cached_language(i18n: Any, guild: Any | None) -> str | None:
    """``settings.language`` do servidor SE o documento já está em cache (sem banco, sem ``await``); ``None`` = não se sabe."""
    servers = getattr(i18n.pool, "servers", None)
    guild_id = getattr(guild, "id", None)
    if servers is None or guild_id is None:
        return None
    settings = servers.peek_section(guild_id, SECTION_SETTINGS)
    language = settings.get("language") if isinstance(settings, Mapping) else None
    return language if isinstance(language, str) else None


def guild_text_sync(guild: Any | None, key: str, **values: str | int) -> str:
    """Igual a ``guild_text`` para código síncrono: usa o idioma escolhido em ``settings.language`` quando o documento do servidor
    já está em cache e, sem ele, segue o idioma do Discord do servidor.

    Não lê o banco (isso exige I/O assíncrono); onde houver ``await``, prefira ``guild_text``.
    """
    from empiremuse.i18n.service import get_i18n

    i18n = get_i18n()
    locale = getattr(guild, "preferred_locale", None)
    locale = getattr(locale, "value", locale)
    saved = _cached_language(i18n, guild)
    return i18n.for_locale(i18n.resolve(saved, locale if isinstance(locale, str) else None))(key, **values)
