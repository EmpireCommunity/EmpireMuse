# -*- coding: utf-8 -*-
from os.path import basename

import disnake

from empiremuse.rules.i18n import fit
from empiremuse.utils.branding import asset_url
from empiremuse.utils.music.converters import get_button_style, music_source_image, md_bracketed, md_link, md_code
from empiremuse.utils.music.models import LavalinkPlayer
from empiremuse.utils.others import PlayerControls
from empiremuse.utils.music.app_emojis import emoji as app_emoji


class MicroController:
    __slots__ = ("name", "preview")

    def __init__(self):
        self.name = basename(__file__)[:-3]
        self.preview = asset_url("skin_normal_micro_controller_preview.png")

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
            description=f"-# {md_link(player.current.single_title, player.current.uri or player.current.search_uri, limit=32)} "
            f"[{md_code(player.current.author, limit=12)}] ",
        )

        if not player.current.autoplay:
            embed.description += f"<@{player.current.requester}>"
        else:
            try:
                embed.description += md_bracketed(
                    md_link("Recomendada", player.current.info["extra"]["related"]["uri"])
                )
            except Exception:
                embed.description += "`[Recomendada]`"

        embed.set_author(
            name=player.tr("player.now_playing"), icon_url=music_source_image(player.current.info["sourceName"])
        )

        if player.command_log:
            embed.description += player.tr(
                "player.last_interaction_4",
                command_log_emoji=str(player.command_log_emoji),
                command_log=str(player.command_log),
            )

        if player.current_hint:
            embed_hint = disnake.Embed(colour=embed_color)
            embed_hint.set_footer(text=player.tr("player.tip", current_hint=str(player.current_hint)))
            data["embeds"].append(embed_hint)

        data["embeds"].append(embed)

        data["components"] = [
            disnake.ui.Button(
                emoji=app_emoji(player.bot, "playpause"),
                label=fit("Retomar" if player.paused else player.tr("player.pause.label"), 80),
                custom_id=PlayerControls.pause_resume,
                style=get_button_style(player.paused),
            ),
            disnake.ui.Button(emoji=app_emoji(player.bot, "backward"), label="Voltar", custom_id=PlayerControls.back),
            disnake.ui.Button(
                emoji=app_emoji(player.bot, "stop"),
                label="Parar",
                custom_id=PlayerControls.stop,
                style=disnake.ButtonStyle.red,
            ),
            disnake.ui.Button(emoji=app_emoji(player.bot, "skip"), label="Pular", custom_id=PlayerControls.skip),
            disnake.ui.Button(
                emoji=app_emoji(player.bot, "music_queue"),
                label=fit(player.tr("player.queue.label"), 80),
                custom_id=PlayerControls.queue,
                disabled=not (player.queue or player.queue_autoplay),
            ),
            disnake.ui.Button(
                emoji=app_emoji(player.bot, "loved"),
                label=fit(player.tr("player.add_to_your_favorites.label"), 80),
                custom_id=PlayerControls.add_favorite,
            ),
        ]

        return data


def load():
    return MicroController()
