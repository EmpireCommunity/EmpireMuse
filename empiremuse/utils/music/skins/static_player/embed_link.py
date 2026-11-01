# -*- coding: utf-8 -*-
import datetime
import itertools
from os.path import basename

import disnake

from empiremuse.utils.branding import asset_url
from empiremuse.utils.music.converters import time_format, fix_characters, get_button_style, md_link, md_bracketed
from empiremuse.utils.music.models import LavalinkPlayer
from empiremuse.utils.others import PlayerControls
from empiremuse.utils.music.app_emojis import emoji as app_emoji


class EmbedLinkStaticSkin:
    __slots__ = ("name", "preview")

    def __init__(self):
        self.name = basename(__file__)[:-3] + "_static"
        self.preview = asset_url("skin_static_embed_link_preview.png")

    def setup_features(self, player: LavalinkPlayer):
        player.mini_queue_feature = False
        player.controller_mode = True
        player.auto_update = 0
        player.hint_rate = player.bot.config["HINT_RATE"]
        player.static = True

    def load(self, player: LavalinkPlayer) -> dict:
        txt = ""

        if player.current_hint:
            txt += player.tr(
                "player.tip_4",
                app_emoji=str(app_emoji(player.bot, "notification")),
                current_hint=str(player.current_hint),
            )

        if player.current.is_stream:
            duration_txt = player.tr("player.duration_livestream", app_emoji=str(app_emoji(player.bot, "record")))
        else:
            duration_txt = player.tr(
                "player.duration",
                app_emoji=str(app_emoji(player.bot, "history")),
                time_format=str(time_format(player.current.duration)),
            )

        title = md_link(player.current.title, player.current.uri, code=False)

        if player.paused:
            txt += player.tr(
                "player.paused_3",
                app_emoji=str(app_emoji(player.bot, "pause")),
                title=str(title),
                duration_txt=str(duration_txt),
            )

        else:
            txt += player.tr(
                "player.now_playing_3",
                app_emoji=str(app_emoji(player.bot, "play")),
                title=str(title),
                duration_txt=str(duration_txt),
            )
            if not player.current.is_stream and not player.paused:
                txt += f" {md_bracketed(f'<t:{int((disnake.utils.utcnow() + datetime.timedelta(milliseconds=player.current.duration - player.position)).timestamp())}:R>')}"

        vc_txt = ""

        if not player.current.autoplay:
            txt += player.tr(
                "player.requested_by_4",
                app_emoji=str(app_emoji(player.bot, "user")),
                requester=str(player.current.requester),
            )
        else:
            try:
                mode = f" {md_link(player.tr('player.recommended_song_name'), player.current.info['extra']['related']['uri'])}"
            except Exception:
                mode = player.tr("player.recommended_song_2")
            txt += player.tr("player.added_via_4", app_emoji=str(app_emoji(player.bot, "like")), mode=str(mode))

        try:
            vc_txt += player.tr(
                "player.voice_channel_2",
                app_emoji=str(app_emoji(player.bot, "speaker")),
                mention=str(player.guild.me.voice.channel.mention),
            )
        except AttributeError:
            pass

        if player.current.playlist_name:
            txt += f"> -# {app_emoji(player.bot, 'playlists')} **⠂Playlist:** {md_link(player.current.playlist_name or 'Visualizar', player.current.playlist_url)}\n"

        if player.current.track_loops:
            txt += player.tr(
                "player.repeats_left_3",
                app_emoji=str(app_emoji(player.bot, "repeat")),
                track_loops=str(player.current.track_loops),
            )

        elif player.loop:
            if player.loop == "current":
                txt += player.tr("player.repeat_current_song", app_emoji=str(app_emoji(player.bot, "repeat")))
            else:
                txt += player.tr("player.repeat_queue", app_emoji=str(app_emoji(player.bot, "repeat_all")))

        txt += vc_txt

        if player.command_log:
            txt += player.tr(
                "player.last_interaction_2",
                command_log_emoji=str(player.command_log_emoji),
                command_log=str(player.command_log),
            )

        if qsize := len(player.queue):
            qtext = "> -# **Músicas na lista"

            if qsize > 4:
                qtext += f" [{qsize}]:"

            qtext += "**\n" + "\n".join(
                f"> -# `{(n + 1)} [{time_format(t.duration) if not t.is_stream else '🔴 stream'}]` {md_link(t.title, t.uri, limit=30)}"
                for n, t in enumerate(itertools.islice(player.queue, 4))
            )

            txt = f"{qtext}\n{txt}"

        elif len(player.queue_autoplay):
            txt = (
                player.tr("player.next_recommended_songs_2")
                + "\n".join(
                    f"-# `{(n + 1)} [{time_format(t.duration) if not t.is_stream else '🔴 stream'}]` {md_link(t.title, t.uri, limit=30)}"
                    for n, t in enumerate(itertools.islice(player.queue_autoplay, 4))
                )
                + f"\n{txt}"
            )

        data = {
            "content": txt,
            "embeds": [],
            "components": [
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
            ],
        }

        if queue := player.queue or player.queue_autoplay:
            data["components"].append(
                disnake.ui.Select(
                    placeholder=player.tr("player.next_songs.placeholder"),
                    custom_id="musicplayer_queue_dropdown",
                    min_values=0,
                    max_values=1,
                    required=False,
                    options=[
                        disnake.SelectOption(
                            label=fix_characters(f"{n + 1}. {t.single_title}", 47),
                            description=fix_characters(
                                f"[{time_format(t.duration) if not t.is_stream else '🔴 Live'}]. {t.authors_string}", 47
                            ),
                            value=f"{n:02d}.{t.title[:96]}",
                        )
                        for n, t in enumerate(itertools.islice(queue, 25))
                    ],
                )
            )

        if player.current.ytid and player.node.lyric_support:
            data["components"][5].options.append(
                disnake.SelectOption(
                    label="Visualizar letras",
                    emoji=app_emoji(player.bot, "lyrics"),
                    value=PlayerControls.lyrics,
                    description=player.tr("player.get_the_lyrics_of_the.description"),
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

        return data


def load():
    return EmbedLinkStaticSkin()
