# -*- coding: utf-8 -*-
import asyncio

import disnake
from disnake.ext import commands

from empiremuse.i18n import localized
from empiremuse.utils.music.checks import (
    check_voice,
    has_source,
    is_dj,
    check_pool_bots,
    check_stage_topic,
    check_queue_loading,
)
from empiremuse.utils.music.converters import time_format, string_to_seconds, percentage, md_link
from empiremuse.utils.music.errors import (
    GenericError,
)
from empiremuse.utils.music.interactions import (
    VolumeInteraction,
)
from empiremuse.utils.music.models import (
    LavalinkPlayer,
)
from empiremuse.utils.others import (
    CustomContext,
    pool_command,
)

# O corpo da classe usa estes nomes, então eles precisam existir neste módulo.


class PlaybackControlsMixin:
    volume_cd = commands.CooldownMapping.from_cooldown(1, 7, commands.BucketType.member)

    volume_mc = commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

    @is_dj()
    @has_source()
    @check_voice()
    @pool_command(
        name="volume",
        description="Ajustar volume da música.",
        aliases=["vol", "v"],
        only_voiced=True,
        cooldown=volume_cd,
        max_concurrency=volume_mc,
        usage="{prefix}{cmd} [nivel]\nEx: {prefix}{cmd} 50",
    )
    async def volume_legacy(self, ctx: CustomContext, level: int):
        if not 4 < level < 151:
            raise GenericError("**Volume inválido! escolha entre 5 a 150**", self_delete=7)

        await self.volume.callback(self=self, inter=ctx, value=int(level))

    @is_dj()
    @has_source()
    @check_voice()
    @commands.slash_command(
        description=localized("cmd.volume.description"),
        extras={"only_voiced": True},
        cooldown=volume_cd,
        max_concurrency=volume_mc,
    )
    @commands.contexts(guild=True)
    async def volume(
        self,
        inter: disnake.ApplicationCommandInteraction,
        *,
        value: int = commands.Param(
            name=localized("cmd.volume.opt.level.name"),
            description=localized("cmd.volume.opt.level.description"),
            min_value=5.0,
            max_value=150.0,
        ),
    ):
        try:
            bot = inter.music_bot
            guild = inter.music_guild
        except AttributeError:
            bot = inter.bot
            guild = inter.guild

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        embed = disnake.Embed(color=disnake.Colour.red())

        if value is None:
            view = VolumeInteraction(inter)

            embed.colour = self.bot.get_color(guild.me)
            embed.description = "**Selecione o nível do volume abaixo:**"

            try:
                if bot.user.id != self.bot.user.id:
                    embed.set_footer(
                        text=f"Bot selecionado: {bot.user.display_name}", icon_url=bot.user.display_avatar.url
                    )
            except AttributeError:
                pass

            await inter.send(embed=embed, ephemeral=await self.is_jukebox_channel(inter), view=view)
            await view.wait()
            if view.volume is None:
                return

            value = view.volume

        elif not 4 < value < 151:
            raise GenericError("O volume deve estar entre **5** a **150**.")

        await player.set_volume(value)

        txt = [f"ajustou o volume para **{value}%**", f"🔊 **⠂{inter.author.mention} ajustou o volume para {value}%**"]
        await self.interaction_message(inter, txt, emoji="🔊")

    pause_resume_cd = commands.CooldownMapping.from_cooldown(2, 7, commands.BucketType.member)

    pause_resume_mc = commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

    @is_dj()
    @has_source()
    @check_voice()
    @pool_command(
        name="pause",
        aliases=["pausar"],
        description="Pausar a música.",
        only_voiced=True,
        cooldown=pause_resume_cd,
        max_concurrency=pause_resume_mc,
    )
    async def pause_legacy(self, ctx: CustomContext):
        await self.pause.callback(self=self, inter=ctx)

    @is_dj()
    @has_source()
    @check_voice()
    @commands.slash_command(
        description=localized("cmd.pause.description"),
        extras={"only_voiced": True},
        cooldown=pause_resume_cd,
        max_concurrency=pause_resume_mc,
    )
    @commands.contexts(guild=True)
    async def pause(self, inter: disnake.ApplicationCommandInteraction):
        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        if player.paused:
            raise GenericError("**A música já está pausada.**")

        await player.set_pause(True)

        txt = ["pausou a música.", f"⏸️ **⠂{inter.author.mention} pausou a musica.**"]

        await self.interaction_message(inter, txt, rpc_update=True, emoji="⏸️")
        await player.update_stage_topic()

    @is_dj()
    @has_source()
    @check_voice()
    @pool_command(
        name="resume",
        aliases=["unpause", "retomar"],
        description="Retomar/Despausar a música.",
        only_voiced=True,
        cooldown=pause_resume_cd,
        max_concurrency=pause_resume_mc,
    )
    async def resume_legacy(self, ctx: CustomContext):
        await self.resume.callback(self=self, inter=ctx)

    @is_dj()
    @has_source()
    @check_voice()
    @commands.slash_command(
        description=localized("cmd.resume.description"),
        extras={"only_voiced": True},
        cooldown=pause_resume_cd,
        max_concurrency=pause_resume_mc,
    )
    @commands.contexts(guild=True)
    async def resume(self, inter: disnake.ApplicationCommandInteraction):
        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        if not player.paused:
            raise GenericError("**A música não está pausada.**")

        await player.set_pause(False)

        txt = ["retomou a música.", f"▶️ **⠂{inter.author.mention} despausou a música.**"]
        await self.interaction_message(inter, txt, rpc_update=True, emoji="▶️")
        await player.update_stage_topic()

    seek_cd = commands.CooldownMapping.from_cooldown(2, 10, commands.BucketType.member)

    seek_mc = commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

    @check_stage_topic()
    @is_dj()
    @check_queue_loading()
    @has_source()
    @check_voice()
    @pool_command(
        name="seek",
        aliases=["sk"],
        description="Avançar/Retomar a música para um tempo específico.",
        only_voiced=True,
        cooldown=seek_cd,
        max_concurrency=seek_mc,
        usage="{prefix}{cmd} [tempo]\nEx 1: {prefix}{cmd} 10 (tempo 0:10)\nEx 2: {prefix}{cmd} 1:45 (tempo 1:45)",
    )
    async def seek_legacy(self, ctx: CustomContext, *, position: str):
        await self.seek.callback(self=self, inter=ctx, position=position)

    @check_stage_topic()
    @is_dj()
    @check_queue_loading()
    @has_source()
    @check_voice()
    @commands.slash_command(
        description=localized("cmd.seek.description"),
        extras={"only_voiced": True},
        cooldown=seek_cd,
        max_concurrency=seek_mc,
    )
    @commands.contexts(guild=True)
    async def seek(
        self,
        inter: disnake.ApplicationCommandInteraction,
        position: str = commands.Param(
            name=localized("cmd.seek.opt.time.name"), description=localized("cmd.seek.opt.time.description")
        ),
    ):
        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        if player.current.is_stream:
            raise GenericError("**Você não pode usar esse comando em uma livestream.**")

        position = position.split(" | ")[0].replace(" ", ":")

        seconds = string_to_seconds(position)

        if seconds is None:
            raise GenericError(
                "**Você usou um tempo inválido! Use segundos (1 ou 2 digitos) ou no formato (minutos):(segundos)**"
            )

        milliseconds = seconds * 1000

        if milliseconds < 0:
            milliseconds = 0

        if milliseconds > player.position:
            emoji = "⏩"

            txt = [
                f"avançou o tempo da música para: `{time_format(milliseconds)}`",
                f"{emoji} **⠂{inter.author.mention} avançou o tempo da música para:** `{time_format(milliseconds)}`",
            ]

        else:
            emoji = "⏪"

            txt = [
                f"voltou o tempo da música para: `{time_format(milliseconds)}`",
                f"{emoji} **⠂{inter.author.mention} voltou o tempo da música para:** `{time_format(milliseconds)}`",
            ]

        await player.seek(milliseconds)

        if player.paused:
            await player.set_pause(False)

        await self.interaction_message(inter, txt, emoji=emoji)

        await asyncio.sleep(2)
        await player.update_stage_topic()
        await player.process_rpc()

    @seek.autocomplete("time")
    async def seek_suggestions(self, inter: disnake.Interaction, query: str):
        try:
            if not inter.author.voice:
                return
        except AttributeError:
            pass

        if not self.bot.bot_ready or not self.bot.is_ready():
            return []

        if query:
            return [time_format(string_to_seconds(query) * 1000)]

        try:
            await check_pool_bots(inter, only_voiced=True)
            bot = inter.music_bot
        except Exception:
            return

        try:
            player: LavalinkPlayer = bot.music.players[inter.guild_id]
        except KeyError:
            return

        if not player.current or player.current.is_stream:
            return

        seeks = []

        if player.current.duration >= 90000:
            times = [int(n * 0.5 * 10) for n in range(20)]
        else:
            times = [int(n * 1 * 10) for n in range(20)]

        for p in times:
            percent = percentage(p, player.current.duration)
            seeks.append(f"{time_format(percent)} | {p}%")

        return seeks

    loop_cd = commands.CooldownMapping.from_cooldown(3, 5, commands.BucketType.member)

    loop_mc = commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

    @is_dj()
    @has_source()
    @check_voice()
    @pool_command(
        aliases=["repetir"],
        description=f"Selecionar modo de repetição entre: música atual / fila / desativar / quantidade (usando números).",
        only_voiced=True,
        cooldown=loop_cd,
        max_concurrency=loop_mc,
        usage="{prefix}{cmd} <quantidade|modo>\nEx 1: {prefix}{cmd} 1\nEx 2: {prefix}{cmd} queue",
    )
    async def loop(self, ctx: CustomContext, mode: str = None):
        if not mode:
            embed = disnake.Embed(
                description="**Selecione um modo de repetição:**", color=self.bot.get_color(ctx.guild.me)
            )

            msg = await ctx.send(
                ctx.author.mention,
                embed=embed,
                components=[
                    disnake.ui.Select(
                        placeholder="Selecione uma opção:",
                        custom_id="loop_mode_legacy",
                        options=[
                            disnake.SelectOption(label="Música Atual", value="current"),
                            disnake.SelectOption(label="Fila do player", value="queue"),
                            disnake.SelectOption(label="Desativar repetição", value="off"),
                        ],
                    )
                ],
            )

            try:
                select: disnake.MessageInteraction = await self.bot.wait_for(
                    "dropdown", timeout=30, check=lambda i: i.message.id == msg.id and i.author == ctx.author
                )
            except asyncio.TimeoutError:
                embed.description = "Tempo de seleção esgotado!"
                try:
                    await msg.edit(embed=embed, view=None)
                except Exception:
                    pass
                return

            mode = select.data.values[0]
            ctx.store_message = msg

        if mode.isdigit():
            if len(mode) > 2 or int(mode) > 10:
                raise GenericError(f"**Quantidade inválida: {mode}**\n`Quantidade máxima permitida: 10`")

            await self.loop_amount.callback(self=self, inter=ctx, value=int(mode))
            return

        if mode not in ("current", "queue", "off"):
            raise GenericError("Modo inválido! escolha entre: current/queue/off")

        await self.loop_mode.callback(self=self, inter=ctx, mode=mode)

    @is_dj()
    @has_source()
    @check_voice()
    @commands.slash_command(
        description=localized("cmd.loop_mode.description"),
        extras={"only_voiced": True},
        cooldown=loop_cd,
        max_concurrency=loop_mc,
    )
    @commands.contexts(guild=True)
    async def loop_mode(
        self,
        inter: disnake.ApplicationCommandInteraction,
        mode: str = commands.Param(
            name=localized("cmd.loop_mode.opt.mode.name"),
            choices=[
                disnake.OptionChoice(localized("cmd.loop_mode.opt.mode.choice.current"), "current"),
                disnake.OptionChoice(localized("cmd.loop_mode.opt.mode.choice.queue"), "queue"),
                disnake.OptionChoice(localized("cmd.loop_mode.opt.mode.choice.off"), "off"),
            ],
        ),
    ):
        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        if mode == player.loop:
            raise GenericError("**O modo de repetição selecionado já está ativo...**")

        if mode == "off":
            mode = False
            player.current.info["extra"]["track_loops"] = 0
            emoji = "⭕"
            txt = ["desativou a repetição.", f"{emoji} **⠂{inter.author.mention}desativou a repetição.**"]

        elif mode == "current":
            player.current.info["extra"]["track_loops"] = 0
            emoji = "🔂"
            txt = [
                "ativou a repetição da música atual.",
                f"{emoji} **⠂{inter.author.mention} ativou a repetição da música atual.**",
            ]

        else:  # queue
            emoji = "🔁"
            txt = ["ativou a repetição da fila.", f"{emoji} **⠂{inter.author.mention} ativou a repetição da fila.**"]

        player.loop = mode

        bot.loop.create_task(player.process_rpc())

        await self.interaction_message(inter, txt, emoji=emoji)

    @is_dj()
    @has_source()
    @check_voice()
    @commands.slash_command(
        description=localized("cmd.loop_amount.description"),
        extras={"only_voiced": True},
        cooldown=loop_cd,
        max_concurrency=loop_mc,
    )
    @commands.contexts(guild=True)
    async def loop_amount(
        self,
        inter: disnake.ApplicationCommandInteraction,
        value: int = commands.Param(
            name=localized("cmd.loop_amount.opt.value.name"),
            description=localized("cmd.loop_amount.opt.value.description"),
        ),
    ):
        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        player.current.info["extra"]["track_loops"] = value

        txt = [
            f"definiu a quantidade de repetições da música "
            f"{md_link(player.current.title, player.current.uri or player.current.search_uri, limit=25)} para **{value}**.",
            f"🔄 **⠂{inter.author.mention} definiu a quantidade de repetições da música para [{value}]:**\n"
            f"╰{md_link(player.current.title, player.current.uri or player.current.search_uri)}",
        ]

        await self.interaction_message(inter, txt, rpc_update=True, emoji="🔄")

    nightcore_cd = commands.CooldownMapping.from_cooldown(1, 7, commands.BucketType.guild)

    nightcore_mc = commands.MaxConcurrency(1, per=commands.BucketType.guild, wait=False)

    @is_dj()
    @has_source()
    @check_voice()
    @pool_command(
        name="nightcore",
        aliases=["nc"],
        only_voiced=True,
        cooldown=nightcore_cd,
        max_concurrency=nightcore_mc,
        description="Ativar/Desativar o efeito nightcore (Música acelerada com tom mais agudo).",
    )
    async def nightcore_legacy(self, ctx: CustomContext):
        await self.nightcore.callback(self=self, inter=ctx)

    @is_dj()
    @has_source()
    @check_voice()
    @commands.slash_command(
        description=localized("cmd.nightcore.description"),
        extras={"only_voiced": True},
        cooldown=nightcore_cd,
        max_concurrency=nightcore_mc,
    )
    @commands.contexts(guild=True)
    async def nightcore(self, inter: disnake.ApplicationCommandInteraction):
        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        player.nightcore = not player.nightcore

        if player.nightcore:
            await player.set_timescale(pitch=1.2, speed=1.1)
            txt = "ativou"
        else:
            await player.set_timescale(enabled=False)
            await player.update_filters()
            txt = "desativou"

        txt = [f"{txt} o efeito nightcore.", f"🇳 **⠂{inter.author.mention} {txt} o efeito nightcore.**"]

        await self.interaction_message(inter, txt, emoji="🇳")
