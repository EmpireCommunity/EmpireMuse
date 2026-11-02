# -*- coding: utf-8 -*-
"""O tradutor das views e modais de ``interactions``: quem cria a view passa ``tr`` (o do servidor, já resolvido); sem ele, a
view nasce com o idioma do Discord do servidor (síncrono, sem ir ao banco) e os callbacks pegam o do servidor de verdade.

Dublês de teste sem ``pool.i18n`` usam o ``I18n`` do processo (``get_i18n``), como o painel faz."""

from __future__ import annotations

from typing import Any

from empiremuse.i18n import get_i18n
from empiremuse.rules.i18n import Translator


def i18n_of(bot: Any) -> Any:
    """``bot.pool.i18n`` (o único do processo) ou, sem pool, o mesmo objeto por ``get_i18n()``."""
    i18n = getattr(getattr(bot, "pool", None), "i18n", None)
    return i18n if i18n is not None else get_i18n()


def sync_translator(bot: Any, guild: Any) -> Translator:
    """O tradutor pelo idioma do Discord do servidor, sem esperar o banco: para montar a view na hora em que ela nasce."""
    i18n = i18n_of(bot)
    return i18n.for_locale(i18n.resolve(None, getattr(guild, "preferred_locale", None)))


async def guild_translator(bot: Any, guild: Any) -> Translator:
    """O tradutor do servidor (``settings.language``, cache do store): para as respostas dos callbacks."""
    return await i18n_of(bot).for_guild(guild)
