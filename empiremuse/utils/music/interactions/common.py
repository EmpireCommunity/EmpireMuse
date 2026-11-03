# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import disnake

from empiremuse.utils.others import send_idle_embed

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore

# O logger leva o nome do pacote (e não o de common.py), como quando tudo morava em interactions.py.
logger = logging.getLogger(__package__)

youtube_regex = re.compile(r"https?://www\.youtube\.com/(?:channel/|@)[^/]+")
soundcloud_regex = re.compile(r"^(?:https?:\/\/)?(?:www\.)?soundcloud\.com\/([a-zA-Z0-9_-]+)")


async def process_idle_embed(bot: BotCore, guild: disnake.Guild, guild_data: dict):
    try:
        bot.music.players[guild.id]
        return
    except KeyError:
        pass

    try:
        channel = bot.get_channel(int(guild_data["player_controller"]["channel"]))
    except Exception:
        return

    try:
        message = await channel.fetch_message(int(guild_data["player_controller"]["message_id"]))
    except Exception:
        message = None

    await send_idle_embed(message or channel, bot=bot, guild_data=guild_data)


class ViewMode:
    """Modos do ``/fav_manager``: favoritos e integrações do membro. As playlists do servidor saíram daqui
    (``/jukebox playlists`` e ``/radio playlists``; o valor "1" fica reservado)."""

    fav_manager = "0"
    integrations_manager = "2"


IMPORT_ECHO_LIMIT = 100
