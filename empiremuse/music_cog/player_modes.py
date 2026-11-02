# -*- coding: utf-8 -*-

import disnake
from disnake.ext import commands

from empiremuse.i18n import localized
from empiremuse.utils.music.checks import (
    check_voice,
    has_player,
    is_dj,
)
from empiremuse.utils.music.models import (
    LavalinkPlayer,
)
from empiremuse.utils.others import (
    CustomContext,
    pool_command,
)

# O corpo da classe usa estes nomes, então eles precisam existir neste módulo.


class PlayerModesMixin:
    restrict_cd = commands.CooldownMapping.from_cooldown(2, 7, commands.BucketType.member)

    restrict_mc = commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

    @is_dj()
    @has_player()
    @check_voice()
    @pool_command(
        name="restrictmode",
        aliases=["rstc", "restrito"],
        only_voiced=True,
        cooldown=restrict_cd,
        max_concurrency=restrict_mc,
        description="Ativar/Desativar o modo restrito de comandos que requer DJ/Staff.",
    )
    async def restrict_mode_legacy(self, ctx: CustomContext):
        await self.restrict_mode.callback(self=self, inter=ctx)

    @is_dj()
    @has_player()
    @check_voice()
    @commands.slash_command(
        description=localized("cmd.restrict_mode.description"),
        extras={"only_voiced": True},
        cooldown=restrict_cd,
        max_concurrency=restrict_mc,
    )
    @commands.contexts(guild=True)
    async def restrict_mode(self, inter: disnake.ApplicationCommandInteraction):
        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        player.restrict_mode = not player.restrict_mode

        msg = ["ativou", "🔐"] if player.restrict_mode else ["desativou", "🔓"]

        text = [
            f"{msg[0]} o modo restrito de comandos do player (que requer DJ/Staff).",
            f"{msg[1]} **⠂{inter.author.mention} {msg[0]} o modo restrito de comandos do player (que requer DJ/Staff).**",
        ]

        await self.interaction_message(inter, text, emoji=msg[1])

    autoplay_cd = commands.CooldownMapping.from_cooldown(2, 15, commands.BucketType.member)

    autoplay_mc = commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

    @has_player()
    @check_voice()
    @pool_command(
        name="autoplay",
        aliases=["ap"],
        only_voiced=True,
        cooldown=autoplay_cd,
        max_concurrency=autoplay_mc,
        description="Ativar/Desativar a reprodução automática ao acabar as músicas da fila.",
    )
    async def autoplay_legacy(self, ctx: CustomContext):
        await self.autoplay.callback(self=self, inter=ctx)

    @has_player()
    @check_voice()
    @commands.slash_command(
        name="autoplay",
        description=localized("cmd.autoplay.description"),
        extras={"only_voiced": True},
        cooldown=autoplay_cd,
        max_concurrency=autoplay_mc,
    )
    @commands.contexts(guild=True)
    async def autoplay(self, inter: disnake.ApplicationCommandInteraction):
        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        player.autoplay = not player.autoplay

        msg = ["ativou", "🔄"] if player.autoplay else ["desativou", "❌"]

        text = [
            f"{msg[0]} a reprodução automática.",
            f"{msg[1]} **⠂{inter.author.mention} {msg[0]} a reprodução automática.**",
        ]

        if player.current:
            await self.interaction_message(inter, txt=text, emoji=msg[1])
            return

        await self.interaction_message(inter, text)

        await player.process_next()
