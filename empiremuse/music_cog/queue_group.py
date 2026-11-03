# -*- coding: utf-8 -*-
import logging
from random import shuffle
from typing import Union, Optional

import disnake
from disnake.ext import commands

from empiremuse.i18n import localized
from empiremuse.utils.music.checks import (
    check_voice,
    has_player,
    is_dj,
    check_pool_bots,
    check_queue_loading,
)
from empiremuse.utils.music.errors import (
    GenericError,
)
from empiremuse.utils.music.interactions import (
    QueueInteraction,
)
from empiremuse.utils.music.models import (
    LavalinkPlayer,
)
from empiremuse.utils.others import (
    CustomContext,
    pool_command,
    CommandArgparse,
)

# O corpo da classe usa estes nomes, então eles precisam existir neste módulo.
from empiremuse.music_cog.shared import (
    remove_mc,
    queue_manipulation_cd,
)
from empiremuse.music_cog.queue_clear_steps import _qc_filters, _qc_range, _qc_remove, _qc_report
from empiremuse.music_cog.queue_move_steps import _qm_filter_texts, _qm_filters, _qm_move, _qm_range, _qm_select
from empiremuse.music_cog.queue_state import ClearState, MoveState

logger = logging.getLogger(__name__)


class QueueGroupMixin:
    @has_player()
    @check_voice()
    @commands.slash_command(name="queue", extras={"only_voiced": True})
    @commands.contexts(guild=True)
    async def q(self, inter):
        pass

    @is_dj()
    @has_player()
    @check_voice()
    @pool_command(
        name="shuffle",
        aliases=["sf", "misturar"],
        only_voiced=True,
        description="Misturar as músicas da fila",
        cooldown=queue_manipulation_cd,
        max_concurrency=remove_mc,
    )
    async def shuffle_legacy(self, ctx: CustomContext):
        await self.shuffle_.callback(self, inter=ctx)

    @is_dj()
    @q.sub_command(
        name="shuffle",
        description=localized("cmd.queue.shuffle.description"),
        extras={"only_voiced": True},
        cooldown=queue_manipulation_cd,
        max_concurrency=remove_mc,
    )
    async def shuffle_(self, inter: disnake.ApplicationCommandInteraction):
        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        if len(player.queue) < 3:
            raise GenericError("**A fila tem que ter no mínimo 3 músicas para ser misturada.**")

        shuffle(player.queue)

        await self.interaction_message(
            inter,
            ["misturou as músicas da fila.", f"🔀 **⠂{inter.author.mention} misturou as músicas da fila.**"],
            emoji="🔀",
        )

    @is_dj()
    @has_player()
    @check_voice()
    @pool_command(
        name="reverse",
        aliases=["rv", "inverter"],
        only_voiced=True,
        description="Inverter a ordem das músicas na fila",
        cooldown=queue_manipulation_cd,
        max_concurrency=remove_mc,
    )
    async def reverse_legacy(self, ctx: CustomContext):
        await self.reverse.callback(self=self, inter=ctx)

    @is_dj()
    @q.sub_command(
        description=localized("cmd.queue.reverse.description"),
        extras={"only_voiced": True},
        cooldown=queue_manipulation_cd,
        max_concurrency=remove_mc,
    )
    async def reverse(self, inter: disnake.ApplicationCommandInteraction):
        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        if len(player.queue) < 2:
            raise GenericError("**A fila tem que ter no mínimo 2 músicas para inverter a ordem.**")

        player.queue.reverse()
        await self.interaction_message(
            inter,
            txt=[
                "inverteu a ordem das músicas na fila.",
                f"🔄 **⠂{inter.author.mention} inverteu a ordem das músicas na fila.**",
            ],
            emoji="🔄",
        )

    queue_show_mc = commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

    @has_player()
    @check_voice()
    @pool_command(
        name="queue",
        aliases=["q", "fila"],
        description="Exibir as músicas que estão na fila.",
        only_voiced=True,
        max_concurrency=queue_show_mc,
    )
    async def queue_show_legacy(self, ctx: CustomContext):
        await self.display.callback(self=self, inter=ctx)

    @commands.max_concurrency(1, commands.BucketType.member)
    @q.sub_command(description=localized("cmd.queue.display.description"), max_concurrency=queue_show_mc)
    async def display(self, inter: disnake.ApplicationCommandInteraction):
        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        if not player.queue and not player.queue_autoplay:
            raise GenericError("**Não há músicas na fila.**")

        view = QueueInteraction(bot, inter.author)
        embed = view.embed

        try:
            if bot.user.id != self.bot.user.id:
                embed.set_footer(text=f"Bot selecionado: {bot.user.display_name}", icon_url=bot.user.display_avatar.url)
        except AttributeError:
            pass

        await inter.response.defer(ephemeral=True)

        kwargs = {"embed": embed, "view": view}

        try:
            func = inter.followup.send
            kwargs["ephemeral"] = True
        except AttributeError:
            try:
                func = inter.edit_original_message
            except AttributeError:
                func = inter.send
                kwargs["ephemeral"] = True

        view.message = await func(**kwargs)

        await view.wait()

    adv_queue_flags = CommandArgparse()

    adv_queue_flags.add_argument(
        "-songtitle",
        "-name",
        "-title",
        "-songname",
        nargs="+",
        help="incluir nome que tiver na música.\nEx: -name NCS",
        default=[],
    )

    adv_queue_flags.add_argument(
        "-uploader",
        "-author",
        "-artist",
        nargs="+",
        default=[],
        help="Remover músicas com o nome que tiver no autor/artista/uploader especificado.\nEx: -uploader sekai",
    )

    adv_queue_flags.add_argument(
        "-member",
        "-user",
        "-u",
        nargs="+",
        default=[],
        help="Remover músicas pedidas pelo usuário especificado.\nEx: -user @user",
    )

    adv_queue_flags.add_argument(
        "-duplicates", "-dupes", "-duplicate", action="store_true", help="Remover músicas duplicadas."
    )

    adv_queue_flags.add_argument(
        "-playlist",
        "-list",
        "-pl",
        nargs="+",
        default=[],
        help="Remover música que tiver com o nome especificado na playlist associada.\nEx: -playlist minhaplaylist",
    )

    adv_queue_flags.add_argument(
        "-minimaltime",
        "-mintime",
        "-min",
        "-minduration",
        "-minduration",
        default=None,
        help="Remover músicas com a duração mínima especificada.\nEx: -min 1:23.",
    )

    adv_queue_flags.add_argument(
        "-maxduration",
        "-maxtime",
        "-max",
        default=None,
        help="Remover músicas com a duração máxima especificada.\nEx: -max 1:23.",
    )

    adv_queue_flags.add_argument(
        "-amount",
        "-counter",
        "-count",
        "-c",
        type=int,
        default=None,
        help="Especificar uma quantidade de músicas para mover com o nome especificado.\nEx: -amount 5",
    )

    adv_queue_flags.add_argument(
        "-startposition",
        "-startpos",
        "-start",
        type=int,
        default=0,
        help="Remover músicas a partir de uma posição inicial da fila.\nEx: -start 10",
    )

    adv_queue_flags.add_argument(
        "-endposition",
        "-endpos",
        "-end",
        type=int,
        default=0,
        help="Remover músicas da fila até uma posição específica na fila.\nEx: -end 15",
    )

    adv_queue_flags.add_argument(
        "-absentmembers",
        "-absent",
        "-abs",
        action="store_true",
        help="Remover músicas adicionads por membros que saíram do canal",
    )

    clear_flags = CommandArgparse(parents=[adv_queue_flags])

    @is_dj()
    @has_player()
    @check_voice()
    @pool_command(
        name="clear",
        aliases=["limpar"],
        description="Limpar a fila de música.",
        only_voiced=True,
        extras={"flags": clear_flags},
        cooldown=queue_manipulation_cd,
        max_concurrency=remove_mc,
    )
    async def clear_legacy(self, ctx: CustomContext, *, flags: str = ""):
        args, unknown = ctx.command.extras["flags"].parse_known_args(flags.split())

        await self.clear.callback(
            self=self,
            inter=ctx,
            song_name=" ".join(args.songtitle + unknown),
            song_author=" ".join(args.uploader),
            user=await commands.MemberConverter().convert(ctx, " ".join(args.member)) if args.member else None,
            duplicates=args.duplicates,
            playlist=" ".join(args.playlist),
            min_duration=args.minimaltime,
            max_duration=args.maxduration,
            amount=args.amount,
            range_start=args.startposition,
            range_end=args.endposition,
            absent_members=args.absentmembers,
        )

    @check_queue_loading()
    @is_dj()
    @has_player()
    @check_voice()
    @q.sub_command(
        name="clear",
        description=localized("cmd.queue.clear.description"),
        extras={"only_voiced": True},
        cooldown=queue_manipulation_cd,
        max_concurrency=remove_mc,
    )
    async def clear(
        self,
        inter: disnake.ApplicationCommandInteraction,
        song_name: str = commands.Param(
            name=localized("cmd.queue.clear.opt.name.name"),
            description=localized("cmd.queue.clear.opt.name.description"),
            default=None,
        ),
        song_author: str = commands.Param(
            name=localized("cmd.queue.clear.opt.uploader.name"),
            description=localized("cmd.queue.clear.opt.uploader.description"),
            default=None,
        ),
        user: disnake.Member = commands.Param(
            name=localized("cmd.queue.clear.opt.member.name"),
            description=localized("cmd.queue.clear.opt.member.description"),
            default=None,
        ),
        duplicates: bool = commands.Param(
            name=localized("cmd.queue.clear.opt.duplicates.name"),
            description=localized("cmd.queue.clear.opt.duplicates.description"),
            default=False,
        ),
        playlist: str = commands.Param(
            name=localized("cmd.queue.clear.opt.playlist.name"),
            description=localized("cmd.queue.clear.opt.playlist.description"),
            default=None,
        ),
        min_duration: str = commands.Param(
            name=localized("cmd.queue.clear.opt.min_duration.name"),
            description=localized("cmd.queue.clear.opt.min_duration.description"),
            default=None,
        ),
        max_duration: str = commands.Param(
            name=localized("cmd.queue.clear.opt.max_duration.name"),
            description=localized("cmd.queue.clear.opt.max_duration.description"),
            default=None,
        ),
        amount: int = commands.Param(
            name=localized("cmd.queue.clear.opt.amount.name"),
            description=localized("cmd.queue.clear.opt.amount.description"),
            min_value=0,
            max_value=99,
            default=None,
        ),
        range_start: int = commands.Param(
            name=localized("cmd.queue.clear.opt.range_start.name"),
            description=localized("cmd.queue.clear.opt.range_start.description"),
            min_value=1.0,
            max_value=500.0,
            default=0,
        ),
        range_end: int = commands.Param(
            name=localized("cmd.queue.clear.opt.range_end.name"),
            description=localized("cmd.queue.clear.opt.range_end.description"),
            min_value=1.0,
            max_value=500.0,
            default=0,
        ),
        absent_members: bool = commands.Param(
            name=localized("cmd.queue.clear.opt.absent_members.name"),
            description=localized("cmd.queue.clear.opt.absent_members.description"),
            default=False,
        ),
    ):
        req = ClearState(
            inter=inter,
            song_name=song_name,
            song_author=song_author,
            user=user,
            duplicates=duplicates,
            playlist=playlist,
            min_duration=min_duration,
            max_duration=max_duration,
            amount=amount,
            range_start=range_start,
            range_end=range_end,
            absent_members=absent_members,
        )
        _qc_filters(self, req)

        if not req.filters and not req.range_start and not req.range_end:
            req.player.queue.clear()
            req.txt = ["limpou a fila de música.", f"♻️ **⠂{req.inter.author.mention} limpou a fila de música.**"]

        else:
            _qc_range(self, req)

            _qc_remove(self, req)

            if await _qc_report(self, req):
                return

        try:
            kwargs = {"thumb": req.tracklist[0].thumb}
        except IndexError:
            kwargs = {}

        await self.interaction_message(req.inter, req.txt, emoji="♻️", **kwargs)

    move_queue_flags = CommandArgparse(parents=[adv_queue_flags])

    move_queue_flags.add_argument(
        "-position", "-pos", help="Especificar uma posição de destino (opcional).\nEx: -pos 1", type=int, default=None
    )

    move_queue_flags.add_argument(
        "-casesensitive",
        "-cs",
        action="store_true",
        help="Buscar por músicas com a frase exata no nome da música ao invés de buscar palavra por palavra.",
    )

    @check_queue_loading()
    @is_dj()
    @has_player()
    @check_voice()
    @pool_command(
        name="move",
        aliases=["mv", "mover"],
        description="Mover músicas da fila.",
        only_voiced=True,
        extras={"flags": move_queue_flags},
        cooldown=queue_manipulation_cd,
        max_concurrency=remove_mc,
    )
    async def move_legacy(self, ctx: CustomContext, position: Optional[int] = None, *, flags: str = ""):
        args, unknown = ctx.command.extras["flags"].parse_known_args(flags.split())

        if args.position:
            if position:
                unknown.insert(0, str(position))
            position = args.position

        if position is None:
            position = 1

        await self.do_move(
            inter=ctx,
            position=position,
            song_name=" ".join(unknown + args.songtitle),
            song_author=" ".join(args.uploader),
            user=await commands.MemberConverter().convert(ctx, " ".join(args.member)) if args.member else None,
            duplicates=args.duplicates,
            playlist=" ".join(args.playlist),
            min_duration=args.minimaltime,
            max_duration=args.maxduration,
            amount=args.amount,
            range_start=args.startposition,
            range_end=args.endposition,
            absent_members=args.absentmembers,
        )

    @check_queue_loading()
    @is_dj()
    @has_player()
    @check_voice()
    @commands.slash_command(
        name="move",
        description=localized("cmd.move.description"),
        extras={"only_voiced": True},
        cooldown=queue_manipulation_cd,
        max_concurrency=remove_mc,
    )
    @commands.contexts(guild=True)
    async def move(
        self,
        inter: disnake.ApplicationCommandInteraction,
        song_name: str = commands.Param(
            name=localized("cmd.move.opt.name.name"),
            description=localized("cmd.move.opt.name.description"),
            default=None,
        ),
        position: int = commands.Param(
            name=localized("cmd.move.opt.position.name"),
            description=localized("cmd.move.opt.position.description"),
            min_value=1,
            max_value=900,
            default=1,
        ),
        song_author: str = commands.Param(
            name=localized("cmd.move.opt.uploader.name"),
            description=localized("cmd.move.opt.uploader.description"),
            default=None,
        ),
        user: disnake.Member = commands.Param(
            name=localized("cmd.move.opt.member.name"),
            description=localized("cmd.move.opt.member.description"),
            default=None,
        ),
        duplicates: bool = commands.Param(
            name=localized("cmd.move.opt.duplicates.name"),
            description=localized("cmd.move.opt.duplicates.description"),
            default=False,
        ),
        playlist: str = commands.Param(
            name=localized("cmd.move.opt.playlist.name"),
            description=localized("cmd.move.opt.playlist.description"),
            default=None,
        ),
        min_duration: str = commands.Param(
            name=localized("cmd.move.opt.min_duration.name"),
            description=localized("cmd.move.opt.min_duration.description"),
            default=None,
        ),
        max_duration: str = commands.Param(
            name=localized("cmd.move.opt.max_duration.name"),
            description=localized("cmd.move.opt.max_duration.description"),
            default=None,
        ),
        amount: int = commands.Param(
            name=localized("cmd.move.opt.amount.name"),
            description=localized("cmd.move.opt.amount.description"),
            min_value=0,
            max_value=99,
            default=None,
        ),
        range_start: int = commands.Param(
            name=localized("cmd.move.opt.range_start.name"),
            description=localized("cmd.move.opt.range_start.description"),
            min_value=1.0,
            max_value=500.0,
            default=0,
        ),
        range_end: int = commands.Param(
            name=localized("cmd.move.opt.range_end.name"),
            description=localized("cmd.move.opt.range_end.description"),
            min_value=1.0,
            max_value=500.0,
            default=0,
        ),
        absent_members: bool = commands.Param(
            name=localized("cmd.move.opt.absent_members.name"),
            description=localized("cmd.move.opt.absent_members.description"),
            default=False,
        ),
    ):
        await self.do_move(
            inter=inter,
            position=position,
            song_name=song_name,
            song_author=song_author,
            user=user,
            duplicates=duplicates,
            playlist=playlist,
            min_duration=min_duration,
            max_duration=max_duration,
            amount=amount,
            range_start=range_start,
            range_end=range_end,
            absent_members=absent_members,
        )

    async def do_move(
        self,
        inter: Union[disnake.ApplicationCommandInteraction, CustomContext],
        position: int = 1,
        song_name: str = None,
        song_author: str = None,
        user: disnake.Member = None,
        duplicates: bool = False,
        playlist: str = None,
        min_duration: str = None,
        max_duration: str = None,
        amount: int = None,
        range_start: int = 0,
        range_end: int = 0,
        absent_members: bool = False,
        case_sensitive=False,
    ):
        req = MoveState(
            inter=inter,
            position=position,
            song_name=song_name,
            song_author=song_author,
            user=user,
            duplicates=duplicates,
            playlist=playlist,
            min_duration=min_duration,
            max_duration=max_duration,
            amount=amount,
            range_start=range_start,
            range_end=range_end,
            absent_members=absent_members,
            case_sensitive=case_sensitive,
        )
        _qm_filters(self, req)

        _qm_range(self, req)

        if not req.tracklist:
            _qm_select(self, req)

        if not req.tracklist:
            raise GenericError("Nenhuma música encontrada com os filtros selecionados!")

        for req.t in reversed(req.tracklist):
            req.insert_func(req.position - 1, req.t)

        _qm_filter_texts(self, req)

        await _qm_move(self, req)

    @move.autocomplete("playlist")
    @clear.autocomplete("playlist")
    async def queue_playlist(self, inter: disnake.Interaction, query: str):
        try:
            if not inter.author.voice:
                return
        except Exception:
            pass

        if not self.bot.bot_ready or not self.bot.is_ready():
            return [query]

        try:
            await check_pool_bots(inter, only_voiced=True)
            bot = inter.music_bot
        except Exception:
            logger.exception("Falha em queue_playlist")
            return

        try:
            player = bot.music.players[inter.guild_id]
        except KeyError:
            return

        return list(
            set(
                [
                    track.playlist_name
                    for track in player.queue
                    if track.playlist_name and query.lower() in track.playlist_name.lower()
                ]
            )
        )[:20]

    @move.autocomplete("uploader")
    @clear.autocomplete("uploader")
    async def queue_author(self, inter: disnake.Interaction, query: str):
        if not self.bot.bot_ready or not self.bot.is_ready():
            return [query]

        try:
            await check_pool_bots(inter, only_voiced=True)
            bot = inter.music_bot
        except Exception:
            return

        if not inter.author.voice:
            return

        try:
            player = bot.music.players[inter.guild_id]
        except KeyError:
            return

        if not query:
            return list(set([track.authors_string for track in player.queue]))[:20]
        else:
            return list(
                set([track.authors_string for track in player.queue if query.lower() in track.authors_string.lower()])
            )[:20]
