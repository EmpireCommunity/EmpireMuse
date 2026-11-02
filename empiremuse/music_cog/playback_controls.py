# -*- coding: utf-8 -*-
import asyncio

import disnake
from disnake.ext import commands

from empiremuse.i18n import localized
from empiremuse.rules.i18n import fit
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
        aliases=["vol", "v"],
        only_voiced=True,
        cooldown=volume_cd,
        max_concurrency=volume_mc,
        usage="{prefix}{cmd} [nivel]\nEx: {prefix}{cmd} 50",
    )
    async def volume_legacy(self, ctx: CustomContext, level: int):
        if not 4 < level < 151:
            tr = await self.bot.pool.i18n.for_guild(ctx.guild)
            raise GenericError(tr("playback.volume.invalid_range"), self_delete=7)

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
        tr = await self.bot.pool.i18n.for_guild(inter.guild)

        embed = disnake.Embed(color=disnake.Colour.red())

        if value is None:
            view = VolumeInteraction(inter, tr=tr)

            embed.colour = self.bot.get_color(guild.me)
            embed.description = tr("playback.volume.pick")

            try:
                if bot.user.id != self.bot.user.id:
                    embed.set_footer(
                        text=tr("player.selected_bot", bot=bot.user.display_name), icon_url=bot.user.display_avatar.url
                    )
            except AttributeError:
                pass

            await inter.send(embed=embed, ephemeral=await self.is_jukebox_channel(inter), view=view)
            await view.wait()
            if view.volume is None:
                return

            value = view.volume

        elif not 4 < value < 151:
            raise GenericError(tr("playback.volume.out_of_range"))

        await player.set_volume(value)

        txt = [
            tr("playback.volume.log", value=value),
            tr("playback.volume.done", mention=inter.author.mention, value=value),
        ]
        await self.interaction_message(inter, txt, emoji="🔊")

    pause_resume_cd = commands.CooldownMapping.from_cooldown(2, 7, commands.BucketType.member)

    pause_resume_mc = commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

    @is_dj()
    @has_source()
    @check_voice()
    @pool_command(
        name="pause",
        aliases=["pausar"],
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
        tr = await self.bot.pool.i18n.for_guild(inter.guild)

        if player.paused:
            raise GenericError(tr("playback.pause.already"))

        await player.set_pause(True)

        txt = [tr("playback.pause.log"), tr("playback.pause.done", mention=inter.author.mention)]

        await self.interaction_message(inter, txt, rpc_update=True, emoji="⏸️")
        await player.update_stage_topic()

    @is_dj()
    @has_source()
    @check_voice()
    @pool_command(
        name="resume",
        aliases=["unpause", "retomar"],
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
        tr = await self.bot.pool.i18n.for_guild(inter.guild)

        if not player.paused:
            raise GenericError(tr("playback.resume.not_paused"))

        await player.set_pause(False)

        txt = [tr("playback.resume.log"), tr("playback.resume.done", mention=inter.author.mention)]
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
        tr = await self.bot.pool.i18n.for_guild(inter.guild)

        if player.current.is_stream:
            raise GenericError(tr("playback.seek.livestream"))

        position = position.split(" | ")[0].replace(" ", ":")

        seconds = string_to_seconds(position)

        if seconds is None:
            raise GenericError(tr("playback.seek.invalid_time"))

        milliseconds = seconds * 1000

        if milliseconds < 0:
            milliseconds = 0

        if milliseconds > player.position:
            emoji = "⏩"

            txt = [
                tr("playback.seek.forward_log", time=time_format(milliseconds)),
                tr(
                    "playback.seek.forward_done",
                    emoji=emoji,
                    mention=inter.author.mention,
                    time=time_format(milliseconds),
                ),
            ]

        else:
            emoji = "⏪"

            txt = [
                tr("playback.seek.back_log", time=time_format(milliseconds)),
                tr(
                    "playback.seek.back_done", emoji=emoji, mention=inter.author.mention, time=time_format(milliseconds)
                ),
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
        only_voiced=True,
        cooldown=loop_cd,
        max_concurrency=loop_mc,
        usage="{prefix}{cmd} <1-10|current|queue|off>\nEx 1: {prefix}{cmd} 1\nEx 2: {prefix}{cmd} queue",
    )
    async def loop(self, ctx: CustomContext, mode: str = None):
        tr = await self.bot.pool.i18n.for_guild(ctx.guild)
        if not mode:
            embed = disnake.Embed(description=tr("playback.loop.pick"), color=self.bot.get_color(ctx.guild.me))

            msg = await ctx.send(
                ctx.author.mention,
                embed=embed,
                components=[
                    disnake.ui.Select(
                        placeholder=fit(tr("playback.loop.placeholder"), 150),
                        custom_id="loop_mode_legacy",
                        options=[
                            disnake.SelectOption(label=fit(tr("playback.loop.current.label"), 100), value="current"),
                            disnake.SelectOption(label=fit(tr("playback.loop.queue.label"), 100), value="queue"),
                            disnake.SelectOption(label=fit(tr("playback.loop.off.label"), 100), value="off"),
                        ],
                    )
                ],
            )

            try:
                select: disnake.MessageInteraction = await self.bot.wait_for(
                    "dropdown", timeout=30, check=lambda i: i.message.id == msg.id and i.author == ctx.author
                )
            except asyncio.TimeoutError:
                embed.description = tr("playback.loop.timeout")
                try:
                    await msg.edit(embed=embed, view=None)
                except Exception:
                    pass
                return

            mode = select.data.values[0]
            ctx.store_message = msg

        if mode.isdigit():
            if len(mode) > 2 or int(mode) > 10:
                raise GenericError(tr("playback.loop.invalid_amount", mode=mode))

            await self.loop_amount.callback(self=self, inter=ctx, value=int(mode))
            return

        if mode not in ("current", "queue", "off"):
            raise GenericError(tr("playback.loop.invalid_mode"))

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
        tr = await self.bot.pool.i18n.for_guild(inter.guild)

        if mode == player.loop:
            raise GenericError(tr("playback.loop.already_active"))

        if mode == "off":
            mode = False
            player.current.info["extra"]["track_loops"] = 0
            emoji = "⭕"
            txt = [tr("playback.loop.off_log"), tr("playback.loop.off_done", emoji=emoji, mention=inter.author.mention)]

        elif mode == "current":
            player.current.info["extra"]["track_loops"] = 0
            emoji = "🔂"
            txt = [
                tr("playback.loop.current_log"),
                tr("playback.loop.current_done", emoji=emoji, mention=inter.author.mention),
            ]

        else:  # queue
            emoji = "🔁"
            txt = [
                tr("playback.loop.queue_log"),
                tr("playback.loop.queue_done", emoji=emoji, mention=inter.author.mention),
            ]

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
        tr = await self.bot.pool.i18n.for_guild(inter.guild)

        player.current.info["extra"]["track_loops"] = value

        txt = [
            tr(
                "playback.loop_amount.log",
                track=md_link(player.current.title, player.current.uri or player.current.search_uri, limit=25),
                value=value,
            ),
            tr(
                "playback.loop_amount.done",
                mention=inter.author.mention,
                value=value,
                track=md_link(player.current.title, player.current.uri or player.current.search_uri),
            ),
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
        tr = await self.bot.pool.i18n.for_guild(inter.guild)

        player.nightcore = not player.nightcore

        if player.nightcore:
            await player.set_timescale(pitch=1.2, speed=1.1)
            state = "on"
        else:
            await player.set_timescale(enabled=False)
            await player.update_filters()
            state = "off"

        txt = [
            tr(f"playback.nightcore.{state}_log"),
            tr(f"playback.nightcore.{state}_done", mention=inter.author.mention),
        ]

        await self.interaction_message(inter, txt, emoji="🇳")
