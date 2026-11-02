# -*- coding: utf-8 -*-
import logging
import asyncio
from typing import Union, Optional

import disnake
from async_timeout import timeout
from disnake.ext import commands

from empiremuse.i18n import localized
from empiremuse.utils.db import DBModel
from empiremuse.utils.music.checks import (
    check_voice,
    can_send_message_check,
    can_send_message,
    can_connect,
    check_pool_bots,
    check_channel_limit,
    check_player_perm,
)
from empiremuse.utils.music.converters import (
    URL_REG,
    google_search,
)
from empiremuse.utils.music.errors import (
    GenericError,
    NoVoice,
    DiffVoiceChannel,
)
from empiremuse.utils.music.saved_queue import has_saved_queue, load_saved_queue
from empiremuse.utils.music.interactions import (
    SelectBotVoice,
)
from empiremuse.utils.others import (
    CustomContext,
    update_inter,
)

# O corpo da classe usa estes nomes, então eles precisam existir neste módulo.
from empiremuse.music_cog.shared import (
    play_cd,
    play_mc,
)

from empiremuse.music_cog.play_enqueue import PlayEnqueueMixin
from empiremuse.music_cog.play_menus import PlayMenusMixin
from empiremuse.music_cog.play_resolve import PlayResolveMixin
from empiremuse.music_cog.play_state import PlayState

logger = logging.getLogger(__name__)


class PlayCommandMixin(PlayMenusMixin, PlayResolveMixin, PlayEnqueueMixin):
    @can_send_message_check()
    @check_voice()
    @commands.slash_command(
        description=localized("cmd.play.description"),
        extras={"check_player": False},
        cooldown=play_cd,
        max_concurrency=play_mc,
    )
    @commands.contexts(guild=True)
    async def play(
        self,
        inter: Union[disnake.ApplicationCommandInteraction, CustomContext],
        query: str = commands.Param(
            name=localized("cmd.play.opt.query.name"), description=localized("cmd.play.opt.query.description")
        ),
        *,
        position: int = commands.Param(
            name=localized("cmd.play.opt.position.name"),
            description=localized("cmd.play.opt.position.description"),
            default=0,
        ),
        force_play: str = commands.Param(
            name=localized("cmd.play.opt.play_now.name"),
            description=localized("cmd.play.opt.play_now.description"),
            default="no",
            choices=[disnake.OptionChoice(localized("cmd.play.opt.play_now.choice.yes"), "yes")],
        ),
        mix: str = commands.Param(
            name=localized("cmd.play.opt.recommended.name"),
            description=localized("cmd.play.opt.recommended.description"),
            default=False,
            choices=[disnake.OptionChoice(localized("cmd.play.opt.recommended.choice.yes"), "yes")],
        ),
        manual_selection: bool = commands.Param(
            name=localized("cmd.play.opt.select_manually.name"),
            description=localized("cmd.play.opt.select_manually.description"),
            default=False,
        ),
        options: str = commands.Param(
            name=localized("cmd.play.opt.options.name"),
            description=localized("cmd.play.opt.options.description"),
            choices=[
                disnake.OptionChoice(localized("cmd.play.opt.options.choice.shuffle"), "shuffle"),
                disnake.OptionChoice(localized("cmd.play.opt.options.choice.reversed"), "reversed"),
            ],
            default=False,
        ),
        server: str = commands.Param(
            name=localized("cmd.play.opt.server.name"),
            description=localized("cmd.play.opt.server.description"),
            default=None,
        ),
        manual_bot_choice: str = commands.Param(
            name=localized("cmd.play.opt.select_bot.name"),
            description=localized("cmd.play.opt.select_bot.description"),
            default="no",
            choices=[disnake.OptionChoice(localized("cmd.play.opt.select_bot.choice.yes"), "yes")],
        ),
    ):
        req = PlayState(
            inter=inter,
            query=query,
            position=position,
            force_play=force_play,
            mix=mix,
            manual_selection=manual_selection,
            options=options,
            server=server,
            manual_bot_choice=manual_bot_choice,
        )
        await self._play_prepare(req)

        if await self._play_select_bot(req):
            return

        await self._play_check_channel(req)

        await self._play_init_request(req)

        await self._play_user_context(req)

        if not req.query:
            await self._play_menu_options(req)

            if await self._play_ask_menu(req):
                return

        await self._play_fav_options(req)

        if await self._play_fav_select(req):
            return

        await self._play_resolve_prefix(req)

        await self._play_profile_playlists(req)

        if await self._play_profile_select(req):
            return

        if req.query.startswith(">> [💾 Fila Salva 💾] <<"):
            data = await load_saved_queue(req.inter.author.id, req.inter.guild_id)

            req.tracks = await self.check_player_queue(
                req.inter.author, req.bot, req.guild.id, self.bot.pool.process_track_cls(data["tracks"])[0]
            )
            req.node = await self.get_best_node(req.bot)
            req.queue_loaded = True
            req.source = False

        else:
            req.query = req.query.strip("<>")

            req.urls = URL_REG.findall(req.query)

            req.reg_query = {}

            if req.urls:
                if await self._play_url_query(req):
                    return

            else:
                if await self._play_ask_source(req):
                    return

        await self._play_search_tracks(req)

        if isinstance(req.tracks, list):
            if await self._play_select_tracks(req):
                return

            if await self._play_ensure_player(req):
                return

            if not isinstance(req.tracks, list):
                self._play_enqueue_track(req)

            else:
                self._play_enqueue_tracks(req)

        else:
            if await self._play_ensure_player(req):
                return

            self._play_enqueue_playlist(req)

            await self._play_playlist_artwork(req)

            self._play_playlist_embed(req)

        await self._play_reply(req)

        await self._play_connect_and_process(req)

    @play.autocomplete("query")
    async def fav_add_autocomplete(self, inter: disnake.Interaction, query: str):
        if not self.bot.is_ready() or URL_REG.match(query) or URL_REG.match(query):
            return [query] if len(query) < 100 else []

        favs = [">> [⭐ Favoritos ⭐] <<", ">> [💠 Integrações 💠] <<", ">> [📌 Favoritos do servidor 📌] <<"]

        if has_saved_queue(inter.author.id):
            favs.append(">> [💾 Fila Salva 💾] <<")

        if not inter.guild_id:
            try:
                await check_pool_bots(inter, return_first=True)
            except Exception:
                return [query] if len(query) < 100 else []

        try:
            vc = inter.author.voice
        except AttributeError:
            vc = True

        user_data = await self.bot.get_global_data(inter.author.id, db_name=DBModel.users)

        favs.extend(
            reversed(
                [
                    (f"{rec['url']} || {rec['name']}"[:100] if len(rec["url"]) < 101 else rec["name"][:100])
                    for rec in user_data["last_tracks"]
                    if rec.get("url")
                ]
            )
        )

        if not vc or not query:
            return favs[:20]

        return await google_search(self.bot, query, max_entries=20) or favs[:20]

    async def _play_prepare(self, req: "PlayState") -> None:
        """Resolve bot, servidor e autor e adia a resposta da interação."""
        inter = req.inter
        mix = req.mix

        try:
            bot = inter.music_bot
            guild = inter.music_guild
            author = guild.get_member(inter.author.id)
        except AttributeError:
            bot = inter.bot
            guild = inter.guild
            author = inter.author

        original_bot = bot

        mix = mix == "yes" or mix is True

        msg = None
        guild_data = await bot.get_data(guild.id, db_name=DBModel.guilds)  # o documento do SERVIDOR (era o id do autor)
        ephemeral = None

        if not inter.response.is_done():
            try:
                async with timeout(1.5):
                    ephemeral = await self.is_jukebox_channel(inter, data=guild_data, ignore_thread=True)
            except asyncio.TimeoutError:
                ephemeral = True
            await inter.response.defer(ephemeral=ephemeral, with_message=True)

        req.author = author
        req.bot = bot
        req.ephemeral = ephemeral
        req.guild = guild
        req.guild_data = guild_data
        req.mix = mix
        req.msg = msg
        req.original_bot = original_bot

    async def _play_select_bot(self, req: "PlayState") -> bool:
        """Escolhe o bot livre que atenderá o pedido quando o bot atual não está na call do autor."""
        author = req.author
        bot = req.bot
        guild = req.guild
        guild_data = req.guild_data
        inter = req.inter
        manual_bot_choice = req.manual_bot_choice
        msg = req.msg

        if guild.me.voice and bot.user.id not in author.voice.channel.voice_states:
            if str(inter.channel.id) == guild_data["player_controller"]["channel"]:
                try:
                    if inter.author.id not in bot.music.players[guild.id].last_channel.voice_states:
                        raise DiffVoiceChannel()
                except KeyError, AttributeError:
                    pass

            else:
                free_bots = await self.check_available_bot(inter=inter, guild=guild, bot=bot, message=msg)

                if len(free_bots) > 1 and manual_bot_choice == "yes":
                    v = SelectBotVoice(inter, guild, free_bots)

                    try:
                        func = msg.edit
                    except AttributeError:
                        try:
                            func = inter.edit_original_message
                        except AttributeError:
                            func = inter.send

                    newmsg = await func(
                        embed=disnake.Embed(
                            description=f"**Escolha qual bot você deseja usar no canal {author.voice.channel.mention}**",
                            color=self.bot.get_color(guild.me),
                        ),
                        view=v,
                    )
                    await v.wait()

                    if newmsg:
                        msg = newmsg

                    if v.status is None:
                        try:
                            func = msg.edit
                        except AttributeError:
                            func = inter.edit_original_message
                        try:
                            await func(
                                embed=disnake.Embed(
                                    description="### Tempo esgotado...", color=self.bot.get_color(guild.me)
                                ),
                                view=None,
                            )
                        except Exception:
                            logger.exception("Falha em _play_select_bot")
                        return True

                    if v.status is False:
                        try:
                            func = msg.edit
                        except AttributeError:
                            func = inter.edit_original_message
                        await func(
                            embed=disnake.Embed(
                                description="### Operação cancelada.", color=self.bot.get_color(guild.me)
                            ),
                            view=None,
                        )
                        return True

                    if not author.voice:
                        try:
                            func = msg.edit
                        except AttributeError:
                            func = inter.edit_original_message
                        await func(
                            embed=disnake.Embed(
                                description="### Você não está conectado em um canal de voz...",
                                color=self.bot.get_color(guild.me),
                            ),
                            view=None,
                        )
                        return True

                    update_inter(inter, v.inter)

                    current_bot = v.bot
                    inter = v.inter
                    guild = v.guild

                    await inter.response.defer()

                else:
                    try:
                        current_bot = free_bots.pop(0)
                    except Exception:
                        return True

                if bot != current_bot:
                    guild_data = await current_bot.get_data(guild.id, db_name=DBModel.guilds)

                bot = current_bot

        req.bot = bot
        req.guild = guild
        req.guild_data = guild_data
        req.inter = inter
        req.msg = msg
        return False

    async def _play_check_channel(self, req: "PlayState") -> None:
        """Valida canal de texto, permissões e lotação do canal de voz."""
        author = req.author
        bot = req.bot
        guild = req.guild
        guild_data = req.guild_data
        inter = req.inter

        channel = bot.get_channel(inter.channel.id)

        can_send_message(channel, bot.user)

        await check_player_perm(inter=inter, bot=bot, channel=channel, guild_data=guild_data)

        if not guild.voice_client and not check_channel_limit(guild.me, author.voice.channel):
            raise GenericError(f"**O canal {author.voice.channel.mention} está lotado!**")

        await self.check_player_queue(inter.author, bot, guild.id)

        req.channel = channel

    async def _play_init_request(self, req: "PlayState") -> None:
        """Normaliza a busca e resolve player, nó e dados do servidor."""
        author = req.author
        bot = req.bot
        channel = req.channel
        ephemeral = req.ephemeral
        guild = req.guild
        inter = req.inter
        player = req.player
        query = req.query
        server = req.server

        query = query.replace("\n", " ").strip()
        warn_message = None
        queue_loaded = False
        reg_query = None
        image_file = None

        try:
            if isinstance(inter.message, disnake.Message):
                message_inter = inter.message
            else:
                message_inter = None
        except AttributeError:
            message_inter = None

        try:
            modal_message_id = int(inter.data.custom_id[15:])
        except Exception:
            modal_message_id = None

        attachment: Optional[disnake.Attachment] = None

        try:
            voice_channel: disnake.VoiceChannel = bot.get_channel(author.voice.channel.id)
        except AttributeError:
            raise NoVoice()

        try:
            player = bot.music.players[guild.id]

            if not server:
                node = player.node
            else:
                node = bot.music.get_node(server) or player.node

            guild_data = {}

        except KeyError:
            node = bot.music.get_node(server)

            if not node:
                node = await self.get_best_node(bot)

            guild_data = await bot.get_data(inter.guild_id, db_name=DBModel.guilds)

            if not guild.me.voice:
                can_connect(voice_channel, guild, guild_data["check_other_bots_in_vc"], bot=bot)

            static_player = guild_data["player_controller"]

            if not inter.response.is_done():
                ephemeral = await self.is_jukebox_channel(inter, data=guild_data, ignore_thread=True)
                await inter.response.defer(ephemeral=ephemeral)

            if static_player["channel"]:
                channel, warn_message, message = await self.check_channel(guild_data, inter, channel, guild, bot)

        req.attachment = attachment
        req.channel = channel
        req.ephemeral = ephemeral
        req.guild_data = guild_data
        req.image_file = image_file
        req.message_inter = message_inter
        req.modal_message_id = modal_message_id
        req.node = node
        req.player = player
        req.query = query
        req.queue_loaded = queue_loaded
        req.reg_query = reg_query
        req.voice_channel = voice_channel
        req.warn_message = warn_message

    async def _play_user_context(self, req: "PlayState") -> None:
        """Carrega os dados do usuário e prepara os links dos comandos."""
        attachment = req.attachment
        ephemeral = req.ephemeral
        guild_data = req.guild_data
        inter = req.inter
        query = req.query

        if ephemeral is None:
            ephemeral = await self.is_jukebox_channel(inter, data=guild_data, ignore_thread=True)

        is_pin = None

        original_query = query or ""

        if not query:
            if self.bot.config["ENABLE_DISCORD_URLS_PLAYBACK"]:
                try:
                    attachment = inter.message.attachments[0]

                    if attachment.size > 18000000:
                        raise GenericError("**O arquivo que você enviou deve ter o tamanho igual ou inferior a 18mb.**")

                    if attachment.content_type not in self.audio_formats:
                        raise GenericError("**O arquivo que você enviou não é um arquivo de música válido...**")

                    query = attachment.url

                except IndexError:
                    pass

        user_data = await self.bot.get_global_data(inter.author.id, db_name=DBModel.users)

        try:
            fav_slashcmd = (
                f"</fav_manager:"
                + str(
                    self.bot.get_global_command_named(
                        "fav_manager", cmd_type=disnake.ApplicationCommandType.chat_input
                    ).id
                )
                + ">"
            )
        except AttributeError:
            fav_slashcmd = "/fav_manager"

        try:
            savequeue_slashcmd = (
                f"</save_queue:"
                + str(
                    self.bot.get_global_command_named(
                        "save_queue", cmd_type=disnake.ApplicationCommandType.chat_input
                    ).id
                )
                + ">"
            )
        except AttributeError:
            savequeue_slashcmd = "/save_queue"

        req.attachment = attachment
        req.ephemeral = ephemeral
        req.fav_slashcmd = fav_slashcmd
        req.is_pin = is_pin
        req.original_query = original_query
        req.query = query
        req.savequeue_slashcmd = savequeue_slashcmd
        req.user_data = user_data
