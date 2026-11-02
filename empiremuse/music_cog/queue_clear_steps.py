# -*- coding: utf-8 -*-
"""Etapas de `/queue clear` (filtros, intervalo, remoção das faixas e resposta)."""

import traceback
from random import shuffle
from typing import Union, Optional

import disnake
from disnake.ext import commands

from empiremuse.utils.music.guild_text import guild_text
from empiremuse.utils.music.checks import (
    check_voice,
    has_player,
    is_dj,
    check_pool_bots,
    check_queue_loading,
)
from empiremuse.utils.music.converters import time_format, string_to_seconds, md_link, md_code
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
    PlayerControls,
    queue_track_index,
    pool_command,
    CommandArgparse,
)

# O corpo da classe usa estes nomes, então eles precisam existir neste módulo.
from empiremuse.music_cog.shared import (
    DESC_PREFIX as desc_prefix,
    remove_mc,
    queue_manipulation_cd,
)


async def _qc_filters(self, req):
    """Valida os argumentos e monta a lista de filtros."""
    absent_members = req.absent_members
    amount = req.amount
    duplicates = req.duplicates
    inter = req.inter
    max_duration = req.max_duration
    min_duration = req.min_duration
    playlist = req.playlist
    song_author = req.song_author
    song_name = req.song_name
    user = req.user

    if min_duration and max_duration:
        raise GenericError(
            await guild_text(
                req.inter.bot,
                req.inter.guild,
                "legacy.queue.one_duration_option",
                below="duração_abaixo_de",
                above="duração_acima_de",
            )
        )

    try:
        bot = inter.music_bot
    except AttributeError:
        bot = inter.bot

    player: LavalinkPlayer = bot.music.players[inter.guild_id]

    if not player.queue:
        raise GenericError(await guild_text(bot, inter.guild, "legacy.queue.empty"))

    if amount is None:
        amount = 0

    filters = []
    final_filters = set()

    txt = []
    playlist_hyperlink = set()

    tracklist = []

    if song_name:
        song_name = song_name.replace("️", "")
        filters.append("song_name")
    if song_author:
        song_author = song_author.replace("️", "")
        filters.append("song_author")
    if user:
        filters.append("user")
    if playlist:
        playlist = playlist.replace("️", "")
        filters.append("playlist")
    if min_duration:
        filters.append("time_below")
        min_duration = string_to_seconds(min_duration) * 1000
    if max_duration:
        filters.append("time_above")
        max_duration = string_to_seconds(max_duration) * 1000
    if absent_members:
        filters.append("absent_members")
    if duplicates:
        filters.append("duplicates")

    req.amount = amount
    req.filters = filters
    req.final_filters = final_filters
    req.max_duration = max_duration
    req.min_duration = min_duration
    req.player = player
    req.playlist = playlist
    req.playlist_hyperlink = playlist_hyperlink
    req.song_author = song_author
    req.song_name = song_name
    req.tracklist = tracklist
    req.txt = txt


async def _qc_range(self, req):
    """Resolve o intervalo de posições da fila."""
    amount = req.amount
    player = req.player
    range_end = req.range_end
    range_start = req.range_start
    txt = req.txt

    if range_start > 0 and range_end > 0:
        if range_start >= range_end:
            raise GenericError(await guild_text(req.inter.bot, req.inter.guild, "legacy.queue.range_end_after_start"))

        song_list = list(player.queue)[range_start - 1 : -(range_end - 1)]
        txt.append(f"**Posição inicial da fila:** `{range_start}`\n**Posição final da fila:** `{range_end}`")

    elif range_start > 0:
        song_list = list(player.queue)[range_start - 1 :]
        txt.append(f"**Posição inicial da fila:** `{range_start}`")
    elif range_end > 0:
        song_list = list(player.queue)[: -(range_end - 1)]
        txt.append(f"**Posição final da fila:** `{range_end}`")
    else:
        song_list = list(player.queue)

    deleted_tracks = 0

    duplicated_titles = set()

    amount_counter = int(amount) if amount > 0 else 0

    req.amount_counter = amount_counter
    req.deleted_tracks = deleted_tracks
    req.duplicated_titles = duplicated_titles
    req.song_list = song_list


def _qc_remove(self, req):
    """Remove da fila as faixas que passam pelos filtros."""
    amount = req.amount
    amount_counter = req.amount_counter
    deleted_tracks = req.deleted_tracks
    duplicated_titles = req.duplicated_titles
    filters = req.filters
    final_filters = req.final_filters
    inter = req.inter
    max_duration = req.max_duration
    min_duration = req.min_duration
    player = req.player
    playlist = req.playlist
    playlist_hyperlink = req.playlist_hyperlink
    song_author = req.song_author
    song_list = req.song_list
    song_name = req.song_name
    tracklist = req.tracklist
    user = req.user

    for t in song_list:
        if amount and amount_counter < 1:
            break

        temp_filter = list(filters)

        if "duplicates" in temp_filter:
            if (title := f"{t.author} - {t.title}".lower()) in duplicated_titles:
                temp_filter.remove("duplicates")
                final_filters.add("duplicates")
            else:
                duplicated_titles.add(title)

        if "time_below" in temp_filter and t.duration >= min_duration:
            temp_filter.remove("time_below")
            final_filters.add("time_below")

        elif "time_above" in temp_filter and t.duration <= max_duration:
            temp_filter.remove("time_above")
            final_filters.add("time_above")

        if "song_name" in temp_filter:
            title = t.title.replace("️", "").lower().split()

            query_words = song_name.lower().split()

            word_count = 0

            for query_word in song_name.lower().split():
                for title_word in title:
                    if query_word in title_word:
                        title.remove(title_word)
                        word_count += 1
                        break

            if word_count == len(query_words):
                temp_filter.remove("song_name")
                final_filters.add("song_name")

        if "song_author" in temp_filter and song_author.lower() in t.author.replace("️", "").lower():
            temp_filter.remove("song_author")
            final_filters.add("song_author")

        if "user" in temp_filter and user.id == t.requester:
            temp_filter.remove("user")
            final_filters.add("user")

        elif "absent_members" in temp_filter and t.requester not in player.guild.me.voice.channel.voice_states:
            temp_filter.remove("absent_members")
            final_filters.add("absent_members")

        playlist_link = None

        if "playlist" in temp_filter:
            if playlist == t.playlist_name.replace("️", "") or (
                isinstance(inter, CustomContext) and playlist.lower() in t.playlist_name.replace("️", "").lower()
            ):
                playlist_link = md_link(t.playlist_name, t.playlist_url)
                temp_filter.remove("playlist")
                final_filters.add("playlist")

        if not temp_filter:
            tracklist.append(t)
            player.queue.remove(t)
            deleted_tracks += 1
            if playlist_link:
                playlist_hyperlink.add(playlist_link)

            if amount:
                amount_counter -= 1

    req.amount_counter = amount_counter
    req.deleted_tracks = deleted_tracks


async def _qc_report(self, req):
    """Monta o texto dos filtros usados e o resumo da remoção."""
    deleted_tracks = req.deleted_tracks
    duplicated_titles = req.duplicated_titles
    final_filters = req.final_filters
    inter = req.inter
    max_duration = req.max_duration
    min_duration = req.min_duration
    playlist_hyperlink = req.playlist_hyperlink
    song_author = req.song_author
    song_name = req.song_name
    tracklist = req.tracklist
    txt = req.txt
    user = req.user

    duplicated_titles.clear()

    if not deleted_tracks:
        await inter.send(await guild_text(req.inter.bot, inter.guild, "legacy.queue.none_found"), ephemeral=True)
        return True

    try:
        final_filters.remove("song_name")
        txt.append(f"**Inclui nome:** {md_code(song_name)}")
    except Exception:
        pass

    try:
        final_filters.remove("song_author")
        txt.append(f"**Inclui nome no uploader/artista:** {md_code(song_author)}")
    except Exception:
        pass

    try:
        final_filters.remove("user")
        txt.append(f"**Pedido pelo membro:** {user.mention}")
    except Exception:
        pass

    try:
        final_filters.remove("playlist")
        txt.append(f"**Playlist:** {' | '.join(playlist_hyperlink)}")
    except Exception:
        pass

    try:
        final_filters.remove("time_below")
        txt.append(f"**Com duração inicial/igual:** `{time_format(min_duration)}`")
    except Exception:
        pass

    try:
        final_filters.remove("time_above")
        txt.append(f"**Com duração máxima:** `{time_format(max_duration)}`")
    except Exception:
        pass

    try:
        final_filters.remove("duplicates")
        txt.append(f"**Músicas duplicadas**")
    except Exception:
        pass

    try:
        final_filters.remove("absent_members")
        txt.append("`Músicas pedidas por membros que saíram do canal.`")
    except Exception:
        pass

    msg_txt = (
        f"### ♻️ ⠂{inter.author.mention} removeu {deleted_tracks} música{'s'[: deleted_tracks ^ 1]} da fila:\n"
        + "\n".join(md_link(t.title, t.uri, limit=45) for t in tracklist[:7])
    )

    if (trackcount := (len(tracklist) - 7)) > 0:
        msg_txt += f"\n`e mais {trackcount} música{'s'[: trackcount ^ 1]}.`"

    msg_txt += f"\n### ✅ ⠂Filtro{(t := 's'[: len(txt) ^ 1])} usado{t}:\n" + "\n".join(txt)

    txt = [f"removeu {deleted_tracks} música{'s'[: deleted_tracks ^ 1]} da fila via clear.", msg_txt]

    req.txt = txt
    return False
