# -*- coding: utf-8 -*-
import logging
import asyncio
from typing import Union

import disnake
from disnake.ext import commands

from empiremuse.i18n import localized
from empiremuse.utils.branding import asset_url
from empiremuse.utils.client import BotCore
from empiremuse.utils.music.checks import (
    check_voice,
    has_player,
    is_dj,
    can_send_message_check,
    can_connect,
    check_deafen,
    check_pool_bots,
)
from empiremuse.utils.music.converters import URL_REG, google_search, md_link
from empiremuse.utils.music.errors import (
    GenericError,
    MissingVoicePerms,
)
from empiremuse.utils.music.models import (
    LavalinkPlayer,
    LavalinkPlaylist,
)
from empiremuse.utils.others import (
    CustomContext,
    pool_command,
    CommandArgparse,
)

# O corpo da classe usa estes nomes, então eles precisam existir neste módulo.
from empiremuse.music_cog.shared import (
    play_cd,
    play_mc,
)

logger = logging.getLogger(__name__)


class PlayEntryMixin:
    @check_voice()
    @can_send_message_check()
    @commands.message_command(
        name="add to queue", extras={"check_player": False}, cooldown=play_cd, max_concurrency=play_mc
    )
    async def message_play(self, inter: disnake.MessageCommandInteraction):
        if not inter.target.content:
            emb = disnake.Embed(
                description=f"Não há texto na {md_link('mensagem', inter.target.jump_url, code=False)} selecionada...",
                color=disnake.Colour.red(),
            )
            await inter.send(embed=emb, ephemeral=True)
            return

        await self.play.callback(
            self=self,
            inter=inter,
            query=inter.target.content,
            position=0,
            options="",
            manual_selection=False,
            force_play="no",
        )

    @check_voice()
    @can_send_message_check()
    @commands.slash_command(
        name="search",
        extras={"check_player": False},
        cooldown=play_cd,
        max_concurrency=play_mc,
        description=localized("cmd.search.description"),
    )
    @commands.contexts(guild=True)
    async def search(
        self,
        inter: disnake.ApplicationCommandInteraction,
        query: str = commands.Param(
            name=localized("cmd.search.opt.query.name"), description=localized("cmd.search.opt.query.description")
        ),
        *,
        position: int = commands.Param(
            name=localized("cmd.search.opt.position.name"),
            description=localized("cmd.search.opt.position.description"),
            default=0,
        ),
        force_play: str = commands.Param(
            name=localized("cmd.search.opt.play_now.name"),
            description=localized("cmd.search.opt.play_now.description"),
            default="no",
            choices=[disnake.OptionChoice(localized("cmd.search.opt.play_now.choice.yes"), "yes")],
        ),
        options: str = commands.Param(
            name=localized("cmd.search.opt.options.name"),
            description=localized("cmd.search.opt.options.description"),
            choices=[
                disnake.OptionChoice(localized("cmd.search.opt.options.choice.shuffle"), "shuffle"),
                disnake.OptionChoice(localized("cmd.search.opt.options.choice.reversed"), "reversed"),
            ],
            default=False,
        ),
        server: str = commands.Param(
            name=localized("cmd.search.opt.server.name"),
            description=localized("cmd.search.opt.server.description"),
            default=None,
        ),
        manual_bot_choice: str = commands.Param(
            name=localized("cmd.search.opt.select_bot.name"),
            description=localized("cmd.search.opt.select_bot.description"),
            default="no",
            choices=[disnake.OptionChoice(localized("cmd.search.opt.select_bot.choice.yes"), "yes")],
        ),
    ):
        await self.play.callback(
            self=self,
            inter=inter,
            query=query,
            position=position,
            force_play=force_play,
            options=options,
            manual_selection=True,
            server=server,
            manual_bot_choice=manual_bot_choice,
        )

    @search.autocomplete("query")
    async def search_autocomplete(self, inter: disnake.Interaction, current: str):
        if not current:
            return []

        if not self.bot.bot_ready or not self.bot.is_ready() or URL_REG.match(current):
            return [current] if len(current) < 100 else []

        try:
            bot, guild = await check_pool_bots(inter, only_voiced=True)
        except GenericError:
            return [current[:99]]
        except Exception:
            bot = inter.bot

        try:
            if not inter.author.voice:
                return []
        except AttributeError:
            return [current[:99]]

        return await google_search(bot, current)

    @is_dj()
    @has_player()
    @can_send_message_check()
    @commands.max_concurrency(1, commands.BucketType.guild)
    @commands.slash_command(extras={"only_voiced": True}, description=localized("cmd.connect.description"))
    @commands.contexts(guild=True)
    async def connect(
        self,
        inter: disnake.ApplicationCommandInteraction,
        channel: Union[disnake.VoiceChannel, disnake.StageChannel] = commands.Param(
            name=localized("cmd.connect.opt.channel.name"), description=localized("cmd.connect.opt.channel.description")
        ),
    ):
        try:
            channel = inter.music_bot.get_channel(channel.id)
        except AttributeError:
            pass

        await self.do_connect(inter, channel)

    async def do_connect(
        self,
        ctx: Union[disnake.ApplicationCommandInteraction, commands.Context, disnake.Message],
        channel: Union[disnake.VoiceChannel, disnake.StageChannel] = None,
        check_other_bots_in_vc: bool = False,
        bot: BotCore = None,
        me: disnake.Member = None,
    ):
        if not channel:
            try:
                channel = ctx.music_bot.get_channel(ctx.author.voice.channel.id) or ctx.author.voice.channel
            except AttributeError:
                channel = ctx.author.voice.channel

        if not bot:
            try:
                bot = ctx.music_bot
            except AttributeError:
                try:
                    bot = ctx.bot
                except Exception:
                    bot = self.bot

        if not me:
            try:
                me = ctx.music_guild.me
            except AttributeError:
                me = channel.guild.me

        try:
            guild_id = ctx.guild_id
        except AttributeError:
            guild_id = ctx.guild.id

        try:
            text_channel = ctx.music_bot.get_channel(ctx.channel.id)
        except AttributeError:
            text_channel = ctx.channel

        try:
            player = bot.music.players[guild_id]
        except KeyError:
            logger.debug("Player debug test 20: %s | %s", bot.user, self.bot.user)
            raise GenericError(
                f"**O player do bot {bot.user.mention} foi finalizado antes de conectar no canal de voz "
                f"(ou o player não foi inicializado)...\nPor via das dúvidas tente novamente.**"
            )

        can_connect(channel, me.guild, check_other_bots_in_vc=check_other_bots_in_vc, bot=bot)

        deafen_check = True

        if isinstance(ctx, disnake.ApplicationCommandInteraction) and ctx.application_command.name == self.connect.name:
            perms = channel.permissions_for(me)

            if not perms.connect or not perms.speak:
                raise MissingVoicePerms(channel)

            await player.connect(channel.id, self_deaf=True)

            if channel != me.voice and me.voice.channel:
                txt = [f"me moveu para o canal <#{channel.id}>", f"**Movido com sucesso para o canal** <#{channel.id}>"]

                deafen_check = False

            else:
                txt = [f"me conectou no canal <#{channel.id}>", f"**Conectei no canal** <#{channel.id}>"]

            await self.interaction_message(ctx, txt, emoji="🔈", rpc_update=True)

        else:
            await player.connect(channel.id, self_deaf=True)

        try:
            player.members_timeout_task.cancel()
        except Exception:
            pass

        if deafen_check and bot.config["GUILD_DEAFEN_WARN"]:
            retries = 0

            while retries < 5:
                if me.voice:
                    break

                await asyncio.sleep(1)
                retries += 1

            if not await check_deafen(me):
                await text_channel.send(
                    embed=disnake.Embed(
                        title="Aviso:",
                        description="Para manter sua privacidade e me ajudar a economizar "
                        "recursos, recomendo desativar meu áudio do canal clicando "
                        "com botão direito sobre mim e em seguida marcar: desativar "
                        "áudio no servidor.",
                        color=self.bot.get_color(me),
                    ).set_image(url=asset_url("tutorial_server_deafen_guide.png")),
                    delete_after=20,
                )

        if isinstance(channel, disnake.StageChannel):
            stage_perms = channel.permissions_for(me)

            if stage_perms.mute_members:
                retries = 5

                while retries > 0:
                    await asyncio.sleep(1)
                    if not me.voice:
                        retries -= 1
                        continue
                    break
                await asyncio.sleep(1.5)
                await me.edit(suppress=False)
            else:
                embed = disnake.Embed(color=self.bot.get_color(me))

                embed.description = f"**Preciso que algum staff me convide para falar no palco: {md_link(channel.name, channel.jump_url, code=False)}.**"

                embed.set_footer(
                    text="💡 Dica: para me permitir falar no palco automaticamente será necessário me conceder "
                    "permissão de silenciar membros (no servidor ou apenas no canal de palco escolhido)."
                )

                await text_channel.send(ctx.author.mention, embed=embed, delete_after=45)

    @can_send_message_check()
    @check_voice()
    @commands.bot_has_guild_permissions(send_messages=True)
    @commands.max_concurrency(1, commands.BucketType.member)
    @pool_command(
        name="addposition",
        description="Adicionar música em uma posição especifica da fila.",
        aliases=["adp", "addpos"],
        check_player=False,
        cooldown=play_cd,
        max_concurrency=play_mc,
        usage="{prefix}{cmd} [posição(Nº)] [nome|link]\nEx: {prefix}{cmd} 2 sekai - burn me down",
    )
    async def addpos_legacy(self, ctx: CustomContext, position: int, *, query: str):
        if position < 1:
            raise GenericError("**Número da posição da fila tem que ser 1 ou superior.**")

        await self.play.callback(
            self=self,
            inter=ctx,
            query=query,
            position=position,
            options=False,
            force_play="no",
            manual_selection=False,
            server=None,
        )

    stage_flags = CommandArgparse()

    stage_flags.add_argument("query", nargs="*", help="nome ou link da música")

    stage_flags.add_argument(
        "-position",
        "-pos",
        "-p",
        type=int,
        default=0,
        help="Colocar a música em uma posição específica da fila (será ignorado caso use -next etc).\nEx: -p 10",
    )

    stage_flags.add_argument(
        "-next",
        "-proximo",
        action="store_true",
        help="Adicionar a música/playlist no topo da fila (equivalente ao: -pos 1)",
    )

    stage_flags.add_argument(
        "-reverse",
        "-r",
        action="store_true",
        help="Inverter a ordem das músicas adicionadas (efetivo apenas ao adicionar playlist).",
    )

    stage_flags.add_argument(
        "-shuffle",
        "-sl",
        action="store_true",
        help="Misturar as músicas adicionadas (efetivo apenas ao adicionar playlist).",
    )

    stage_flags.add_argument(
        "-select", "-s", action="store_true", help="Escolher a música entre os resultados encontrados."
    )

    stage_flags.add_argument(
        "-mix",
        "-rec",
        "-recommended",
        action="store_true",
        help="Adicionar/tocar músicas recomendadas com o nome do artsta - música informado.",
    )

    stage_flags.add_argument(
        "-force",
        "-now",
        "-n",
        "-f",
        action="store_true",
        help="Tocar a música adicionada imediatamente (efetivo apenas se houver uma música tocando atualmente.)",
    )

    stage_flags.add_argument("-server", "-sv", type=str, default=None, help="Usar um servidor de música específico.")

    stage_flags.add_argument("-selectbot", "-sb", action="store_true", help="Selecionar um bot disponível manualmente.")

    @can_send_message_check()
    @commands.bot_has_guild_permissions(send_messages=True)
    @check_voice()
    @commands.max_concurrency(1, commands.BucketType.member)
    @pool_command(
        name="play",
        description="Tocar música em um canal de voz.",
        aliases=["p"],
        check_player=False,
        cooldown=play_cd,
        max_concurrency=play_mc,
        extras={"flags": stage_flags},
        usage="{prefix}{cmd} [nome|link]\nEx: {prefix}{cmd} sekai - burn me down",
    )
    async def play_legacy(self, ctx: CustomContext, *, flags: str = ""):
        args, unknown = ctx.command.extras["flags"].parse_known_args(flags.split())

        await self.play.callback(
            self=self,
            inter=ctx,
            query=" ".join(args.query + unknown),
            position=1 if args.next else args.position if args.position > 0 else 0,
            options="shuffle" if args.shuffle else "reversed" if args.reverse else None,
            force_play="yes" if args.force else "no",
            manual_selection=args.select,
            server=args.server,
            manual_bot_choice="yes" if args.selectbot else "no",
            mix=args.mix,
        )

    @can_send_message_check()
    @commands.bot_has_guild_permissions(send_messages=True)
    @check_voice()
    @pool_command(
        name="search",
        description="Pesquisar por músicas e escolher uma entre os resultados para tocar.",
        aliases=["sc"],
        check_player=False,
        cooldown=play_cd,
        max_concurrency=play_mc,
        usage="{prefix}{cmd} [nome]\nEx: {prefix}{cmd} sekai - burn me down",
    )
    async def search_legacy(self, ctx: CustomContext, *, query):
        await self.play.callback(
            self=self,
            inter=ctx,
            query=query,
            position=0,
            options=False,
            force_play="no",
            manual_selection=True,
            server=None,
        )

    @can_send_message_check()
    @check_voice()
    @commands.slash_command(
        name="play_music_file",
        description=localized("cmd.play_music_file.description"),
        extras={"check_player": False},
        cooldown=play_cd,
        max_concurrency=play_mc,
    )
    @commands.contexts(guild=True)
    async def play_file(
        self,
        inter: Union[disnake.ApplicationCommandInteraction, CustomContext],
        file: disnake.Attachment = commands.Param(
            name=localized("cmd.play_music_file.opt.file.name"),
            description=localized("cmd.play_music_file.opt.file.description"),
        ),
        position: int = commands.Param(
            name=localized("cmd.play_music_file.opt.position.name"),
            description=localized("cmd.play_music_file.opt.position.description"),
            default=0,
        ),
        force_play: str = commands.Param(
            name=localized("cmd.play_music_file.opt.play_now.name"),
            description=localized("cmd.play_music_file.opt.play_now.description"),
            default="no",
            choices=[disnake.OptionChoice(localized("cmd.play_music_file.opt.play_now.choice.yes"), "yes")],
        ),
        server: str = commands.Param(
            name=localized("cmd.play_music_file.opt.server.name"),
            description=localized("cmd.play_music_file.opt.server.description"),
            default=None,
        ),
        manual_bot_choice: str = commands.Param(
            name=localized("cmd.play_music_file.opt.select_bot.name"),
            description=localized("cmd.play_music_file.opt.select_bot.description"),
            default="no",
            choices=[disnake.OptionChoice(localized("cmd.play_music_file.opt.select_bot.choice.yes"), "yes")],
        ),
    ):
        class DummyMessage:
            attachments = [file]

        try:
            thread = inter.message.thread
        except Exception:
            thread = None
        inter.message = DummyMessage()
        inter.message.thread = thread

        await self.play.callback(
            self=self,
            inter=inter,
            query="",
            position=position,
            options=False,
            force_play=force_play,
            manual_selection=False,
            server=server,
            manual_bot_choice=manual_bot_choice,
        )

    async def check_player_queue(
        self, user: disnake.User, bot: BotCore, guild_id: int, tracks: Union[list, LavalinkPlaylist] = None
    ):
        count = self.bot.config["QUEUE_MAX_ENTRIES"]

        try:
            player: LavalinkPlayer = bot.music.players[guild_id]
        except KeyError:
            if count < 1:
                return tracks
            count += 1
        else:
            if count < 1:
                return tracks
            if len(player.queue) >= count and not (await bot.is_owner(user)):
                raise GenericError(f"**A fila está cheia ({self.bot.config['QUEUE_MAX_ENTRIES']} músicas).**")

        if tracks:
            if isinstance(tracks, list):
                if not await bot.is_owner(user):
                    tracks = tracks[:count]
            else:
                if not await bot.is_owner(user):
                    tracks.tracks = tracks.tracks[:count]

        return tracks
