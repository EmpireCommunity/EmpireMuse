# -*- coding: utf-8 -*-
import itertools
from os.path import basename

import disnake

from empiremuse.utils.branding import asset_url
from empiremuse.utils.music.converters import (
    time_format,
    fix_characters,
    get_button_style,
    music_source_image,
    md_bracketed,
    md_link,
)
from empiremuse.utils.music.models import LavalinkPlayer
from empiremuse.utils.others import PlayerControls
from empiremuse.utils.music.app_emojis import emoji as app_emoji


class MiniSkin:
    __slots__ = ("name", "preview")

    def __init__(self):
        self.name = basename(__file__)[:-3]
        self.preview = asset_url("skin_normal_mini_preview.png")

    def setup_features(self, player: LavalinkPlayer):
        player.mini_queue_feature = True
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
            description=f"-# {md_link(player.current.single_title, player.current.uri or player.current.search_uri)}",
        )
        embed_queue = None
        queue_size = len(player.queue)

        if not player.paused:
            embed.set_author(
                name=player.tr("player.now_playing"),
                icon_url=music_source_image(player.current.info["sourceName"]),
            )

        else:
            embed.set_author(name=player.tr("player.paused"), icon_url=asset_url("player_pause_icon.png"))

        if player.current.track_loops:
            embed.description += f" `[🔂 {player.current.track_loops}]`"

        elif player.loop:
            if player.loop == "current":
                embed.description += player.tr("player.current_song_2")
            else:
                embed.description += player.tr("player.queue_2")

        if not player.current.autoplay:
            embed.description += f" {md_bracketed(f'<@{player.current.requester}>')}"
        else:
            try:
                embed.description += (
                    f" {md_bracketed(md_link('Recomendada', player.current.info['extra']['related']['uri']))}"
                )
            except Exception:
                embed.description += "` [Recomendada]`"

        duration = "🔴 Livestream" if player.current.is_stream else time_format(player.current.duration)

        embed.add_field(
            name=player.tr("player.duration_2", app_emoji=str(app_emoji(player.bot, "history"))),
            value=f"```ansi\n[34;1m{duration}[0m\n```",
        )
        embed.add_field(
            name=player.tr("player.uploader_artist", app_emoji=str(app_emoji(player.bot, "microphone"))),
            value=f"```ansi\n[34;1m{fix_characters(player.current.author, 18)}[0m\n```",
        )

        if player.command_log:
            embed.add_field(
                name=player.tr("player.last_interaction_5", command_log_emoji=str(player.command_log_emoji)),
                value=f"{player.command_log}",
                inline=False,
            )

        if queue_size:
            embed.description += f" `({queue_size})`"

            if player.mini_queue_enabled:
                embed_queue = disnake.Embed(
                    color=embed_color,
                    description="\n".join(
                        f"`{(n + 1):02}) [{time_format(t.duration) if not t.is_stream else '🔴 Livestream'}]` {md_link(t.title, t.uri, limit=38)}"
                        for n, t in (enumerate(itertools.islice(player.queue, 5)))
                    ),
                )
                embed_queue.set_image(url=asset_url("ui_rainbow_bar_2.gif"))

        embed.set_thumbnail(url=player.current.thumb)
        embed.set_image(url=asset_url("ui_rainbow_bar_2.gif"))

        if player.current_hint:
            embed.set_footer(text=player.tr("player.tip", current_hint=str(player.current_hint)))

        data["embeds"] = [embed_queue, embed] if embed_queue else [embed]

        data["components"] = [
            disnake.ui.Button(
                emoji=app_emoji(player.bot, "playpause"),
                custom_id=PlayerControls.pause_resume,
                style=get_button_style(player.paused),
            ),
            disnake.ui.Button(emoji=app_emoji(player.bot, "backward"), custom_id=PlayerControls.back),
            disnake.ui.Button(emoji=app_emoji(player.bot, "stop"), custom_id=PlayerControls.stop),
            disnake.ui.Button(emoji=app_emoji(player.bot, "skip"), custom_id=PlayerControls.skip),
            disnake.ui.Button(
                emoji=app_emoji(player.bot, "music_queue"),
                custom_id=PlayerControls.queue,
                disabled=not (player.queue or player.queue_autoplay),
            ),
            disnake.ui.Select(
                placeholder=player.tr("player.more_options.placeholder"),
                custom_id="musicplayer_dropdown_inter",
                min_values=0,
                max_values=1,
                required=False,
                options=[
                    disnake.SelectOption(
                        label=player.tr("player.add_song.label"),
                        emoji=app_emoji(player.bot, "add_music"),
                        value=PlayerControls.add_song,
                        description=player.tr("player.add_a_song_playlist_to.description"),
                    ),
                    disnake.SelectOption(
                        label=player.tr("player.add_to_your_favorites.label"),
                        emoji=app_emoji(player.bot, "loved"),
                        value=PlayerControls.add_favorite,
                        description=player.tr("player.add_the_current_song_to.description"),
                    ),
                    disnake.SelectOption(
                        label=player.tr("player.play_from_the_start.label"),
                        emoji=app_emoji(player.bot, "fast_backward"),
                        value=PlayerControls.seek_to_start,
                        description=player.tr("player.go_back_to_the_start.description"),
                    ),
                    disnake.SelectOption(
                        label=player.tr("player.volume.label", volume=str(player.volume)),
                        emoji=app_emoji(player.bot, "volume"),
                        value=PlayerControls.volume,
                        description=player.tr("player.adjust_the_volume.description"),
                    ),
                    disnake.SelectOption(
                        label="Misturar",
                        emoji=app_emoji(player.bot, "shuffle"),
                        value=PlayerControls.shuffle,
                        description=player.tr("player.shuffle_the_queue_songs.description"),
                    ),
                    disnake.SelectOption(
                        label="Readicionar",
                        emoji=app_emoji(player.bot, "playlist_add"),
                        value=PlayerControls.readd,
                        description=player.tr("player.add_the_played_songs_back.description"),
                    ),
                    disnake.SelectOption(
                        label=player.tr("player.repeat.label"),
                        emoji=app_emoji(player.bot, "repeat_all"),
                        value=PlayerControls.loop_mode,
                        description=player.tr("player.enable_disable_repeating_the_song.description"),
                    ),
                    disnake.SelectOption(
                        label=(
                            player.tr("player.disable_the_nightcore_effect")
                            if player.nightcore
                            else player.tr("player.enable_the_nightcore_effect")
                        ),
                        emoji=app_emoji(player.bot, "sound_spectrum"),
                        value=PlayerControls.nightcore,
                        description=player.tr("player.effect_that_raises_the_speed.description"),
                    ),
                    disnake.SelectOption(
                        label=(
                            player.tr("player.disable_autoplay")
                            if player.autoplay
                            else player.tr("player.enable_autoplay")
                        ),
                        emoji=app_emoji(player.bot, "refresh"),
                        value=PlayerControls.autoplay,
                        description=player.tr("player.automatic_song_adding_system_when.description"),
                    ),
                    disnake.SelectOption(
                        label="Last.fm scrobble",
                        emoji=app_emoji(player.bot, "lastfm"),
                        value=PlayerControls.lastfm_scrobble,
                        description=player.tr("player.enable_disable_scrobbling_logging_of.description"),
                    ),
                    disnake.SelectOption(
                        label=(
                            player.tr("player.disable_restricted_mode")
                            if player.restrict_mode
                            else player.tr("player.enable_restricted_mode")
                        ),
                        emoji=app_emoji(player.bot, "lock"),
                        value=PlayerControls.restrict_mode,
                        description="Apenas DJ's/Staff's podem usar comandos restritos.",
                    ),
                ],
            ),
        ]

        if player.current.ytid and player.node.lyric_support:
            data["components"][5].options.append(
                disnake.SelectOption(
                    label="Visualizar letras",
                    emoji=app_emoji(player.bot, "lyrics"),
                    value=PlayerControls.lyrics,
                    description=player.tr("player.get_the_lyrics_of_the.description"),
                )
            )

        if player.mini_queue_feature:
            data["components"][5].options.append(
                disnake.SelectOption(
                    label=player.tr("player.player_mini_queue.label"),
                    emoji=app_emoji(player.bot, "music_queue"),
                    value=PlayerControls.miniqueue,
                    description=player.tr("player.enable_disable_the_player_s.description"),
                )
            )

        if isinstance(player.last_channel, disnake.VoiceChannel):
            data["components"][5].options.append(
                disnake.SelectOption(
                    label=player.tr("player.automatic_status.label"),
                    emoji=app_emoji(player.bot, "speaker"),
                    value=PlayerControls.set_voice_status,
                    description=player.tr("player.set_up_the_voice_channel.description"),
                )
            )

        if not player.has_thread:
            data["components"][5].options.append(
                disnake.SelectOption(
                    label="Song-Request Thread",
                    emoji=app_emoji(player.bot, "comment"),
                    value=PlayerControls.song_request_thread,
                    description=player.tr("player.create_a_temporary_thread_to.description"),
                )
            )

        return data


def load():
    return MiniSkin()
