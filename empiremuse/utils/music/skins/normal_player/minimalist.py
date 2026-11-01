# -*- coding: utf-8 -*-
from os.path import basename

from empiremuse.utils.branding import asset_url
from empiremuse.utils.music.app_emojis import emoji as app_emoji
from empiremuse.utils.music.converters import fix_characters, time_format, md_link
from empiremuse.utils.music.models import LavalinkPlayer


class Minimalist:
    __slots__ = ("name", "preview")

    def __init__(self):
        self.name = basename(__file__)[:-3]
        self.preview = asset_url("skin_normal_minimalist_preview.png")

    def setup_features(self, player: LavalinkPlayer):
        player.mini_queue_feature = False
        player.controller_mode = False
        player.auto_update = 0
        player.hint_rate = 9
        player.static = False

    def load(self, player: LavalinkPlayer) -> dict:
        duration = "🔴 Livestream" if player.current.is_stream else time_format(player.current.duration)

        data = {
            "embeds": [],
            "content": player.tr(
                "player.playing",
                app_emoji=str(app_emoji(player.bot, "play")),
                md_link=str(md_link(player.current.title, player.current.uri or player.current.search_uri, limit=42)),
                fix_characters=str(fix_characters(player.current.author, 20)),
                duration=str(duration),
            ),
        }

        if player.current_hint:
            data["content"] += player.tr(
                "player.tip_3",
                app_emoji=str(app_emoji(player.bot, "notification")),
                current_hint=str(player.current_hint),
            )

        return data


def load():
    return Minimalist()
