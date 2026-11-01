# -*- coding: utf-8 -*-
"""Etapas de `Music.song_requests` e `Music.parse_song_request` (pedidos de música por mensagem)."""

import asyncio
import datetime
import traceback
from typing import Union, Optional

import disnake
from disnake.ext import commands

from empiremuse.utils.client import BotCore
from empiremuse.utils.db import DBModel
from empiremuse.utils.request_panel import fetch_panel_message
from empiremuse.utils.music.checks import (
    can_connect,
    check_player_perm,
)
from empiremuse.utils.music.converters import (
    time_format,
    fix_characters,
    URL_REG,
    YOUTUBE_VIDEO_REG,
    music_source_image,
    md_link,
    md_code,
)
from empiremuse.utils.music.errors import (
    GenericError,
    PoolException,
    parse_error,
    error_code_text,
    log_unexpected_error,
)
from empiremuse.utils.music.interactions import (
    SelectInteraction,
    ButtonInteraction,
)
from empiremuse.utils.music.models import (
    LavalinkPlayer,
)
from empiremuse.utils.others import (
    send_idle_embed,
    CustomContext,
    PlayerControls,
    string_to_file,
    song_request_buttons,
    update_inter,
)

# O corpo da classe usa estes nomes, então eles precisam existir neste módulo.
from empiremuse.music_cog.shared import (
    logger,
)
from empiremuse.utils.music.app_emojis import emoji as app_emoji


async def _pr_checks(self, req):
    """Confere voz, permissões e obtém o player do pedido."""
    data = req.data
    message = req.message
    source = req.source
    # a mensagem é do bot do painel; quem executa pode ser outro bot: tudo que depende do "eu" (permissões, voz) vem do servidor DELE
    guild = self.bot.get_guild(message.guild.id) or message.guild
    text_channel = self.bot.get_channel(req.text_channel.id) or req.text_channel

    if not message.author.voice:
        raise GenericError("Você deve entrar em um canal de voz para pedir uma música.")

    voice_channel = guild.get_channel(message.author.voice.channel.id) or message.author.voice.channel

    can_connect(
        channel=voice_channel,
        guild=guild,
        check_other_bots_in_vc=data["check_other_bots_in_vc"],
        bot=self.bot,
    )

    try:
        if guild.me.voice.channel != voice_channel:
            raise GenericError(f"Você deve entrar no canal <#{guild.me.voice.channel.id}> para pedir uma música.")
    except AttributeError:
        pass

    try:
        player = self.bot.music.players[message.guild.id]
        await check_player_perm(message, self.bot, message.channel, guild_data=data)
        destroy_message = True
    except KeyError:
        destroy_message = False
        player = await self.create_player(
            inter=message, bot=self.bot, guild=guild, channel=text_channel, guild_data=data
        )

    tracks, node = await self.get_tracks(message.content, message, message.author, source=source)
    tracks = await self.check_player_queue(message.author, self.bot, message.guild.id, tracks)

    if not player.message:
        # o painel é do bot do painel (o autor da mensagem): só ele pode editá-la, então o player a recebe ligada a ele
        try:
            cached_message = await fetch_panel_message(self.bot.pool, message.guild.id)
        except Exception:
            cached_message = None
        if cached_message is None:
            cached_message = await self.bot.pool.request_panel.resend(message.guild.id)

        player.message = cached_message

    embed = disnake.Embed(color=self.bot.get_color(guild.me))

    try:
        components = [disnake.ui.Button(emoji="🎛️", label="Player-controller", url=player.message.jump_url)]
    except AttributeError:
        components = []

    req.components = components
    req.destroy_message = destroy_message
    req.embed = embed
    req.player = player
    req.tracks = tracks


async def _pr_playlist(self, req):
    """Adiciona a playlist pedida na fila e responde."""
    components = req.components
    destroy_message = req.destroy_message
    embed = req.embed
    message = req.message
    player = req.player
    response = req.response
    tracks = req.tracks

    player.queue.extend(tracks.tracks)

    if isinstance(message.channel, disnake.Thread) and not isinstance(message.channel.parent, disnake.ForumChannel):
        tcount = len(tracks.tracks)
        embed.description = f"{app_emoji(self.bot, 'user')} **⠂ Pedido por:** {message.author.mention}\n{app_emoji(self.bot, 'music_notes')} **⠂ Música{'s'[: tcount ^ 1]}:** `[{tcount}]`"
        embed.set_thumbnail(url=tracks.tracks[0].thumb)
        embed.set_author(
            name="⠂" + fix_characters(tracks.tracks[0].playlist_name, 35),
            url=message.content,
            icon_url=music_source_image(tracks.tracks[0].info["sourceName"]),
        )

        try:
            embed.description += (
                f"\n{app_emoji(self.bot, 'speaker')} **⠂ Canal de voz:** {message.author.voice.channel.mention}"
            )
        except AttributeError:
            pass

        try:
            self.bot.pool.enqueue_playlist_embed_cooldown.get_bucket(message).update_rate_limit()
        except Exception:
            pass

        components.extend(
            [
                disnake.ui.Button(
                    emoji=app_emoji(self.bot, "loved"), label="Favoritar", custom_id=PlayerControls.embed_add_fav
                ),
                disnake.ui.Button(
                    emoji=app_emoji(self.bot, "add_music"),
                    label="Adicionar na fila",
                    custom_id=PlayerControls.embed_enqueue_playlist,
                ),
            ]
        )

        if response:
            await response.edit(content=None, embed=embed, components=components)
        else:
            await message.reply(embed=embed, fail_if_not_exists=False, mention_author=False)

    else:
        player.set_command_log(
            text=f"{message.author.mention} adicionou a playlist {md_link(tracks.data['playlistInfo']['name'], tracks.tracks[0].playlist_url, limit=20)}"
            f" `({len(tracks.tracks)})`.",
            emoji="🎶",
        )
    if destroy_message:
        await self.delete_message(message)


async def _pr_single(self, req):
    """Adiciona a faixa pedida na fila e responde."""
    attachment = req.attachment
    components = req.components
    destroy_message = req.destroy_message
    embed = req.embed
    message = req.message
    player = req.player
    response = req.response
    tracks = req.tracks

    track = tracks[0]

    if track.info.get("sourceName") == "http":
        if track.title == "Unknown title":
            if attachment:
                track.info["title"] = attachment.filename
            else:
                track.info["title"] = track.uri.split("/")[-1]
            track.title = track.info["title"]

        track.info["uri"] = ""

    player.queue.append(track)

    if isinstance(message.channel, disnake.Thread) and not isinstance(message.channel.parent, disnake.ForumChannel):
        embed.description = (
            f"{app_emoji(self.bot, 'microphone')} **⠂ Uploader:** {md_code(track.author)}\n"
            f"{app_emoji(self.bot, 'user')} **⠂ Pedido por:** {message.author.mention}\n"
            f"{app_emoji(self.bot, 'history')} **⠂ Duração:** `{time_format(track.duration) if not track.is_stream else '🔴 Livestream'}`"
        )

        try:
            embed.description += (
                f"\n{app_emoji(self.bot, 'speaker')} **⠂ Canal de voz:** {message.author.voice.channel.mention}"
            )
        except AttributeError:
            pass

        try:
            self.bot.pool.enqueue_track_embed_cooldown.get_bucket(message).update_rate_limit()
        except Exception:
            pass

        components.extend(
            [
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
        )

        embed.set_thumbnail(url=track.thumb)
        embed.set_author(
            name=fix_characters(track.title, 35),
            url=track.uri or track.search_uri,
            icon_url=music_source_image(track.info["sourceName"]),
        )
        if response:
            await response.edit(content=None, embed=embed, components=components)
        else:
            await message.reply(embed=embed, fail_if_not_exists=False, mention_author=False, components=components)

    else:
        duration = time_format(tracks[0].duration) if not tracks[0].is_stream else "🔴 Livestream"
        player.set_command_log(
            text=f"{message.author.mention} adicionou {md_link(tracks[0].title, tracks[0].uri or tracks[0].search_uri, limit=20)} `({duration})`.",
            emoji="🎵",
        )
        if destroy_message:
            await self.delete_message(message)


async def _is_panel_bot(self, guild_id: int) -> bool:
    """Este bot é o bot do painel do servidor? Store indisponível = não (melhor ignorar a mensagem que duas respostas)."""
    try:
        return await self.bot.pool.servers.request.is_panel_bot(guild_id, self.bot)
    except GenericError:
        return False


async def _sr_validate_channel(self, req):
    """Valida o canal de pedidos, as permissões e a mensagem recebida."""
    data = req.data
    message = req.message
    player = req.player

    if player and isinstance(message.channel, disnake.Thread) and not player.static:
        try:
            if player.text_channel.id != message.id:
                return True
        except AttributeError:
            return True

        if not player.controller_mode:
            return True

        text_channel = message.channel

    else:
        static_player = data["player_controller"]

        channel_id = static_player["channel"]

        if not channel_id:
            return True

        if isinstance(message.channel, disnake.Thread):
            if isinstance(message.channel.parent, disnake.TextChannel):
                if str(message.channel.parent.id) != channel_id:
                    return True
            elif str(message.channel.id) != channel_id:
                return True
        elif str(message.channel.id) != channel_id:
            return True

        if not await _is_panel_bot(self, message.guild.id):
            return True  # o canal é único para o pool: só o bot do painel trata o que é digitado nele

        try:
            text_channel = self.bot.get_channel(int(channel_id)) or await self.bot.fetch_channel(int(channel_id))
        except disnake.NotFound:
            text_channel = None

        if not text_channel:
            await self.reset_controller_db(message.guild.id, data)
            return True

        if isinstance(text_channel, disnake.Thread):
            send_message_perm = text_channel.parent.permissions_for(message.guild.me).send_messages_in_threads
        else:
            send_message_perm = text_channel.permissions_for(message.guild.me).send_messages

        if not send_message_perm:
            return True

        if not self.bot.intents.message_content:
            if self.song_request_cooldown.get_bucket(message).update_rate_limit():
                return True

            await message.channel.send(
                message.author.mention,
                embed=disnake.Embed(
                    description="Infelizmente não posso conferir o conteúdo de sua mensagem...\n"
                    "Tente adicionar música usando **/play** ou clique em um dos botões abaixo:",
                    color=self.bot.get_color(message.guild.me),
                ),
                components=song_request_buttons(self.bot, await self.bot.pool.i18n.for_guild(message.guild)),
                delete_after=20,
            )
            return True

    req.text_channel = text_channel
    return False


async def _sr_empty_content(self, req):
    """Trata a mensagem sem texto: anexo de áudio ou aviso ao usuário."""
    message = req.message

    if not message.content:
        if message.type == disnake.MessageType.thread_starter_message:
            return True

        if message.is_system():
            await self.delete_message(message)
            return True

        try:
            attachment = message.attachments[0]
        except IndexError:
            await message.channel.send(
                f"{message.author.mention} você deve enviar um link/nome da música.", delete_after=8
            )
            return True

        else:
            if attachment.size > 18000000:
                await message.channel.send(
                    f"{message.author.mention} o arquivo que você enviou deve ter o tamanho inferior a 18mb.",
                    delete_after=8,
                )
                return True

            if attachment.content_type not in self.audio_formats:
                await message.channel.send(
                    f"{message.author.mention} o arquivo que você enviou deve ter o tamanho inferior a 18mb.",
                    delete_after=8,
                )
                return True

            message.content = attachment.url
    return False


async def _sr_prepare_query(self, req):
    """Reserva a vaga do pedido e prepara a busca (links e serviço)."""
    message = req.message
    msg = req.msg

    try:
        await self.song_request_concurrency.acquire(message)
    except Exception:
        await message.channel.send(
            f"{message.author.mention} você deve aguardar seu pedido de música anterior carregar...",
        )

        await self.delete_message(message)
        return True

    message.content = message.content.strip("<>")

    urls = URL_REG.findall(message.content)

    if not urls:
        source = None

    else:
        source = False
        message.content = urls[0]

        if "&list=" in message.content:
            view = ButtonInteraction(
                user=message.author,
                timeout=45,
                buttons=[
                    disnake.ui.Button(
                        label="Carregar apenas a música", emoji=app_emoji(self.bot, "music_note"), custom_id="music"
                    ),
                    disnake.ui.Button(
                        label="Carregar playlist", emoji=app_emoji(self.bot, "playlist_play"), custom_id="playlist"
                    ),
                ],
            )

            embed = disnake.Embed(
                description="**O link contém vídeo com playlist.**\n"
                f"Selecione uma opção em até <t:{int((disnake.utils.utcnow() + datetime.timedelta(seconds=45)).timestamp())}:R> para prosseguir.\n"
                f"-# Nota: Caso o link seja de uma playlist privada, apenas o vídeo do link atual será carregado.",
                color=self.bot.get_color(message.guild.me),
            )

            msg = await message.channel.send(message.author.mention, embed=embed, view=view)
            req.msg = msg  # o tratamento de erro de song_requests lê a mensagem já enviada

            await view.wait()

            try:
                await view.inter.response.defer()
            except Exception:
                pass

            if view.selected == "music":
                message.content = YOUTUBE_VIDEO_REG.search(message.content).group()

    req.msg = msg
    req.source = source
    return False
