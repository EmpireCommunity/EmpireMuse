# -*- coding: utf-8 -*-
from os.path import basename

import disnake

from empiremuse.utils.branding import asset_url
from empiremuse.utils.music.app_emojis import emoji as app_emoji
from empiremuse.utils.music.converters import fix_characters, md_link
from empiremuse.utils.music.models import LavalinkPlayer


class MicroNC:
    __slots__ = ("name", "preview")

    def __init__(self):
        self.name = basename(__file__)[:-3]
        self.preview = asset_url("skin_normal_micro_nc_preview.png")

    def setup_features(self, player: LavalinkPlayer):
        player.mini_queue_feature = False
        player.controller_mode = False
        player.auto_update = 0
        player.hint_rate = 9
        player.static = False

    def load(self, player: LavalinkPlayer) -> dict:
        data = {"content": None, "embeds": []}

        embed = disnake.Embed(
            color=player.bot.get_color(player.guild.me),
            description=f"-# {app_emoji(player.bot, 'music_notes')} **⠂{md_link(player.current.title, player.current.uri or player.current.search_uri, code=False, limit=30)}** `[{fix_characters(player.current.author, 12)}]`",
        )

        data["embeds"].append(embed)

        if player.current_hint:
            data["embeds"].append(
                disnake.Embed(color=player.bot.get_color(player.guild.me)).set_footer(
                    text=player.tr("player.tip", current_hint=str(player.current_hint))
                )
            )

        return data


def load():
    return MicroNC()
