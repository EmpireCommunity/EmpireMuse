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
        aliases=["rstc", "restrict", "restrito", "modorestrito"],
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

    nonstop_cd = commands.CooldownMapping.from_cooldown(2, 15, commands.BucketType.member)

    nonstop_mc = commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

    @has_player()
    @check_voice()
    @commands.has_guild_permissions(manage_guild=True)
    @pool_command(
        name="247",
        aliases=["nonstop"],
        only_voiced=True,
        cooldown=nonstop_cd,
        max_concurrency=nonstop_mc,
        description="Ativar/Desativar o modo 24/7 do player (Em testes).",
    )
    async def nonstop_legacy(self, ctx: CustomContext):
        await self.nonstop.callback(self=self, inter=ctx)

    @has_player()
    @check_voice()
    @commands.slash_command(
        name="247",
        description=localized("cmd.247.description"),
        default_member_permissions=disnake.Permissions(manage_guild=True),
        extras={"only_voiced": True},
        cooldown=nonstop_cd,
        max_concurrency=nonstop_mc,
    )
    @commands.contexts(guild=True)
    async def nonstop(self, inter: disnake.ApplicationCommandInteraction):
        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        player.keep_connected = not player.keep_connected

        msg = ["ativou", "♾️"] if player.keep_connected else ["desativou", "❌"]

        text = [
            f"{msg[0]} o modo 24/7 (interrupto) do player.",
            f"{msg[1]} **⠂{inter.author.mention} {msg[0]} o modo 24/7 (interrupto) do player.**",
        ]

        if not len(player.queue):
            player.queue.extend(player.played)
            player.played.clear()

        await player.process_save_queue()

        if player.current:
            await self.interaction_message(inter, txt=text, emoji=msg[1])
            return

        await self.interaction_message(inter, text)

        await player.process_next()

    autoplay_cd = commands.CooldownMapping.from_cooldown(2, 15, commands.BucketType.member)

    autoplay_mc = commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

    @has_player()
    @check_voice()
    @pool_command(
        name="autoplay",
        aliases=["ap", "aplay"],
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
