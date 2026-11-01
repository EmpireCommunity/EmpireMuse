# -*- coding: utf-8 -*-
from os.path import basename

import disnake

from empiremuse.utils.branding import asset_url
from empiremuse.utils.music.converters import get_button_style, music_source_image, md_bracketed, md_link, md_code
from empiremuse.utils.music.models import LavalinkPlayer
from empiremuse.utils.others import PlayerControls
from empiremuse.utils.music.app_emojis import emoji as app_emoji


class MiniPlayer:
    __slots__ = ("name", "preview")

    def __init__(self):
        self.name = basename(__file__)[:-3]
        self.preview = asset_url("skin_normal_miniplayer_preview.png")

    def setup_features(self, player: LavalinkPlayer):
        player.mini_queue_feature = False
        player.controller_mode = True
        player.auto_update = 0
        player.hint_rate = player.bot.config["HINT_RATE"]
        player.static = False

    def load(self, player: LavalinkPlayer) -> dict:
        data = {
            "content": None,
            "embeds": [],
        }

        embed_color = player.bot.get_color(player.guild.me)

        embed = disnake.Embed(
            color=embed_color,
            description=f"-# {md_link(player.current.single_title, player.current.uri or player.current.search_uri, code=False, limit=48)}\n"
            f"-# **Uploader:** {md_code(player.current.author, limit=17)}\n",
        )

        if player.current.thumb:
            embed.set_thumbnail(url=player.current.thumb)

        if not player.current.autoplay:
            embed.description += f"-# **Pedido por:** <@{player.current.requester}>\n"
        else:
            try:
                embed.description += f"-# **Adicionado via:** {md_bracketed(md_link('Recomendação', player.current.info['extra']['related']['uri']))}\n"
            except Exception:
                embed.description += "-# **Adicionado via:** `[Recomendação]`\n"

        embed.set_author(name="Tocando Agora:", icon_url=music_source_image(player.current.info["sourceName"]))

        if player.command_log:
            embed.description += f"-# {player.command_log_emoji} ⠂**Última Interação:** {player.command_log}"

        if player.current_hint:
            embed_hint = disnake.Embed(colour=embed_color)
            embed_hint.set_footer(text=f"💡 Dica: {player.current_hint}")
            data["embeds"].append(embed_hint)

        data["embeds"].append(embed)

        data["components"] = [
            disnake.ui.Button(
                emoji=app_emoji(player.bot, "playpause"),
                custom_id=PlayerControls.pause_resume,
                style=get_button_style(player.paused),
            ),
            disnake.ui.Button(emoji=app_emoji(player.bot, "backward"), custom_id=PlayerControls.back),
            disnake.ui.Button(
                emoji=app_emoji(player.bot, "stop"), custom_id=PlayerControls.stop, style=disnake.ButtonStyle.red
            ),
            disnake.ui.Button(emoji=app_emoji(player.bot, "skip"), custom_id=PlayerControls.skip),
            disnake.ui.Button(emoji=app_emoji(player.bot, "loved"), custom_id=PlayerControls.add_favorite),
        ]

        return data


def load():
    return MiniPlayer()
