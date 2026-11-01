# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import asyncio
from typing import Union, List

import disnake

from empiremuse.utils.db import DBModel

from empiremuse.utils.music.models.tracks import (
    LavalinkTrack,
    PartialTrack,
)
from empiremuse.utils.music.models.state_steps import (
    _cu_cancel_tasks,
    _cu_close_player_message,
    _cu_static_session,
    _cu_voice_channel,
)

logger = logging.getLogger("empiremuse.player")


class PlayerStateMixin:
    async def cleanup(self, inter: disnake.MessageInteraction = None):
        self.played.clear()

        _cu_cancel_tasks(self)

        if self.guild.me:
            self.bot.loop.create_task(self.update_stage_topic(reconnect=False, clear=True))

            vc = _cu_voice_channel(self)

            try:
                await self.process_rpc(vc, close=True)
            except Exception:
                logger.exception("Falha em cleanup")

            if self.static:
                await _cu_static_session(self, inter)

            elif await _cu_close_player_message(self, inter):
                return

        self.queue.clear()

    async def process_destroy(self, force: bool = False, inter: disnake.MessageInteraction = None):
        if self.is_closing:
            return

        self.is_closing = True

        try:
            self.track_load_task.cancel()
        except Exception:
            pass

        try:
            self.event_queue_task.cancel()
        except Exception:
            pass

        try:
            self.lyric_task.cancel()
        except Exception:
            pass

        self._cancel_voice_status_tasks()  # envio pendente e reaplicação do status do canal (2.1.2)

        await self.cleanup(inter)

        try:
            channel = self.guild.voice_client.channel
        except AttributeError:
            channel = self.last_channel

        if (
            isinstance(channel, disnake.StageChannel)
            and self.stage_title_event
            and self.guild.me
            and self.guild.me.guild_permissions.manage_channels
        ):
            if channel.instance:
                try:
                    await channel.instance.delete()
                except Exception:
                    logger.exception("Falha em process_destroy")

        await super().destroy(force=force, guild=self.guild)

        self.bot.dispatch("player_destroy", player=self)

    async def process_save_queue(self, create_task=True):
        if self._session_resuming:
            await asyncio.sleep(10)
            self._session_resuming = False
            return

        if self._queue_updater_task and self.bot.config["PLAYER_SESSIONS_MONGODB"] and self.bot.config["MONGO"]:
            return

        cog = self.bot.get_cog("PlayerSession")

        if not cog:
            return

        try:
            self._queue_updater_task.cancel()
        except Exception:
            pass

        await cog.save_info(self)

        if create_task:
            self._queue_updater_task = self.bot.loop.create_task(cog.queue_updater_task(self))

    async def _send_rpc_data(self, users: List[int], stats: dict):
        for u in users:
            stats["user"] = u

            data = await self.bot.get_global_data(id_=u, db_name=DBModel.users)

            if self.bot.config["ENABLE_RPC_AUTH"] and not data["token"]:
                continue

            stats.update({"token": data["token"], "lastfm_user": data["lastfm"]["username"]})

            try:
                await self.bot.ws_client.send(stats)
            except Exception:
                logger.exception("Falha em _send_rpc_data")

    async def process_rpc(
        self,
        voice_channel: Union[disnake.VoiceChannel, disnake.StageChannel] = None,
        close=False,
        users: List[int] = None,
        wait: bool = False,
    ):
        if getattr(self, "radio_id", None):
            return  # rádio (item 8): o RPC levaria a URL do relay local, com o token, a cada membro da call
        try:
            if not voice_channel and not close:
                try:
                    voice_channel = self.bot.get_channel(self.channel_id) or self.bot.get_channel(
                        self.guild.voice_client.channel.id
                    )
                except AttributeError:
                    voice_channel = self.last_channel

            if not users:
                try:
                    users = voice_channel.voice_states
                except AttributeError:
                    # TODO: Investigar possível bug ao mover o bot de canal pelo discord.
                    return

            thumb = self.bot.user.display_avatar.replace(size=512, static_format="png").url

            users = [u for u in users if u != self.bot.user.id]

            if close:
                stats = {
                    "op": "close",
                    "bot_id": self.bot.user.id,
                    "bot_name": str(self.bot.user),
                    "thumb": thumb,
                    "auth_enabled": self.bot.config["ENABLE_RPC_AUTH"],
                }

                if wait:
                    await self._send_rpc_data(users, stats)
                else:
                    try:
                        self._rpc_update_task.cancel()
                    except Exception:
                        pass
                    self._rpc_update_task = self.bot.loop.create_task(self._send_rpc_data(users, stats))
                return

            if self.is_closing:
                return

            stats = {
                "op": "update",
                "track": None,
                "bot_id": self.bot.user.id,
                "bot_name": str(self.bot.user),
                "thumb": thumb,
                "guild": self.guild.name,
                "auth_enabled": self.bot.config["ENABLE_RPC_AUTH"],
                "listen_along_invite": self.listen_along_invite,
            }

            if not self.current:
                try:
                    bot_public = self.bot.appinfo.bot_public
                except AttributeError:
                    bot_public = False

                stats.update(
                    {
                        "op": "idle",
                        "bot_id": self.bot.user.id,
                        "invite_permissions": self.bot.config["INVITE_PERMISSIONS"],
                        "bot_name": str(self.bot.user),
                        "public": bot_public,
                        "support_server": self.bot.config["SUPPORT_SERVER"],
                    }
                )

                try:
                    stats["idle_endtime"] = int(self.idle_endtime.timestamp())
                    stats["idle_starttime"] = self.idle_start_timestamp
                except Exception:
                    pass

            else:
                track: Union[LavalinkTrack, PartialTrack] = self.current

                stats["track"] = {
                    "source": track.info["sourceName"],
                    "thumb": track.thumb if len(track.thumb) < 257 else "",
                    "title": track.single_title,
                    "url": track.uri,
                    "author": track.authors_string,
                    "stream": track.is_stream,
                    "position": self.position,
                    "paused": self.is_paused,
                    "requester_id": track.requester,
                    "loop": self.current.track_loops or self.loop,
                    "queue": len(self.queue),
                    "247": self.keep_connected,
                    "autoplay": self.current.autoplay,
                }

                stats["start_time"] = self.start_timestamp

                if self.current.is_stream:
                    stats["track"]["duration"] = int(self.start_time.timestamp())
                else:
                    stats["track"]["duration"] = track.duration

                if track.playlist_name:
                    stats["track"].update(
                        {
                            "playlist_name": track.playlist_name,
                            "playlist_url": track.playlist_url,
                        }
                    )

                if track.album_name:
                    stats["track"].update(
                        {
                            "album_name": track.album_name,
                            "album_url": track.album_url,
                        }
                    )

            if wait:
                await self._send_rpc_data(users, stats)
            else:
                try:
                    self._rpc_update_task.cancel()
                except Exception:
                    pass

                self._rpc_update_task = self.bot.loop.create_task(self._send_rpc_data(users, stats))

        except Exception:
            logger.exception("Falha em process_rpc")
