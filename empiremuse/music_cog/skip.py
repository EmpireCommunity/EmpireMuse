# -*- coding: utf-8 -*-

import disnake
from disnake.ext import commands

from empiremuse.i18n import localized
from empiremuse.utils.music.checks import (
    check_voice,
    has_player,
    has_source,
    is_requester,
    is_dj,
    check_stage_topic,
    check_queue_loading,
    check_yt_cooldown,
)
from empiremuse.utils.music.converters import md_link
from empiremuse.utils.music.errors import (
    GenericError,
)
from empiremuse.utils.music.models import (
    LavalinkPlayer,
    LavalinkTrack,
)
from empiremuse.utils.others import (
    CustomContext,
    queue_track_index,
    pool_command,
)

# O corpo da classe usa estes nomes, então eles precisam existir neste módulo.
from empiremuse.music_cog.shared import (
    case_sensitive_args,
)


class SkipMixin:
    skip_back_cd = commands.CooldownMapping.from_cooldown(4, 13, commands.BucketType.member)

    skip_back_mc = commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

    @check_stage_topic()
    @check_yt_cooldown()
    @is_requester()
    @check_queue_loading()
    @check_voice()
    @pool_command(
        name="skip",
        aliases=["n", "s", "next", "pular"],
        cooldown=skip_back_cd,
        max_concurrency=skip_back_mc,
        description=f"Pular a música atual que está tocando.",
        extras={"flags": case_sensitive_args},
        only_voiced=True,
        usage="{prefix}{cmd} <termo>\nEx: {prefix}{cmd} sekai",
    )
    async def skip_legacy(self, ctx: CustomContext, *, flags: str = ""):
        args, unknown = ctx.command.extras["flags"].parse_known_args(flags.split())

        await self.skip.callback(self=self, inter=ctx, query=" ".join(unknown), case_sensitive=args.casesensitive)

    @check_stage_topic()
    @check_yt_cooldown()
    @is_requester()
    @check_queue_loading()
    @has_source()
    @check_voice()
    @commands.slash_command(
        description=localized("cmd.skipto.description"),
        extras={"only_voiced": True},
        cooldown=skip_back_cd,
        max_concurrency=skip_back_mc,
    )
    @commands.contexts(guild=True)
    async def skipto(
        self,
        inter: disnake.ApplicationCommandInteraction,
        query: str = commands.Param(
            name=localized("cmd.skipto.opt.name.name"), description=localized("cmd.skipto.opt.name.description")
        ),
        case_sensitive: bool = commands.Param(
            name=localized("cmd.skipto.opt.exact_name.name"),
            default=False,
            description=localized("cmd.skipto.opt.exact_name.description"),
        ),
    ):
        await self.skip.callback(self=self, inter=inter, query=query, case_sensitive=case_sensitive)

    @check_stage_topic()
    @check_yt_cooldown()
    @is_requester()
    @check_queue_loading()
    @has_player()
    @check_voice()
    @commands.slash_command(
        description=localized("cmd.skip.description"),
        extras={"only_voiced": True},
        cooldown=skip_back_cd,
        max_concurrency=skip_back_mc,
    )
    @commands.contexts(guild=True)
    async def skip(
        self,
        inter: disnake.ApplicationCommandInteraction,
        *,
        query: str = commands.Param(
            name=localized("cmd.skip.opt.name.name"),
            description=localized("cmd.skip.opt.name.description"),
            default=None,
        ),
        play_only: str = commands.Param(
            name=localized("cmd.skip.opt.play_only.name"),
            choices=[disnake.OptionChoice(localized("cmd.skip.opt.play_only.choice.yes"), "yes")],
            description=localized("cmd.skip.opt.play_only.description"),
            default="no",
        ),
        case_sensitive: bool = commands.Param(
            name=localized("cmd.skip.opt.exact_name.name"),
            default=False,
            description=localized("cmd.skip.opt.exact_name.description"),
        ),
    ):
        try:
            bot = inter.music_bot
            guild = inter.music_guild
        except AttributeError:
            bot = inter.bot
            guild = bot.get_guild(inter.guild_id)

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        ephemeral = await self.is_jukebox_channel(inter)

        interaction = None

        if query:
            try:
                index = queue_track_index(inter, bot, query, case_sensitive=case_sensitive)[0][0]
            except IndexError:
                raise GenericError(f"**Não há músicas na fila com o nome: {query}**")

            if player.queue:
                track: LavalinkTrack = player.queue[index]
                player.queue.append(player.last_track or player.current)
            else:
                track: LavalinkTrack = player.queue_autoplay[index]
                index += 1
                player.queue_autoplay.appendleft(player.last_track or player.current)

            player.last_track = None

            if player.loop == "current":
                player.loop = False

            if play_only == "yes":
                if track.autoplay:
                    del player.queue_autoplay[index]
                    player.queue_autoplay.appendleft(track)
                else:
                    del player.queue[index]
                    player.queue.appendleft(track)

            elif index > 0:
                if track.autoplay:
                    player.queue_autoplay.rotate(0 - index)
                else:
                    player.queue.rotate(0 - index)

            player.set_command_log(emoji="⤵️", text=f"{inter.author.mention} pulou para a música atual.")

            embed = disnake.Embed(
                color=self.bot.get_color(guild.me),
                description=f"⤵️ **⠂{inter.author.mention} pulou para a música:**\n"
                f"╰{md_link(track.title, track.uri or track.search_uri, limit=43)}{player.controller_link}",
            )

            try:
                if bot.user.id != self.bot.user.id:
                    embed.set_footer(
                        text=f"Bot selecionado: {bot.user.display_name}", icon_url=bot.user.display_avatar.url
                    )
            except AttributeError:
                pass

            if isinstance(inter, disnake.MessageInteraction) and inter.data.custom_id == "queue_track_selection":
                await inter.response.edit_message(embed=embed, view=None)
            elif (
                not isinstance(inter, (CustomContext, disnake.ApplicationCommandInteraction))
                and inter.data.custom_id == "musicplayer_queue_dropdown"
            ):
                await inter.response.defer()
            else:
                await inter.send(embed=embed, ephemeral=ephemeral)

        else:
            if isinstance(inter, disnake.MessageInteraction):
                player.set_command_log(text=f"{inter.author.mention} pulou a música.", emoji="⏭️")
                if not inter.response.is_done():
                    try:
                        await inter.response.defer()
                    except Exception:
                        pass
                interaction = inter
            else:
                player.set_command_log(emoji="⏭️", text=f"{inter.author.mention} pulou a música.")

                embed = disnake.Embed(
                    color=self.bot.get_color(guild.me),
                    description=f"⏭️ **⠂{inter.author.mention} pulou a música:\n"
                    f"╰{md_link(player.current.title, player.current.uri or player.current.search_uri, limit=43)}**"
                    f"{player.controller_link}",
                )

                try:
                    if bot.user.id != self.bot.user.id:
                        embed.set_footer(
                            text=f"Bot selecionado: {bot.user.display_name}", icon_url=bot.user.display_avatar.url
                        )
                except AttributeError:
                    pass

                await inter.send(embed=embed, ephemeral=ephemeral)

            if player.loop == "current":
                player.loop = False

        try:
            (player.current or player.last_track).info["extra"]["track_loops"] = 0
        except AttributeError:
            pass

        await player.track_end(ignore_track_loop=True)
        player.ignore_np_once = True
        await player.process_next(inter=interaction)

    @check_yt_cooldown()
    @check_stage_topic()
    @is_dj()
    @check_queue_loading()
    @has_player()
    @check_voice()
    @pool_command(
        name="back",
        aliases=["b", "voltar"],
        description="Voltar para a música anterior.",
        only_voiced=True,
        cooldown=skip_back_cd,
        max_concurrency=skip_back_mc,
    )
    async def back_legacy(self, ctx: CustomContext):
        await self.back.callback(self=self, inter=ctx)

    @check_stage_topic()
    @is_dj()
    @has_player()
    @check_queue_loading()
    @check_voice()
    @commands.max_concurrency(1, commands.BucketType.member)
    @commands.slash_command(
        description=localized("cmd.back.description"),
        extras={"only_voiced": True},
        cooldown=skip_back_cd,
        max_concurrency=skip_back_mc,
    )
    @commands.contexts(guild=True)
    async def back(self, inter: disnake.ApplicationCommandInteraction):
        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        if not len(player.queue) and (player.keep_connected or not len(player.played)):
            await player.seek(0)
            await self.interaction_message(inter, "voltou para o início da música.", emoji="⏪")
            return

        try:
            track = player.played.pop()
        except Exception:
            track = player.queue.pop()

        if not track and player.autoplay:
            try:
                track = player.queue_autoplay.pop()
            except Exception:
                pass

        if player.current:
            if player.current.autoplay:
                if player.autoplay:
                    player.queue_autoplay.appendleft(player.current)
            else:
                player.queue.appendleft(player.current)

        player.last_track = None

        player.queue.appendleft(track)

        if isinstance(inter, disnake.MessageInteraction):
            interaction = inter
            player.set_command_log(text=f"{inter.author.mention} voltou para a música atual.", emoji="⏮️")
            await inter.response.defer()
        else:
            interaction = None

            t = player.queue[0]

            txt = [
                "voltou para a música atual.",
                f"⏮️ **⠂{inter.author.mention} voltou para a música:\n╰{md_link(t.title, t.uri or t.search_uri, limit=43)}**",
            ]

            await self.interaction_message(inter, txt, emoji="⏮️", store_embed=True)

        if player.loop == "current":
            player.loop = False

        player.ignore_np_once = True

        if not player.current:
            await player.process_next(inter=interaction)
        else:
            player.is_previows_music = True
            await player.track_end()
            await player.process_next(inter=interaction, force_np=True)

    @check_stage_topic()
    @check_queue_loading()
    @has_source()
    @check_voice()
    @commands.slash_command(description=localized("cmd.voteskip.description"), extras={"only_voiced": True})
    @commands.contexts(guild=True)
    async def voteskip(self, inter: disnake.ApplicationCommandInteraction):
        try:
            bot = inter.music_bot
            guild = inter.music_guild
        except AttributeError:
            bot = inter.bot
            guild = inter.guild

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        embed = disnake.Embed()

        if inter.author.id in player.votes:
            raise GenericError("**Você já votou para pular a música atual.**")

        embed.colour = self.bot.get_color(guild.me)

        txt = [
            f"votou para pular a música atual (votos: {len(player.votes) + 1}/{self.bot.config['VOTE_SKIP_AMOUNT']}).",
            f"{inter.author.mention} votou para pular a música atual (votos: {len(player.votes) + 1}/{self.bot.config['VOTE_SKIP_AMOUNT']}).",
        ]

        if len(player.votes) < self.bot.config.get("VOTE_SKIP_AMOUNT", 3):
            embed.description = txt
            player.votes.add(inter.author.id)
            await self.interaction_message(inter, txt, emoji="✋")
            return

        await self.interaction_message(inter, txt, emoji="✋")
        await player.track_end()
        await player.process_next()
