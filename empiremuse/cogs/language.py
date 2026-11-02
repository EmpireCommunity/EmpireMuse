# -*- coding: utf-8 -*-
"""``/language``: o idioma do bot neste servidor (``settings.language``; ``auto`` volta ao idioma do Discord do servidor).

Só em servidor. A permissão ``manage_guild`` é conferida em execução (o ``default_member_permissions`` só esconde o comando e pode ser
sobrescrito pelo dono do servidor). O autocomplete não é confiável: quem decide é o ``_field_error`` do store (igualdade exata com um
locale ligado ou ``None``). Cooldown de 10 s por servidor, para a troca não gerar rajada de edições do controller. O erro nunca ecoa o valor.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import disnake
from disnake.ext import commands

from empiremuse.i18n import localized
from empiremuse.rules.servers import SettingError
from empiremuse.utils.music.errors import GenericError

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore

logger = logging.getLogger(__name__)

AUTO = "auto"
MAX_CHOICES = 25
LANGUAGE_COOLDOWN = commands.CooldownMapping.from_cooldown(1, 10, commands.BucketType.guild)


async def language_autocomplete(inter: disnake.ApplicationCommandInteraction, current: str) -> dict[str, str]:
    """Até 25 opções ``código — nome nativo`` dos idiomas ligados, mais ``auto``; o texto digitado só filtra."""
    i18n = inter.bot.pool.i18n
    tr = await i18n.for_guild(inter.guild)
    wanted = (current or "").strip().casefold()
    options = {f"{AUTO} — {tr('language.auto')}": AUTO}
    for locale in sorted(i18n.enabled):
        options[f"{locale} — {i18n.display_name(locale)}"[:100]] = locale
    found = [(label, value) for label, value in options.items() if wanted in label.casefold()]
    return dict(found[:MAX_CHOICES])


class Language(commands.Cog):
    emoji = "🔧"
    name = "Configurações"

    def __init__(self, bot: BotCore):
        self.bot = bot

    @commands.slash_command(
        name=localized("cmd.language.name"),
        description=localized("cmd.language.description"),
        default_member_permissions=disnake.Permissions(manage_guild=True),
    )
    @commands.contexts(guild=True)
    async def language(
        self,
        inter: disnake.ApplicationCommandInteraction,
        language: str = commands.Param(
            name=localized("cmd.language.opt.language.name"),
            description=localized("cmd.language.opt.language.description"),
            autocomplete=language_autocomplete,
        ),
    ):
        await inter.response.defer(ephemeral=True)  # confirma em 3 s; a leitura do idioma e a gravação podem demorar
        i18n = self.bot.pool.i18n
        tr = await i18n.for_guild(inter.guild)

        if inter.guild is None or not inter.author.guild_permissions.manage_guild:  # a fronteira é esta checagem
            raise GenericError(tr("language.no_permission"))

        bucket = LANGUAGE_COOLDOWN.get_bucket(inter)
        if bucket is not None and (retry := bucket.update_rate_limit()):
            raise GenericError(tr("language.cooldown", seconds=int(retry) + 1))

        value = None if language == AUTO else language
        servers = self.bot.pool.servers
        try:
            before = (await servers.settings(inter.guild_id)).get("language")
            await servers.patch(inter.guild_id, "settings", {"language": value})
        except SettingError:
            raise GenericError(tr("language.invalid")) from None  # nunca ecoa o valor recebido
        except GenericError:
            raise
        except Exception:
            logger.exception("Falha ao gravar o idioma do servidor [%s]", inter.guild_id)
            raise GenericError(tr("language.unavailable")) from None

        logger.info("Idioma do servidor [%s] alterado por %s: %s -> %s", inter.guild_id, inter.author.id, before, value)
        new_tr = await i18n.for_guild(inter.guild)
        shown = new_tr("language.auto") if value is None else i18n.display_name(value)
        await inter.edit_original_message(content=new_tr("language.changed", language=shown))
        await i18n.apply_language(inter.guild_id)


def setup(bot: BotCore):
    bot.add_cog(Language(bot))
