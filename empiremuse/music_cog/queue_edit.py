# -*- coding: utf-8 -*-

import disnake
from disnake.ext import commands

from empiremuse.i18n import localized
from empiremuse.utils.music.checks import (
    check_voice,
    has_player,
    is_dj,
    check_queue_loading,
)
from empiremuse.utils.music.converters import md_link
from empiremuse.utils.music.errors import (
    GenericError,
)
from empiremuse.utils.music.models import (
    LavalinkPlayer,
)
from empiremuse.utils.others import (
    CustomContext,
    PlayerControls,
    queue_track_index,
    pool_command,
)

# O corpo da classe usa estes nomes, então eles precisam existir neste módulo.
from empiremuse.music_cog.shared import (
    case_sensitive_args,
    queue_manipulation_cd,
    remove_mc,
)
from empiremuse.utils.music.app_emojis import emoji as app_emoji


class QueueEditMixin:
    @is_dj()
    @has_player()
    @check_voice()
    @pool_command(
        name="remove",
        aliases=["r", "del"],
        description="Remover uma música específica da fila.",
        only_voiced=True,
        max_concurrency=remove_mc,
        extras={"flags": case_sensitive_args},
        usage="{prefix}{cmd} [nome]\nEx: {prefix}{cmd} sekai",
    )
    async def remove_legacy(self, ctx: CustomContext, *, flags: str = ""):
        args, unknown = ctx.command.extras["flags"].parse_known_args(flags.split())

        if not unknown:
            raise GenericError("**Você não adicionou o nome da música.**")

        await self.remove.callback(self=self, inter=ctx, query=" ".join(unknown), case_sensitive=args.casesensitive)

    @is_dj()
    @has_player()
    @check_voice()
    @commands.slash_command(
        description=localized("cmd.remove.description"),
        extras={"only_voiced": True},
        max_concurrency=remove_mc,
    )
    @commands.contexts(guild=True)
    async def remove(
        self,
        inter: disnake.ApplicationCommandInteraction,
        query: str = commands.Param(
            name=localized("cmd.remove.opt.name.name"), description=localized("cmd.remove.opt.name.description")
        ),
        case_sensitive: bool = commands.Param(
            name=localized("cmd.remove.opt.exact_name.name"),
            default=False,
            description=localized("cmd.remove.opt.exact_name.description"),
        ),
    ):
        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        try:
            index = queue_track_index(inter, bot, query, case_sensitive=case_sensitive)[0][0]
        except IndexError:
            raise GenericError(f"**Não há músicas na fila com o nome: {query}**")

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        track = player.queue[index]

        player.queue.remove(track)

        txt = [
            f"removeu a música {md_link(track.title, track.uri or track.search_uri, limit=25)} da fila.",
            f"♻️ **⠂{inter.author.mention} removeu a música da fila:**\n╰{md_link(track.title, track.uri or track.search_uri)}",
        ]

        await self.interaction_message(inter, txt, emoji="♻️")

        await player.update_message()

    @is_dj()
    @has_player()
    @check_voice()
    @pool_command(
        name="readd",
        aliases=["readicionar", "rdd"],
        only_voiced=True,
        cooldown=queue_manipulation_cd,
        max_concurrency=remove_mc,
        description="Readicionar as músicas tocadas na fila.",
    )
    async def readd_legacy(self, ctx: CustomContext):
        await self.readd_songs.callback(self=self, inter=ctx)

    @is_dj()
    @has_player()
    @check_voice()
    @commands.slash_command(
        description=localized("cmd.readd_songs.description"),
        extras={"only_voiced": True},
        cooldown=queue_manipulation_cd,
        max_concurrency=remove_mc,
    )
    @commands.contexts(guild=True)
    async def readd_songs(self, inter: disnake.ApplicationCommandInteraction):
        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        if not player.played and not player.failed_tracks:
            raise GenericError("**Não há músicas tocadas.**")

        qsize = len(player.played) + len(player.failed_tracks)

        player.played.reverse()
        player.failed_tracks.reverse()
        player.queue.extend(player.failed_tracks)
        player.queue.extend(player.played)
        player.played.clear()
        player.failed_tracks.clear()

        txt = [
            f"readicionou [{qsize}] música{(s := 's'[: qsize ^ 1])} tocada{s} na fila.",
            f"🎶 **⠂{inter.author.mention} readicionou {qsize} música{s} na fila.**",
        ]

        await self.interaction_message(inter, txt, emoji="🎶")

        await player.update_message()

        if not player.current:
            await player.process_next()
        else:
            await player.update_message()

    @is_dj()
    @has_player()
    @check_voice()
    @pool_command(
        name="rotate",
        aliases=["rt", "rotacionar"],
        only_voiced=True,
        description="Rotacionar a fila para a música especificada.",
        cooldown=queue_manipulation_cd,
        max_concurrency=remove_mc,
        extras={"flags": case_sensitive_args},
        usage="{prefix}{cmd} [nome]\nEx: {prefix}{cmd} sekai",
    )
    async def rotate_legacy(self, ctx: CustomContext, *, flags: str = ""):
        args, unknown = ctx.command.extras["flags"].parse_known_args(flags.split())

        if not unknown:
            raise GenericError("**Você não adicionou o nome da música.**")

        await self.rotate.callback(self=self, inter=ctx, query=" ".join(unknown), case_sensitive=args.casesensitive)

    @is_dj()
    @check_queue_loading()
    @has_player()
    @check_voice()
    @commands.slash_command(
        description=localized("cmd.rotate.description"),
        extras={"only_voiced": True},
        cooldown=queue_manipulation_cd,
        max_concurrency=remove_mc,
    )
    @commands.contexts(guild=True)
    async def rotate(
        self,
        inter: disnake.ApplicationCommandInteraction,
        query: str = commands.Param(
            name=localized("cmd.rotate.opt.name.name"), description=localized("cmd.rotate.opt.name.description")
        ),
        case_sensitive: bool = commands.Param(
            name=localized("cmd.rotate.opt.exact_name.name"),
            default=False,
            description=localized("cmd.rotate.opt.exact_name.description"),
        ),
    ):
        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        index = queue_track_index(inter, bot, query, case_sensitive=case_sensitive)

        if not index:
            raise GenericError(f"**Não há músicas na fila com o nome: {query}**")

        index = index[0][0]

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        track = (player.queue + player.queue_autoplay)[index]

        if index <= 0:
            raise GenericError(
                f"**A música **{md_link(track.title, track.uri or track.search_uri)} já é a próxima da fila."
            )

        if track.autoplay:
            player.queue_autoplay.rotate(0 - (index - len(player.queue)))
        else:
            player.queue.rotate(0 - (index))

        txt = [
            f"rotacionou a fila para a música {md_link(track.title, track.uri or track.search_uri, limit=25)}.",
            f"🔃 **⠂{inter.author.mention} rotacionou a fila para a música:**\n╰{md_link(track.title, track.uri or track.search_uri)}.",
        ]

        if isinstance(inter, disnake.MessageInteraction):
            player.set_command_log(text=f"{inter.author.mention} " + txt[0], emoji="🔃")
        else:
            await self.interaction_message(
                inter,
                txt,
                emoji="🔃",
                components=[
                    disnake.ui.Button(
                        emoji=app_emoji(inter.bot, "play"),
                        label="Tocar agora",
                        custom_id=PlayerControls.embed_forceplay,
                    ),
                ],
            )

        await player.update_message()
