# -*- coding: utf-8 -*-
import itertools
from os.path import basename

import disnake

from empiremuse.utils.branding import asset_url
from empiremuse.utils.music.converters import time_format, get_button_style, music_source_image, md_link, md_code
from empiremuse.utils.music.models import LavalinkPlayer
from empiremuse.utils.others import PlayerControls
from empiremuse.utils.music.app_emojis import emoji as app_emoji


class ClassicSkin:
    __slots__ = ("name", "preview")

    def __init__(self):
        self.name = basename(__file__)[:-3]
        self.preview = asset_url("skin_normal_classic_preview.png")

    def setup_features(self, player: LavalinkPlayer):
        player.mini_queue_feature = True
        player.controller_mode = True
        player.auto_update = 0
        player.hint_rate = player.bot.config["HINT_RATE"]
        player.static = False

    def load(self, player: LavalinkPlayer) -> dict:
        data = {"content": None, "embeds": []}

        color = player.bot.get_color(player.guild.me)

        embed = disnake.Embed(color=color, description="")

        queue_txt = ""

        bar = asset_url("ui_rainbow_bar_3.gif")

        embed_top = disnake.Embed(
            color=color,
            description=f"### {md_link(player.current.title, player.current.uri or player.current.search_uri, code=False)}",
        )
        embed.set_image(url=bar)

        embed_top.set_image(url=bar)

        embed_top.set_thumbnail(url=player.current.thumb)

        if not player.paused:
            (embed_top or embed).set_author(
                name=player.tr("player.now_playing"), icon_url=music_source_image(player.current.info["sourceName"])
            )
        else:
            (embed_top or embed).set_author(
                name=player.tr("player.paused"), icon_url=asset_url("player_pause_icon.png")
            )

        if player.current.is_stream:
            duration = f"{app_emoji(player.bot, 'record')} **⠂ `Livestream`"
        else:
            duration = f"{app_emoji(player.bot, 'history')} **⠂** `{time_format(player.current.duration)}`"

        txt = f"{duration}\n{app_emoji(player.bot, 'microphone')} **⠂** {md_code(player.current.author)}\n"

        if not player.current.autoplay:
            txt += f"{app_emoji(player.bot, 'user')} **⠂** <@{player.current.requester}>\n"
        else:
            try:
                mode = f" {md_link('Recomendada', player.current.info['extra']['related']['uri'])}"
            except Exception:
                mode = "`Recomendada`"
            txt += f"> {app_emoji(player.bot, 'like')} **⠂** {mode}\n"

        if player.current.playlist_name:
            txt += f"{app_emoji(player.bot, 'playlists')} **⠂** {md_link(player.current.playlist_name, player.current.playlist_url, limit=19)}\n"

        if qsize := len(player.queue):
            if not player.mini_queue_enabled:
                txt += f"{app_emoji(player.bot, 'music_notes')} **⠂** {player.tr('player.queue.count', count=qsize)}\n"
            else:
                queue_txt += "```ansi\n[0;33mPróximas Músicas:[0m```" + "\n".join(
                    f"`{(n + 1):02}) [{time_format(t.duration) if t.duration else '🔴 Livestream'}]` "
                    f"{md_link(t.title, t.uri, limit=29)}"
                    for n, t in enumerate(itertools.islice(player.queue, 3))
                )

                if qsize > 3:
                    queue_txt += "\n" + player.tr("player.queue.and_more", count=qsize - 3)

        elif len(player.queue_autoplay):
            queue_txt += "```ansi\n[0;33mPróximas Músicas:[0m```" + "\n".join(
                f"{app_emoji(player.bot, 'like')} `{(n + 1):02}) [{time_format(t.duration) if t.duration else '🔴 Livestream'}]` "
                f"{md_link(t.title, t.uri, limit=29)}"
                for n, t in enumerate(itertools.islice(player.queue_autoplay, 3))
            )

        if player.command_log:
            txt += player.tr(
                "player.last_interaction",
                command_log_emoji=str(player.command_log_emoji),
                command_log=str(player.command_log),
            )

        embed.description += txt + queue_txt

        if player.current_hint:
            embed.set_footer(text=player.tr("player.tip", current_hint=str(player.current_hint)))
        else:
            embed.set_footer(text=str(player), icon_url=asset_url("player_loading_neon_circle.gif"))

        data["embeds"] = [embed_top, embed] if embed_top else [embed]

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
    return ClassicSkin()
