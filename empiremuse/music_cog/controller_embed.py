# -*- coding: utf-8 -*-
"""Botões da mensagem de embed do player (tocar, adicionar à fila e favoritar o link)."""

import asyncio
from typing import Union, Optional

import disnake
from disnake.ext import commands

import empiremuse.wavelink as wavelink
from empiremuse.rules.errors import RuleError
from empiremuse.rules.favorites import save_track
from empiremuse.utils.client import BotCore
from empiremuse.utils.db import DBModel
from empiremuse.utils.music.checks import (
    is_dj,
    can_connect,
    check_player_perm,
)
from empiremuse.utils.music.converters import URL_REG, md_link
from empiremuse.utils.music.errors import (
    GenericError,
)
from empiremuse.utils.music.user_limits import quota_for, track_fav_error_text
from empiremuse.utils.music.interactions import (
    SelectInteraction,
)
from empiremuse.utils.music.models import (
    LavalinkPlayer,
)
from empiremuse.utils.others import (
    send_idle_embed,
    PlayerControls,
    music_source_emoji_url,
)


async def _pc_embed_resolve(self, req):
    """Valida o link do embed e resolve o bot, o player e o autor."""
    interaction = req.interaction

    try:
        if not (url := interaction.message.embeds[0].author.url):
            if not (matches := URL_REG.findall(interaction.message.embeds[0].description)):
                return True
            url = matches[0].split(">")[0]
    except Exception:
        return True

    try:
        await self.player_interaction_concurrency.acquire(interaction)
    except Exception:
        raise GenericError((await self.bot.pool.i18n.ack_for_guild(interaction))("player.a_song_is_being_processed"))

    bot: Optional[BotCore] = None
    player: Optional[LavalinkPlayer] = None
    channel: Union[disnake.TextChannel, disnake.VoiceChannel, disnake.StageChannel, disnake.Thread] = None
    author: Optional[disnake.Member] = None

    for b in sorted(self.bot.pool.get_guild_bots(interaction.guild_id), key=lambda b: b.identifier, reverse=True):
        try:
            p = b.music.players[interaction.guild_id]
        except KeyError:
            if c := b.get_channel(interaction.channel_id):
                bot = b
                channel = c
                author = c.guild.get_member(interaction.author.id)
            continue

        if p.guild.me.voice and interaction.author.id in p.guild.me.voice.channel.voice_states:
            if p.locked:
                raise GenericError(
                    (await self.bot.pool.i18n.ack_for_guild(interaction))("player.this_action_cannot_be_run")
                )

            player = p
            bot = b
            channel = player.text_channel
            author = p.guild.get_member(interaction.author.id)
            break

    if not channel:
        raise GenericError((await self.bot.pool.i18n.ack_for_guild(interaction))("player.there_are_no_bots_available"))

    if not author.voice:
        raise GenericError((await self.bot.pool.i18n.ack_for_guild(interaction))("player.you_must_join_a_voice"))

    try:
        node = player.node
    except Exception:
        node: Optional[wavelink.Node] = None

    try:
        interaction.author = author
    except AttributeError:
        pass

    await check_player_perm(inter=interaction, bot=bot, channel=interaction.channel)

    vc_id: int = author.voice.channel.id

    can_connect(channel=author.voice.channel, guild=channel.guild)

    await interaction.response.defer()

    req.author = author
    req.bot = bot
    req.channel = channel
    req.node = node
    req.player = player
    req.url = url
    req.vc_id = vc_id
    return False


async def _pc_embed_playlist(self, req):
    """Adiciona a playlist do embed na fila do player."""
    author = req.author
    bot = req.bot
    channel = req.channel
    interaction = req.interaction
    node = req.node
    player = req.player
    url = req.url
    vc_id = req.vc_id

    if retry_after := self.bot.pool.enqueue_playlist_embed_cooldown.get_bucket(interaction).update_rate_limit():
        tr = player.tr if player else await self.bot.pool.i18n.ack_for_guild(interaction)
        raise GenericError(tr("player.cooldown.playlist", count=int(retry_after)))

    if not player:
        player = await self.create_player(inter=interaction, bot=bot, guild=channel.guild, channel=channel, node=node)

    await self.check_player_queue(interaction.author, bot, interaction.guild_id)
    result, node = await self.get_tracks(url, interaction, author, source=False, node=player.node, bot=bot)
    result = await self.check_player_queue(interaction.author, bot, interaction.guild_id, tracks=result)
    player.queue.extend(result.tracks)
    await interaction.send(
        player.tr(
            "player.the_playlist_was_added_successfully",
            mention=str(interaction.author.mention),
            md_link=str(md_link(result.name, url)),
            controller_link=str(player.controller_link),
        ),
        ephemeral=True,
    )

    if not player.is_connected:
        await player.connect(vc_id)

    try:
        vc = interaction.author.voice.channel
    except AttributeError:
        vc = player.bot.get_channel(vc_id)

    if isinstance(vc, disnake.StageChannel):
        retries = 5

        while retries > 0:
            await asyncio.sleep(1)

            if not player.guild.me.voice:
                retries -= 1
                continue

            break

        if player.guild.me not in vc.speakers:
            stage_perms = vc.permissions_for(player.guild.me)
            if stage_perms.manage_permissions:
                await asyncio.sleep(1.5)
                await player.guild.me.edit(suppress=False)

    if not player.current:
        await player.process_next()

    req.node = node
    req.player = player
    req.vc = vc


async def _pc_embed_find_track(self, req):
    """Procura a faixa do embed na fila, nas tocadas e nas com falha."""
    bot = req.bot
    control = req.control
    interaction = req.interaction
    player = req.player
    url = req.url

    track = []
    seek_status = False

    if player:
        if (
            control == PlayerControls.embed_forceplay
            and player.current
            and (player.current.uri.startswith(url) or url.startswith(player.current.uri))
        ):
            await self.check_stage_title(inter=interaction, bot=bot, player=player)
            await player.seek(0)
            player.set_command_log(player.tr("player.went_back_to_the_start"), emoji="⏪")
            await asyncio.sleep(3)
            await player.update_stage_topic()
            await asyncio.sleep(7)
            seek_status = True

        else:
            for t in list(player.queue):
                if t.uri.startswith(url) or url.startswith(t.uri):
                    track = [t]
                    player.queue.remove(t)
                    break

            if not track:
                for t in list(player.played):
                    if t.uri.startswith(url) or url.startswith(t.uri):
                        track = [t]
                        player.played.remove(t)
                        break

                if not track:
                    for t in list(player.failed_tracks):
                        if t.uri.startswith(url) or url.startswith(t.uri):
                            track = [t]
                            player.failed_tracks.remove(t)
                            break

    req.seek_status = seek_status
    req.track = track


async def _pc_embed_track(self, req):
    """Adiciona a faixa do embed na fila ou toca na hora."""
    author = req.author
    bot = req.bot
    channel = req.channel
    control = req.control
    interaction = req.interaction
    node = req.node
    player = req.player
    seek_status = req.seek_status
    track = req.track
    url = req.url
    vc_id = req.vc_id

    if not seek_status:
        if not track:
            if retry_after := self.bot.pool.enqueue_track_embed_cooldown.get_bucket(interaction).update_rate_limit():
                tr = player.tr if player else await self.bot.pool.i18n.ack_for_guild(interaction)
                raise GenericError(tr("player.cooldown.song", count=int(retry_after)))

            if control == PlayerControls.embed_enqueue_track:
                await self.check_player_queue(interaction.author, bot, interaction.guild_id)

            result, node = await self.get_tracks(url, interaction, author, source=False, node=node, bot=bot)

            track = result

        if control == PlayerControls.embed_enqueue_track:
            if not player:
                player = await self.create_player(
                    inter=interaction, bot=bot, guild=channel.guild, channel=channel, node=node
                )
            await self.check_player_queue(interaction.author, bot, interaction.guild_id)
            player.update = True
            if isinstance(track, list):
                t = track[0]
                player.queue.append(t)
                await interaction.send(
                    player.tr(
                        "player.the_song_was_added_to",
                        mention=str(author.mention),
                        md_link=str(md_link(t.title, t.uri)),
                        controller_link=str(player.controller_link),
                    ),
                    ephemeral=True,
                )
            else:
                player.queue.extend(track.tracks)
                await interaction.send(
                    player.tr(
                        "player.the_playlist_was_added_to",
                        mention=str(author.mention),
                        md_link=str(md_link(track.name, track.url)),
                        controller_link=str(player.controller_link),
                    ),
                    ephemeral=True,
                )
            if not player.is_connected:
                await player.connect(vc_id)
            if not player.current:
                await player.process_next()

        else:
            if not player:
                player = await self.create_player(
                    inter=interaction, bot=bot, guild=channel.guild, channel=channel, node=node
                )
            else:
                await self.check_stage_title(inter=interaction, bot=bot, player=player)

            if isinstance(track, list):
                player.queue.insert(0, track[0])
            else:
                index = len(player.queue)
                player.queue.extend(track.tracks)
                if index:
                    player.queue.rotate(index * -1)
            if not player.is_connected:
                await player.connect(vc_id)
            await self.process_music(inter=interaction, player=player, force_play="yes")

    req.node = node
    req.player = player
    req.track = track


async def _pc_fav_embed_save(self, req):
    """Salva o link do embed nos favoritos do usuário."""
    interaction = req.interaction

    try:
        embed = interaction.message.embeds[0]
    except IndexError:
        await interaction.send("A embed da mensagem foi removida...", ephemeral=True)
        return True

    if retry_after := self.bot.pool.add_fav_embed_cooldown.get_bucket(interaction).update_rate_limit():
        await interaction.send(
            (await self.bot.pool.i18n.ack_for_guild(interaction))("player.cooldown.favorite", count=int(retry_after)),
            ephemeral=True,
        )
        return True

    await interaction.response.defer()

    user_data = await self.bot.get_global_data(interaction.author.id, db_name=DBModel.users)

    limit = await quota_for(self.bot, interaction.author, "MAX_USER_FAVS")

    saved = save_track(user_data["fav_links"], embed.author.name[1:], embed.author.url, limit=limit)

    if isinstance(saved, RuleError):
        await interaction.edit_original_message(track_fav_error_text(saved))
        return True

    fav_name = saved.name
    user_data["fav_links"] = saved.entries

    req.embed = embed
    req.fav_name = fav_name
    req.user_data = user_data
    return False


async def _pc_fav_embed_reply(self, req):
    """Responde ao usuário depois de favoritar o link do embed."""
    embed = req.embed
    fav_name = req.fav_name
    interaction = req.interaction
    user_data = req.user_data

    await self.bot.update_global_data(interaction.author.id, user_data, db_name=DBModel.users)

    global_data = await self.bot.get_global_data(interaction.guild_id, db_name=DBModel.guilds)

    try:
        cmd = (
            f"</play:"
            + str(self.bot.get_global_command_named("play", cmd_type=disnake.ApplicationCommandType.chat_input).id)
            + ">"
        )
    except AttributeError:
        cmd = "/play"

    try:
        interaction.message.embeds[0].fields[0].value = f"{interaction.author.mention} " + interaction.message.embeds[
            0
        ].fields[0].value.replace(interaction.author.mention, "")
    except IndexError:
        interaction.message.embeds[0].add_field(
            name="**Membros que favoritaram o link:**", value=interaction.author.mention
        )

    await interaction.send(
        embed=disnake.Embed(
            description=(await self.bot.pool.i18n.for_guild(req.interaction.guild))(
                "player.was_added_to_your_favorites",
                md_link=str(md_link(fav_name, embed.author.url)),
                cmd=str(cmd),
                v=str(global_data["prefix"] or self.bot.default_prefix),
                name=str(self.play_legacy.name),
            )
        ).set_footer(
            text=f"Caso queira ver todos os seus favoritos use o comando {global_data['prefix'] or self.bot.default_prefix}{self.fav_manager_legacy.name}"
        ),
        ephemeral=True,
    )

    if not interaction.message.flags.ephemeral:
        if not interaction.guild:
            await (await interaction.original_response()).edit(embed=interaction.message.embeds[0])
        else:
            await interaction.message.edit(embed=interaction.message.embeds[0])

    req.cmd = cmd
