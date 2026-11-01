# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import asyncio

import empiremuse.wavelink as wavelink
from empiremuse.logs.context import guild_extra
from empiremuse.rules.text import plain_snippet
from empiremuse.utils.music.models.event_steps import _he_dispatch
from empiremuse.utils.music.models.helpers import LAVALINK_LOG_LIMIT

logger = logging.getLogger("empiremuse.player")

# Só os códigos conhecidos têm chave própria: os de fechamento do WebSocket (RFC 6455, 1000-1015) e os da voz do Discord (4000-4022).
# O code vem do JSON do nó (de terceiros): um nó hostil que o variasse faria cada evento virar uma chave própria.
VOICE_CLOSE_CODES = frozenset((*range(1000, 1016), *range(4000, 4023)))


def _voice_close_key(code: object) -> str:
    """Chave de agregação do erro de voz: o próprio código se for um dos conhecidos (``VOICE_CLOSE_CODES``), senão ``other``."""
    return str(code) if isinstance(code, int) and not isinstance(code, bool) and code in VOICE_CLOSE_CODES else "other"


class PlayerEventsMixin:
    async def hook(self, event) -> None:
        if isinstance(event, wavelink.WebsocketClosed):
            if event.code == 1000:
                return

            if radio_id := getattr(self, "radio_id", None):
                # rádio (item 8): a volta é só do ``RadioRunner`` (recuo 5 a 60 s com jitter, premium reconferido, limite do processo);
                # o reconecta antigo de 3 a 5 s fixos abriria uma segunda volta, imediata e sem limite
                self.bot.pool.radio.manager.note_disconnect(radio_id)
                return

            if not self.guild.me or (
                event.code == 4014 and event.reason.startswith("Disconnected") and not self.keep_connected
            ):
                await self.destroy(force=True)
                return

            try:
                vc = self.last_channel or self.guild.me.voice.channel
            except AttributeError:
                vc = None

            if event.code == 4022:
                if self.keep_connected and vc:
                    await asyncio.sleep(5)
                    self.set_command_log(
                        emoji="⚠️",
                        text=self.tr("player.if_you_want_to_disconnect", mention=str(vc.mention)),
                    )
                    await self.connect(vc.id)
                    self.update = True
                else:
                    await self.destroy(force=True)
                    logger.debug("Desconectado do canal de voz: %s", self.guild_id)
                return

            if event.code == 4014 and self.guild.me.voice:
                pass
            else:
                logger.warning(
                    "Erro no canal de voz: bot %s, canal %s, servidor de música %s, code %s, reason %s",
                    "online" if self.bot.is_ready() else "offline",
                    vc.id,
                    self.node.identifier,
                    event.code,
                    plain_snippet(event.reason, LAVALINK_LOG_LIMIT),
                    extra={"aggregate_key": _voice_close_key(event.code), **guild_extra(self.guild_id)},
                )

            if self.is_closing:
                return

            if event.code in (
                4000,  # internal error
                1006,
                1001,
                4016,  # Connection started elsewhere
                4005,  # Already authenticated.
            ):
                try:
                    vc_id = self.guild.me.voice.channel.id
                except AttributeError:
                    vc_id = self.last_channel.id

                await asyncio.sleep(3)

                if self.is_closing:
                    return

                await self.connect(vc_id)
                return

            if event.code in (
                4014,
                4006,  # Session is no longer valid.
            ):
                await asyncio.sleep(5)

                try:
                    self.bot.music.players[self.guild_id]
                except KeyError:
                    return

                if self.guild and self.guild.me.voice or self.is_closing:
                    return

                try:
                    vc_id = self.guild.me.voice.channel.id
                except AttributeError:
                    vc_id = self.last_channel.id

                await self.connect(vc_id)
                return

        else:
            await self.event_queue.put(event)

    async def hook_events(self):
        while True:
            try:
                event = await self.event_queue.get()

                if self.is_closing:
                    return

                await self.bot.wait_until_ready()

                await _he_dispatch(self, event)

            except Exception:
                logger.exception("Falha em hook_events")

    async def track_end(self, ignore_track_loop=False):
        self.votes.clear()

        self.locked = True

        await asyncio.sleep(0.5)

        if self.last_track:
            if not self.last_track.is_stream and self.last_track.duration < 60000:
                await asyncio.sleep(7)

            if self.loop == "current":
                self.queue.appendleft(self.last_track)
            elif self.is_previows_music:
                self.queue.insert(1, self.last_track)
                self.is_previows_music = False
            elif self.last_track.track_loops and not ignore_track_loop:
                self.last_track.info["extra"]["track_loops"] -= 1
                self.queue.insert(0, self.last_track)
            elif self.loop == "queue":  # or self.keep_connected:
                if self.is_previows_music:
                    self.queue.insert(1, self.last_track)
                    self.is_previows_music = False
                else:
                    self.queue.append(self.last_track)
            elif not self.last_track.autoplay:
                self.played.append(self.last_track)

        elif self.is_previows_music:
            self.is_previows_music = False

        self.locked = False
