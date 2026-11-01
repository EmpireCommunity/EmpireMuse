# -*- coding: utf-8 -*-
"""Montagem da fila, resposta ao usuário e conexão do `/play`."""

import logging
import asyncio
from io import BytesIO
from random import shuffle
from typing import TYPE_CHECKING, Union

import aiohttp
import disnake
from yt_dlp import YoutubeDL

from empiremuse import paths
from empiremuse.utils.branding import asset_url
from empiremuse.utils.db import DBModel
from empiremuse.utils.music.checks import (
    can_send_message,
    check_pool_bots,
    check_player_perm,
)
from empiremuse.utils.music.converters import time_format, fix_characters, URL_REG, music_source_image, md_link, md_code
from empiremuse.utils.music.errors import (
    NoVoice,
)
from empiremuse.utils.others import (
    CustomContext,
    PlayerControls,
    update_inter,
)
from empiremuse.utils.music.app_emojis import emoji as app_emoji

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from empiremuse.music_cog.play_state import PlayState

# O corpo da classe usa estes nomes, então eles precisam existir neste módulo.


class PlayEnqueueMixin:
    async def _play_search_tracks(self, req: "PlayState") -> None:
        """Busca as faixas, resolve o bot do player e prepara o embed da resposta."""
        bot = req.bot
        ephemeral = req.ephemeral
        guild = req.guild
        guild_data = req.guild_data
        inter = req.inter
        mix = req.mix
        node = req.node
        player = req.player
        position = req.position
        query = req.query
        queue_loaded = req.queue_loaded
        source = req.source
        tracks = req.tracks
        warn_message = req.warn_message

        if not inter.response.is_done():
            await inter.response.defer(ephemeral=ephemeral)

        if not queue_loaded:
            tracks, node = await self.get_tracks(query, inter, inter.author, node=node, source=source, bot=bot, mix=mix)
            tracks = await self.check_player_queue(inter.author, bot, guild.id, tracks)

        try:
            player = bot.music.players[guild.id]
        except KeyError:
            new_bot, guild = await check_pool_bots(inter, check_player=False, bypass_prefix=True, bypass_attribute=True)
            channel = bot.get_channel(inter.channel.id)

            try:
                new_bot.music.players[guild.id]
            except KeyError:
                if new_bot != bot or not guild_data:
                    guild_data = await new_bot.get_data(guild.id, db_name=DBModel.guilds)

                static_player = guild_data["player_controller"]

                if static_player["channel"]:
                    channel, warn_message, message = await self.check_channel(
                        guild_data, inter, channel, guild, new_bot
                    )

            bot = new_bot

        channel = bot.get_channel(inter.channel.id)

        can_send_message(channel, bot.user)

        pos_txt = ""

        embed = disnake.Embed(color=disnake.Colour.red())

        embed.colour = self.bot.get_color(guild.me)

        position -= 1

        embed_description = ""

        track_url = ""

        req.bot = bot
        req.channel = channel
        req.embed = embed
        req.embed_description = embed_description
        req.guild = guild
        req.guild_data = guild_data
        req.node = node
        req.player = player
        req.pos_txt = pos_txt
        req.position = position
        req.track_url = track_url
        req.tracks = tracks
        req.warn_message = warn_message

    async def _play_select_tracks(self, req: "PlayState") -> bool:
        """Aplica a seleção manual de faixas ou fica com o primeiro resultado."""
        attachment = req.attachment
        embed = req.embed
        inter = req.inter
        manual_selection = req.manual_selection
        msg = req.msg
        original_query = req.original_query
        queue_loaded = req.queue_loaded
        reg_query = req.reg_query
        track_url = req.track_url
        tracks = req.tracks

        if self.bot.pool.song_select_cooldown.get_bucket(inter).get_retry_after() > 0:
            manual_selection = True

        if not queue_loaded and len(tracks) > 1 and manual_selection:
            embed.description = f"**Selecione a(s) música(s) desejada(s) abaixo:**"

            try:
                func = inter.edit_original_message
            except AttributeError:
                func = inter.send

            try:
                add_id = f"_{inter.id}"
            except AttributeError:
                add_id = ""

            tracks = tracks[:25]

            msg = await func(
                embed=embed,
                components=[
                    disnake.ui.Select(
                        placeholder="Resultados:",
                        custom_id=f"track_selection{add_id}",
                        min_values=1,
                        max_values=len(tracks),
                        required=False,
                        options=[
                            disnake.SelectOption(
                                label=f"{n + 1}. {t.title[:96]}",
                                value=f"track_select_{n}",
                                description=f"{t.author[:70]} [{time_format(t.duration)}]",
                            )
                            for n, t in enumerate(tracks)
                        ],
                    )
                ],
            )

            def check_song_selection(i: Union[CustomContext, disnake.MessageInteraction]):
                try:
                    return i.data.custom_id == f"track_selection_{inter.id}" and i.author == inter.author
                except AttributeError:
                    return i.author == inter.author and i.message.id == msg.id

            try:
                select_interaction: disnake.MessageInteraction = await self.bot.wait_for(
                    "dropdown", timeout=45, check=check_song_selection
                )
            except asyncio.TimeoutError:
                try:
                    func = inter.edit_original_message
                except AttributeError:
                    func = msg.edit
                try:
                    await func(
                        embed=disnake.Embed(color=disnake.Colour.red(), description="**Tempo esgotado!**"),
                        view=None,
                    )
                except disnake.NotFound:
                    pass
                return True

            update_inter(inter, select_interaction)

            if len(select_interaction.data.values) > 1:
                indexes = set(int(v[13:]) for v in select_interaction.data.values)

                selected_tracks = []

                for i in indexes:
                    for n, t in enumerate(tracks):
                        if i == n:
                            selected_tracks.append(t)
                            break

                tracks = selected_tracks

            else:
                tracks = tracks[int(select_interaction.data.values[0][13:])]

            if isinstance(inter, CustomContext):
                inter.message = msg

            if reg_query is not None:
                try:
                    reg_query = {"name": tracks.title, "url": tracks.uri}
                except AttributeError:
                    reg_query = {"name": tracks[0].title, "url": tracks[0].uri}

                if not reg_query["url"]:
                    reg_query = None

            await select_interaction.response.defer()

            inter = select_interaction

        elif not queue_loaded:
            tracks = tracks[0]

            if tracks.info.get("sourceName") == "http":
                if tracks.title == "Unknown title":
                    if attachment:
                        tracks.info["title"] = attachment.filename
                    else:
                        tracks.info["title"] = tracks.uri.split("/")[-1]
                    tracks.title = tracks.info["title"]

                tracks.info["uri"] = ""

            elif url_check := URL_REG.match(original_query.strip("<>")):
                track_url = url_check.group()

        req.inter = inter
        req.manual_selection = manual_selection
        req.msg = msg
        req.reg_query = reg_query
        req.track_url = track_url
        req.tracks = tracks
        return False

    async def _play_ensure_player(self, req: "PlayState") -> bool:
        """Garante o bot, as permissões e o player do canal de voz."""
        author = req.author
        bot = req.bot
        channel = req.channel
        guild = req.guild
        guild_data = req.guild_data
        inter = req.inter
        message_inter = req.message_inter
        modal_message_id = req.modal_message_id
        msg = req.msg
        node = req.node
        voice_channel = req.voice_channel

        if not author.voice:
            raise NoVoice()

        if inter.author.id not in voice_channel.voice_states and bot.user.id not in voice_channel.voice_states:
            if not (free_bots := await self.check_available_bot(inter=inter, guild=guild, bot=bot, message=msg)):
                return True

            if free_bots[0] != bot:
                try:
                    voice_channel = bot.get_channel(author.voice.channel.id)
                except AttributeError:
                    raise NoVoice()
                bot = free_bots.pop(0)
                channel = bot.get_channel(channel.id)
                guild = bot.get_guild(guild.id)
                guild_data = await bot.get_data(guild.id, db_name=DBModel.guilds)
                node = None

        await check_player_perm(inter=inter, bot=bot, channel=channel, guild_data=guild_data)

        try:
            player = bot.music.players[guild.id]
        except KeyError:
            player = await self.create_player(
                inter=inter,
                bot=bot,
                guild=guild,
                guild_data=guild_data,
                channel=channel,
                message_inter=message_inter,
                node=node,
                modal_message_id=modal_message_id,
            )

        req.bot = bot
        req.channel = channel
        req.guild = guild
        req.guild_data = guild_data
        req.node = node
        req.player = player
        req.voice_channel = voice_channel
        return False

    def _play_enqueue_track(self, req: "PlayState") -> None:
        """Adiciona a faixa única à fila e monta o embed."""
        embed = req.embed
        force_play = req.force_play
        inter = req.inter
        player = req.player
        pos_txt = req.pos_txt
        position = req.position
        reg_query = req.reg_query
        track_url = req.track_url
        tracks = req.tracks

        if force_play == "yes":
            player.queue.insert(0, tracks)
        elif position < 0:
            player.queue.append(tracks)
        else:
            player.queue.insert(position, tracks)
            pos_txt = f" na posição {position + 1} da fila"

        duration = time_format(tracks.duration) if not tracks.is_stream else "🔴 Livestream"

        if not track_url:
            track_url = tracks.uri or tracks.search_uri

        log_text = (
            f"{inter.author.mention} adicionou {md_link(tracks.title, track_url, limit=20)}{pos_txt} `({duration})`."
        )

        loadtype = "track"

        embed.set_author(
            name="⠂" + fix_characters(tracks.single_title, 35),
            url=track_url,
            icon_url=music_source_image(tracks.info["sourceName"]),
        )
        embed.set_thumbnail(url=tracks.thumb)
        embed.description = f"{md_code(tracks.author, limit=15)}**┃**`{time_format(tracks.duration) if not tracks.is_stream else '🔴 Livestream'}`**┃**{inter.author.mention}"
        emoji = "🎵"
        if reg_query is not None and tracks.uri:
            reg_query = {"name": tracks.title, "url": tracks.uri}

        req.emoji = emoji
        req.loadtype = loadtype
        req.log_text = log_text
        req.pos_txt = pos_txt
        req.reg_query = reg_query
        req.track_url = track_url

    def _play_enqueue_tracks(self, req: "PlayState") -> None:
        """Adiciona a lista de faixas (busca ou fila salva) e monta o embed."""
        embed = req.embed
        embed_description = req.embed_description
        inter = req.inter
        options = req.options
        player = req.player
        pos_txt = req.pos_txt
        position = req.position
        query = req.query
        queue_loaded = req.queue_loaded
        source = req.source
        tracks = req.tracks

        if options == "shuffle":
            shuffle(tracks)

        if position < 0 or len(tracks) < 2:
            if options == "reversed":
                tracks.reverse()
            for track in tracks:
                player.queue.append(track)
        else:
            if options != "reversed":
                tracks.reverse()
            for track in tracks:
                player.queue.insert(position, track)

            pos_txt = f" (Pos. {position + 1})"

        total_duration = 0

        for t in tracks:
            if not t.is_stream:
                total_duration += t.duration

        if queue_loaded:
            log_text = f"{inter.author.mention} adicionou `{len(tracks)} músicas` via: {query[7:]}."
            title = f"Usando músicas salvas de {inter.author.display_name}"
            icon_url = asset_url("ui_floppy_save_icon.png")

            desc = ""

            tracks_playlists = {}

            for t in tracks:
                if t.playlist_name:
                    try:
                        tracks_playlists[t.playlist_url]["count"] += 1
                    except KeyError:
                        tracks_playlists[t.playlist_url] = {"name": t.playlist_name, "count": 1}

            if tracks_playlists:
                embed_description += (
                    "\n### Playlists carregadas:\n"
                    + "\n".join(
                        f"{md_link(info['name'], url)} `- {info['count']} música{'s'[: info['count'] ^ 1]}` "
                        for url, info in tracks_playlists.items()
                    )
                    + "\n"
                )

        else:
            query = fix_characters(query.replace(f"{source}:", "", 1), 25)
            title = "Músicas adicionadas:"
            icon_url = music_source_image(tracks[0].info["sourceName"])
            log_text = f"{inter.author.mention} adicionou `{len(tracks)} músicas` via busca: `{query}`{pos_txt}."
            desc = "\n".join(
                f"` {c + 1}. ` {md_link(t.title, t.uri, limit=75)} `{time_format(t.duration)}`"
                for c, t in enumerate(tracks)
            )

        embed.set_author(name="⠂" + title, icon_url=icon_url)
        embed.set_thumbnail(url=tracks[0].thumb)
        embed.description = (
            desc
            or f"`{(tcount := len(tracks))} música{'s'[: tcount ^ 1]}`**┃**`{time_format(total_duration)}`**┃**{inter.author.mention}"
        )
        emoji = "🎶"

        req.embed_description = embed_description
        req.emoji = emoji
        req.log_text = log_text
        req.pos_txt = pos_txt
        req.query = query

    def _play_enqueue_playlist(self, req: "PlayState") -> None:
        """Insere as faixas da playlist na fila."""
        options = req.options
        player = req.player
        pos_txt = req.pos_txt
        position = req.position
        tracks = req.tracks

        if options == "shuffle":
            shuffle(tracks.tracks)

        if position < 0 or len(tracks.tracks) < 2:
            if options == "reversed":
                tracks.tracks.reverse()
            for track in tracks.tracks:
                player.queue.append(track)
        else:
            if options != "reversed":
                tracks.tracks.reverse()
            for track in tracks.tracks:
                player.queue.insert(position, track)

            pos_txt = f" (Pos. {position + 1})"

        req.pos_txt = pos_txt

    async def _play_playlist_artwork(self, req: "PlayState") -> None:
        """Obtém a arte da playlist do YouTube."""
        bot = req.bot
        image_file = req.image_file
        inter = req.inter
        query = req.query
        tracks = req.tracks

        if tracks.tracks[0].info["sourceName"] == "youtube":
            try:
                q = f"https://www.youtube.com/playlist?list={query.split('&list=')[1]}"
            except Exception:
                q = query

            if not await bot.is_owner(inter.author):
                try:
                    async with bot.session.get((oembed_url := f"https://www.youtube.com/oembed?url={q}")) as r:
                        try:
                            playlist_data = await r.json()
                        except Exception:
                            raise Exception(f"{r.status} | {await r.text()}")
                        else:
                            tracks.data["playlistInfo"]["thumb"] = playlist_data["thumbnail_url"]
                except Exception as e:
                    logger.warning("Falha ao obter artwork da playlist (oembed): %s", type(e).__name__)

            else:
                try:
                    with YoutubeDL(
                        {
                            "extract_flat": True,
                            "quiet": True,
                            "no_warnings": True,
                            "lazy_playlist": True,
                            "simulate": True,
                            "playlistend": 0,
                            "cachedir": str(paths.RUNTIME_DIR / ".ytdl_cache"),
                            "allowed_extractors": [
                                r".*youtube.*",
                            ],
                            "extractor_args": {"youtubetab": {"skip": ["webpage"]}},
                        }
                    ) as ydl:
                        playlist_data = await bot.loop.run_in_executor(
                            None, lambda: ydl.extract_info(q, download=False)
                        )

                    async with aiohttp.ClientSession() as session:
                        async with session.get(playlist_data["thumbnails"][0]["url"]) as response:
                            if response.status != 200:
                                response.raise_for_status()

                            image_file = disnake.File(
                                fp=BytesIO(await response.read()), filename=f"{playlist_data['id']}.jpg"
                            )

                    tracks.data["playlistInfo"]["thumb"] = playlist_data["thumbnails"][0]["url"]
                except Exception as e:
                    logger.warning("Falha ao obter artwork da playlist: %s", type(e).__name__)

        req.image_file = image_file

    def _play_playlist_embed(self, req: "PlayState") -> None:
        """Monta o embed e o texto de log da playlist."""
        embed = req.embed
        image_file = req.image_file
        inter = req.inter
        pos_txt = req.pos_txt
        reg_query = req.reg_query
        tracks = req.tracks

        loadtype = "playlist"

        log_text = f"{inter.author.mention} adicionou a playlist {md_link(tracks.name, tracks.url, limit=20)}{pos_txt} `({len(tracks.tracks)})`."

        total_duration = 0

        for t in tracks.tracks:
            if not t.is_stream:
                total_duration += t.duration

        try:
            embed.set_author(
                name="⠂" + fix_characters(tracks.name, 35),
                url=tracks.url,
                icon_url=music_source_image(tracks.tracks[0].info["sourceName"]),
            )
        except KeyError:
            embed.set_author(
                name="⠂ Spotify Playlist", icon_url=music_source_image(tracks.tracks[0].info["sourceName"])
            )

        if image_file:
            embed.set_thumbnail(f"attachment://{image_file.filename}")
        else:
            embed.set_thumbnail(url=tracks.thumb)
        embed.description = f"`{(tcount := len(tracks.tracks))} música{'s'[: tcount ^ 1]}`**┃**`{time_format(total_duration)}`**┃**{inter.author.mention}"
        emoji = "🎶"

        if reg_query is not None and tracks.uri:
            reg_query = {"name": tracks.name, "url": tracks.uri}

        req.emoji = emoji
        req.loadtype = loadtype
        req.log_text = log_text
        req.reg_query = reg_query

    async def _play_reply(self, req: "PlayState") -> None:
        """Responde ao usuário com o embed e os botões."""
        bot = req.bot
        embed = req.embed
        embed_description = req.embed_description
        image_file = req.image_file
        inter = req.inter
        is_pin = req.is_pin
        loadtype = req.loadtype
        mix = req.mix
        msg = req.msg
        original_bot = req.original_bot
        player = req.player
        user_data = req.user_data
        voice_channel = req.voice_channel

        embed.description += player.controller_link

        player.queue_autoplay.clear()

        if not is_pin:
            if not player.is_connected:
                try:
                    embed.description += f"\n`Canal de voz:` {voice_channel.mention}"
                except AttributeError:
                    pass

            embed.description += embed_description

            try:
                func = inter.edit_original_message
            except AttributeError:
                if msg:
                    func = msg.edit
                elif inter.message.author.id == bot.user.id:
                    func = inter.message.edit
                else:
                    func = inter.send

            footer_txt = (
                f"`♾️` {md_link(user_data['lastfm']['username'], f'https://www.last.fm/user/{user_data["lastfm"]["username"]}')}"
                if user_data["lastfm"]["sessionkey"] and user_data["lastfm"]["scrobble"]
                else ""
            )

            try:
                if original_bot.user.id != self.bot.user.id:
                    embed.description += f"\n-# **Via:** {bot.user.mention}" + (f" ⠂{footer_txt}" if footer_txt else "")
                elif footer_txt:
                    embed.description += f"\n-# {footer_txt}"
            except AttributeError:
                if footer_txt:
                    embed.description += f"\n-# {footer_txt}"

            if mix:
                components = []

            elif loadtype == "track":
                components = [
                    disnake.ui.Button(
                        emoji=app_emoji(self.bot, "loved"), label="Favoritar", custom_id=PlayerControls.embed_add_fav
                    ),
                    disnake.ui.Button(
                        emoji=app_emoji(self.bot, "play"),
                        label="Tocar" + (" agora" if (player.current and player.current.autoplay) else ""),
                        custom_id=PlayerControls.embed_forceplay,
                    ),
                    disnake.ui.Button(
                        emoji=app_emoji(self.bot, "add_music"),
                        label="Adicionar na fila",
                        custom_id=PlayerControls.embed_enqueue_track,
                    ),
                ]

            elif loadtype == "playlist":
                try:
                    self.bot.pool.enqueue_playlist_embed_cooldown.get_bucket(inter).update_rate_limit()
                except Exception:
                    pass
                components = [
                    disnake.ui.Button(
                        emoji=app_emoji(self.bot, "loved"), label="Favoritar", custom_id=PlayerControls.embed_add_fav
                    ),
                    disnake.ui.Button(
                        emoji=app_emoji(self.bot, "add_music"),
                        label="Adicionar na fila",
                        custom_id=PlayerControls.embed_enqueue_playlist,
                    ),
                ]
            else:
                components = None

            kw_embed = {"components": components} if components else {"view": None}
            if image_file:
                kw_embed["file"] = image_file
            await func(embed=embed, **kw_embed)

    async def _play_connect_and_process(self, req: "PlayState") -> None:
        """Conecta ao canal de voz, se preciso, e inicia o processamento da fila."""
        bot = req.bot
        emoji = req.emoji
        ephemeral = req.ephemeral
        force_play = req.force_play
        guild = req.guild
        guild_data = req.guild_data
        inter = req.inter
        log_text = req.log_text
        player = req.player
        reg_query = req.reg_query
        user_data = req.user_data
        voice_channel = req.voice_channel
        warn_message = req.warn_message

        if not player.is_connected:
            try:
                guild_data["check_other_bots_in_vc"]
            except KeyError:
                guild_data = await bot.get_data(guild.id, db_name=DBModel.guilds)

            if isinstance(voice_channel, disnake.StageChannel):
                player.stage_title_event = False

            await self.do_connect(
                inter,
                channel=voice_channel,
                check_other_bots_in_vc=guild_data["check_other_bots_in_vc"],
                bot=bot,
                me=player.guild.me,
            )

        await self.process_music(
            inter=inter,
            force_play=force_play,
            ephemeral=ephemeral,
            user_data=user_data,
            player=player,
            log_text=log_text,
            emoji=emoji,
            warn_message=warn_message,
            reg_query=reg_query,
        )

        req.guild_data = guild_data
