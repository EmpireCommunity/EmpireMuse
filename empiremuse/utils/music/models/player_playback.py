# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import datetime
import traceback
from time import time
from typing import Union, TYPE_CHECKING

import disnake
from rapidfuzz import fuzz

from empiremuse.rules.i18n import fit
from empiremuse.utils.db import DBModel
from empiremuse.utils.music.converters import md_link
from empiremuse.utils.music.errors import (
    error_code_text,
    log_unexpected_error,
)
from empiremuse.utils.others import PlayerControls, string_to_file
from empiremuse.utils.server_playlists import load_select_options
from empiremuse.wavelink import TrackStart, TrackEnd

if TYPE_CHECKING:
    pass

from empiremuse.utils.music.models.tracks import (
    LavalinkPlaylist,
    LavalinkTrack,
    PartialTrack,
)
from empiremuse.utils.music.models.helpers import (
    TITLE_MATCH_THRESHOLD,
    exclude_tags,
    logger,
)
from empiremuse.utils.music.models.playback_steps import (
    _pn_pre_checks,
    _pn_next_track,
    _pn_sources,
    _pn_native,
    _pn_encoded,
    _pn_start,
)
from empiremuse.utils.music.models.step_state import ProcessNextState
from empiremuse.utils.music.app_emojis import emoji as app_emoji


class PlayerPlaybackMixin:
    def start_auto_skip(self):
        try:
            self.auto_skip_track_task.cancel()
        except Exception:
            pass
        self.auto_skip_track_task = self.bot.loop.create_task(self.auto_skip_track())

    async def process_next(
        self, start_position: Union[int, float] = 0, inter: disnake.MessageInteraction = None, force_np=False
    ):
        try:
            self.lyric_task.cancel()
        except Exception:
            pass

        if self.locked or self.is_closing:
            return

        self.has_lyrics = False

        self.lyric_embed = None

        if not self.node or not self.node.is_available:
            try:
                self._new_node_task.cancel()
            except Exception:
                pass
            self._new_node_task = self.bot.loop.create_task(self._wait_for_new_node())
            return

        try:
            self.track_load_task.cancel()
        except Exception:
            pass
        self.track_load_task = self.bot.loop.create_task(
            self._process_next(start_position=start_position, inter=inter, force_np=force_np)
        )

    async def _process_next(
        self, start_position: Union[int, float] = 0, inter: disnake.MessageInteraction = None, force_np=False
    ):
        req = ProcessNextState(start_position=start_position, inter=inter, force_np=force_np)
        try:
            if await _pn_pre_checks(self, req):
                return

            if await _pn_next_track(self, req):
                return

            self.locked = True

            req.encoded_track = None

            if not self.auto_pause:
                if await _pn_sources(self, req):
                    return

                if await _pn_native(self, req):
                    return

                if await _pn_encoded(self, req):
                    return

            await _pn_start(self, req)
        except Exception as e:
            req.error_code = log_unexpected_error(logger, e, "reprodução da música")

            embed = disnake.Embed(description=fit(self.tr("player.an_error_occurred_while_playing.description"), 4096))

            if self.current:
                embed.description += f" {md_link(self.current.title, self.current.uri)}"

            embed.description += error_code_text(req.error_code)

            if self.text_channel:
                try:
                    await self.text_channel.send(embed=embed, delete_after=15)
                except Exception:
                    logger.exception("Falha em _process_next")

            cog = self.bot.get_cog("ErrorHandler")
            if cog:
                embed.add_field(name=self.tr("player.server"), value=f"{self.guild.name} [{self.guild.id}]")
                try:
                    await cog.send_webhook(
                        embed=embed,
                        file=string_to_file(
                            "".join(traceback.format_exception(type(e), e, e.__traceback__)), "error_traceback_ytdl.txt"
                        ),
                    )
                except Exception:
                    logger.exception("Falha em _process_next")
            await asyncio.sleep(10)
            self.locked = False
            await self.process_next()

    async def play(self, track: Union[LavalinkTrack], *, replace: bool = True, start: int = 0, end: int = 0, **kwargs):
        if replace or not self.is_playing:
            self.last_update = 0
            self.last_position = 0
            self.position_timestamp = 0
            self.paused = False
        else:
            return

        self.current = track

        self.current_encoded = kwargs.pop("temp_id", None) or track.id

        if self.node.version == 3:
            payload = {
                "op": "play",
                "guildId": str(self.guild_id),
                "track": self.current_encoded,
                "noReplace": not replace,
                "startTime": start,
            }

            payload.update(kwargs)

            if end > 0:
                payload["endTime"] = str(end)

            await self.node._send(**payload, **kwargs)
        else:
            vol: int = kwargs.get("volume") or self.volume

            if vol != self.volume:
                self.volume = vol

            pause: bool

            if (p := kwargs.get("paused")) is not None:
                pause = p
            else:
                pause = self.paused

            payload = {
                "encodedTrack": self.current_encoded,
                "volume": vol,
                "position": int(start),
                "paused": pause,
                "filters": self.filters,
            }

            if end > 0:
                payload["endTime"] = str(end)

            if track.source_name == "youtube":
                try:
                    payload["userData"] = {"oauth-token": self.extra_info["guild-yt-oauth"][0]}
                except KeyError:
                    pass

            await self.node.update_player(self.guild_id, payload, replace)

    async def process_idle_message(self):
        controller_opts = [
            disnake.SelectOption(
                emoji=app_emoji(self.bot, "add_music"),
                value=PlayerControls.add_song,
                label=fit(self.tr("player.add_song.label"), 100),
                description=fit(self.tr("player.play_a_new_song_playlist.description"), 100),
            ),
        ]

        if (played := len(self.played)) or self.last_track:
            try:
                play_txt = self.played[-1].title
            except Exception:
                play_txt = self.last_track.title

            controller_opts.extend(
                [
                    disnake.SelectOption(
                        emoji=app_emoji(self.bot, "backward"),
                        value=PlayerControls.back,
                        label="Voltar",
                        description=f"Ouvir novamente: {play_txt[:31]}",
                    ),
                    disnake.SelectOption(
                        label=fit(self.tr("player.enable_autoplay.label"), 100),
                        emoji=app_emoji(self.bot, "refresh"),
                        value=PlayerControls.autoplay,
                        description=fit(self.tr("player.play_songs_related_to.description", v=str(play_txt[:19])), 100),
                    ),
                ]
            )

        if played > 1:
            controller_opts.append(
                disnake.SelectOption(
                    emoji=app_emoji(self.bot, "playlist_add"),
                    value=PlayerControls.readd,
                    label=fit(self.tr("player.play_again.label"), 100),
                    description=fit(self.tr("player.play_all_the_songs_again.description", played=str(played)), 100),
                )
            )

        controller_opts.append(
            disnake.SelectOption(
                emoji=app_emoji(self.bot, "stop"),
                value=PlayerControls.stop,
                label="Finalizar",
                description=fit(self.tr("player.finish_the_player_and_disconnect.description"), 100),
            ),
        )

        components = [
            disnake.ui.Select(
                placeholder=fit(self.tr("player.run_an_action.placeholder"), 150),
                options=controller_opts,
                custom_id="musicplayer_dropdown_idle",
                min_values=0,
                max_values=1,
                required=False,
            )
        ]

        opts = await load_select_options(self.bot, self.guild.id)

        if opts:
            components.append(
                disnake.ui.Select(
                    placeholder=fit(self.tr("player.play_a_song_playlist_from.placeholder"), 150),
                    options=opts,
                    custom_id="player_guild_pin",
                )
            )

        embed = disnake.Embed(
            description=fit(self.tr("player.there_are_no_songs_in.description"), 4096),
            color=self.bot.get_color(self.guild.me),
        )

        embed.set_thumbnail(url=self.bot.user.display_avatar.replace(size=512, static_format="png").url)

        if not self.keep_connected:
            embed.description += self.tr(
                "player.note_the_player_will_turn",
                timestamp=str(
                    int(
                        (
                            disnake.utils.utcnow() + datetime.timedelta(seconds=self.bot.config["IDLE_TIMEOUT"])
                        ).timestamp()
                    )
                ),
            )

        kwargs = {"embed": embed, "content": None, "components": components, "allowed_mentions": self.allowed_mentions}

        try:
            if isinstance(self.text_channel.parent, disnake.ForumChannel) and self.static:
                kwargs["content"] = self.tr("player.waiting_for_new_songs")
        except Exception:
            pass

        try:
            if self.has_thread or self.static or self.text_channel.last_message_id == self.message.id:
                try:
                    await self.message.edit(**kwargs)
                except Exception:
                    logger.exception("Falha em process_idle_message")
                    if self.text_channel and not self.static:
                        self.message = await self.text_channel.send(**kwargs)
                send_message = False
            else:
                send_message = True
        except AttributeError:
            send_message = True

        if send_message:
            try:
                await self.message.delete()
            except Exception:
                pass
            self.message = await self.text_channel.send(**kwargs)

    async def idling_mode(self):
        try:
            vc = self.guild.me.voice.channel
        except AttributeError:
            vc = self.last_channel

        try:
            await self.process_rpc(vc)
        except Exception:
            logger.exception("Falha em idling_mode")

        try:
            await self.process_idle_message()
        except Exception:
            logger.exception("Falha em idling_mode")

        try:
            await self.update_stage_topic()
        except Exception:
            pass

        if self.keep_connected:
            return

        await asyncio.sleep(self.bot.config["IDLE_TIMEOUT"])

        msg = self.tr("player.the_player_was_turned_off")

        try:
            if self.static or self.has_thread:
                self.command_log = msg
            else:
                if self.text_channel:
                    embed = disnake.Embed(description=msg, color=self.bot.get_color(self.guild.me))
                    self.bot.loop.create_task(
                        self.text_channel.send(embed=embed, delete_after=120, allowed_mentions=self.allowed_mentions)
                    )
        except Exception:
            logger.exception("Falha em idling_mode")

        await self.destroy()

    async def auto_skip_track(self):
        if not self.controller_mode or not self.current:
            return

        try:
            try:
                if self.current.is_stream:
                    return
            except AttributeError:
                pass

            retries = 5

            sleep_time = None

            while retries > 0:
                try:
                    sleep_time = (self.current.duration - self.position) / 1000
                    break
                except AttributeError:
                    await asyncio.sleep(5)
                    retries -= 1
                    continue

            if not sleep_time:
                if self.current.duration:
                    sleep_time = self.current.duration / 1000
                else:
                    sleep_time = 180

            await asyncio.sleep(sleep_time)
            self.current = None
            self.current_encoded = None
            self.last_update = 0
            self.bot.loop.create_task(
                self.node.on_event(
                    TrackEnd({"track": self.current, "player": self, "node": self.node, "reason": "FINISHED"})
                )
            )
            return

        except asyncio.CancelledError:
            return

        except Exception:
            logger.exception("Falha em auto_skip_track")
            return
