# -*- coding: utf-8 -*-
import asyncio
import datetime
import itertools
import logging
import os.path
import pprint
import re
import sys
import traceback
from copy import deepcopy
from io import BytesIO
from random import shuffle
from typing import Union, Optional
from urllib.parse import urlparse, parse_qs, quote

import aiohttp
import disnake
from async_timeout import timeout
from disnake.ext import commands
from yt_dlp import YoutubeDL

import empiremuse.wavelink as wavelink
from empiremuse import paths
from empiremuse.rules.errors import RuleError
from empiremuse.rules.favorites import save_track
from empiremuse.utils.branding import asset_url
from empiremuse.utils.client import BotCore
from empiremuse.utils.db import DBModel
from empiremuse.utils.music.audio_sources.deezer import deezer_regex
from empiremuse.utils.music.audio_sources.spotify import spotify_link_regex, spotify_regex_w_user
from empiremuse.utils.music.checks import (
    check_voice,
    has_player,
    has_source,
    is_requester,
    is_dj,
    can_send_message_check,
    check_requester_channel,
    can_send_message,
    can_connect,
    check_deafen,
    check_pool_bots,
    check_channel_limit,
    check_stage_topic,
    check_queue_loading,
    check_player_perm,
    check_yt_cooldown,
)
from empiremuse.utils.music.converters import (
    time_format,
    fix_characters,
    string_to_seconds,
    URL_REG,
    YOUTUBE_VIDEO_REG,
    google_search,
    percentage,
    music_source_image,
)
from empiremuse.utils.music.errors import (
    GenericError,
    MissingVoicePerms,
    NoVoice,
    PoolException,
    parse_error,
    EmptyFavIntegration,
    DiffVoiceChannel,
    NoPlayer,
    error_code_text,
    log_unexpected_error,
)
from empiremuse.utils.music.user_limits import quota_for, track_fav_error_text
from empiremuse.utils.music.skin_store import load_custom_skins
from empiremuse.utils.music.saved_queue import has_saved_queue, load_saved_queue, store_saved_queue
from empiremuse.utils.music.interactions import (
    VolumeInteraction,
    QueueInteraction,
    SelectInteraction,
    FavMenuView,
    ViewMode,
    SetStageTitle,
    SelectBotVoice,
    youtube_regex,
    ButtonInteraction,
)
from empiremuse.utils.music.models import (
    LavalinkPlayer,
    LavalinkTrack,
    LavalinkPlaylist,
    PartialTrack,
    PartialPlaylist,
    native_sources,
    CustomYTDL,
)
from empiremuse.utils.others import (
    check_cmd,
    send_idle_embed,
    CustomContext,
    PlayerControls,
    queue_track_index,
    pool_command,
    string_to_file,
    CommandArgparse,
    music_source_emoji_url,
    jukebox_buttons,
    select_bot_pool,
    ProgressBar,
    update_inter,
    get_source_emoji_cfg,
    music_source_emoji,
)

# Os mixins são reexecutados a cada carga da extensão (um bot por vez), como o corpo de ``Music``: assim
# cada bot mantém os próprios cooldowns/concorrência, e o reload enxerga edições nos mixins.
for _nome in [m for m in sys.modules if m.startswith("empiremuse.music_cog.")]:
    del sys.modules[_nome]

from empiremuse.music_cog.shared import (  # noqa: E402
    DESC_PREFIX,
    EMOJI,
    NAME,
    DISCORD_ATTACHMENT_PATH_PREFIXES,
    DISCORD_CDN_HOSTS,
    case_sensitive_args,
    is_discord_attachment_url,
    logger,
    play_cd,
    play_mc,
    playlist_opts,
    queue_manipulation_cd,
    remove_mc,
    sc_profile_regex,
    sc_recommended,
)
from empiremuse.music_cog.skip import SkipMixin  # noqa: E402
from empiremuse.music_cog.playback_controls import PlaybackControlsMixin  # noqa: E402
from empiremuse.music_cog.queue_edit import QueueEditMixin  # noqa: E402
from empiremuse.music_cog.player_session import PlayerSessionMixin  # noqa: E402
from empiremuse.music_cog.now_playing import NowPlayingMixin  # noqa: E402
from empiremuse.music_cog.player_modes import PlayerModesMixin  # noqa: E402
from empiremuse.music_cog.favorites import FavoritesMixin  # noqa: E402
from empiremuse.music_cog.play_entry import PlayEntryMixin  # noqa: E402
from empiremuse.music_cog.play_command import PlayCommandMixin  # noqa: E402
from empiremuse.music_cog.queue_group import QueueGroupMixin  # noqa: E402
from empiremuse.music_cog.autocompletes import AutocompletesMixin  # noqa: E402
from empiremuse.music_cog.nodes import NodesMixin  # noqa: E402
from empiremuse.music_cog.controller_dispatch import ControllerDispatchMixin  # noqa: E402
from empiremuse.music_cog.panel_interactions import PanelInteractionsMixin  # noqa: E402
from empiremuse.music_cog.player_controller import PlayerControllerMixin  # noqa: E402
from empiremuse.music_cog.jukebox_messages import JukeboxMessagesMixin  # noqa: E402
from empiremuse.music_cog.tracks import TracksMixin  # noqa: E402
from empiremuse.music_cog.player_lifecycle import PlayerLifecycleMixin  # noqa: E402
from empiremuse.music_cog.voice_status import VoiceStatusMixin  # noqa: E402


class Music(
    TracksMixin,
    PlayerLifecycleMixin,
    JukeboxMessagesMixin,
    PlayerControllerMixin,
    ControllerDispatchMixin,
    PanelInteractionsMixin,
    FavoritesMixin,
    NodesMixin,
    PlayerModesMixin,
    AutocompletesMixin,
    QueueGroupMixin,
    NowPlayingMixin,
    PlayerSessionMixin,
    QueueEditMixin,
    PlaybackControlsMixin,
    SkipMixin,
    PlayCommandMixin,
    PlayEntryMixin,
    VoiceStatusMixin,
    commands.Cog,
):
    emoji = EMOJI
    name = NAME
    desc_prefix = DESC_PREFIX
    remove_mc = remove_mc
    queue_manipulation_cd = queue_manipulation_cd
    case_sensitive_args = case_sensitive_args
    play_cd = play_cd
    play_mc = play_mc
    playlist_opts = playlist_opts

    audio_formats = ("audio/mpeg", "audio/ogg", "audio/mp4", "audio/aac")

    providers_info = {
        "youtube": "ytsearch",
        "soundcloud": "scsearch",
        "spotify": "spsearch",
        "tidal": "tdsearch",
        "bandcamp": "bcsearch",
        "applemusic": "amsearch",
        "deezer": "dzsearch",
        "jiosaavn": "jssearch",
    }

    def __init__(self, bot: BotCore):
        self.bot = bot

        self.modules = [
            "empiremuse.utils.music.models",
            "empiremuse.utils.music.audio_sources.spotify",
            "empiremuse.utils.music.audio_sources.deezer",
            "empiremuse.utils.music.filters",
            "empiremuse.utils.music.local_lavalink",
            "empiremuse.utils.music.skin_utils",
            "empiremuse.utils.music.errors",
            "empiremuse.utils.music.interactions",
        ]

        self.extra_hints = bot.config["EXTRA_HINTS"].split("||")

        self.jukebox_concurrency = commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

        self.player_interaction_concurrency = commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

        self.jukebox_cooldown = commands.CooldownMapping.from_cooldown(
            rate=1, per=300, type=commands.BucketType.member
        )

        self.music_settings_cooldown = commands.CooldownMapping.from_cooldown(
            rate=3, per=15, type=commands.BucketType.guild
        )

        if self.bot.config["AUTO_ERROR_REPORT_WEBHOOK"]:
            self.error_report_queue = asyncio.Queue()
            self.error_report_task = bot.loop.create_task(self.error_report_loop())
        else:
            self.error_report_queue = None

    async def cog_check(self, ctx: CustomContext) -> bool:
        return await check_requester_channel(ctx)

    def cog_unload(self):
        try:
            self.error_report_task.cancel()
        except Exception:
            pass


def setup(bot: BotCore):
    if not getattr(bot.pool, "ytdl", None):
        bot.pool.ytdl = CustomYTDL(
            {
                "format": "webm[abr>0]/bestaudio/best",
                "extract_flat": True,
                "quiet": True,
                "no_warnings": True,
                "lazy_playlist": True,
                "playlist_items": "1-700",
                "simulate": True,
                "download": False,
                "cachedir": False,
                "allowed_extractors": [
                    r".*youtube.*",
                    r".*soundcloud.*",
                ],
                "extractor_args": {
                    "youtube": {
                        #'player_client': [
                        #    'web',
                        #    'android',
                        #    'android_creator',
                        #    'web_creator',
                        # ],
                        "max_comments": [0],
                    },
                    "youtubetab": {"skip": ["webpage", "authcheck"]},
                },
            }
        )

    bot.add_cog(Music(bot))
