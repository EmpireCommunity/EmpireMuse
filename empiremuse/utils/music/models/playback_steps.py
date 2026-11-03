# -*- coding: utf-8 -*-
"""Etapas de `LavalinkPlayer._process_next` (escolha, preparo e início da próxima faixa)."""

from __future__ import annotations

import asyncio
import datetime
from time import time
from typing import Union, TYPE_CHECKING

import disnake
from rapidfuzz import fuzz

from empiremuse.utils.db import DBModel
from empiremuse.utils.music.converters import fix_characters, md_link
from empiremuse.utils.music.errors import (
    error_code_text,
    log_unexpected_error,
)
from empiremuse.utils.others import PlayerControls, string_to_file
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


async def _pn_pre_checks(self, req):
    """Confere a conexão e prepara o player antes de escolher a próxima faixa."""
    await self.bot.wait_until_ready()

    if not self.is_connected:
        return True

    if not self.guild.me.voice:
        if self.last_channel:
            await self.connect(self.last_channel.id)
        else:
            return True

    if not self.channel_purged and self.static and self.text_channel:
        data = await self.bot.get_data(self.guild_id, db_name=DBModel.guilds)
        if data["player_controller"]["channel"] == str(self.text_channel.id):
            await self.channel_cleanup()
        else:
            self.channel_purged = True

    try:
        self.idle_task.cancel()
    except Exception:
        pass

    if not self.controller_mode and self.idle_task:
        await self.message.delete()

    self.idle_task = None
    return False


async def _pn_next_track(self, req):
    """Escolhe a próxima faixa: fila, autoplay ou modo ocioso."""
    try:
        track = self.queue.popleft()

    except Exception:
        if getattr(self, "radio_id", None) is not None:
            # Rádio (ao vivo ou personalizada) toca só o que o runner mandou: fila vazia NÃO puxa autoplay nem entra em modo ocioso (o autoplay buscaria
            # recomendações no YouTube e afins). Quem decide o que tocar de novo é o runner.
            await self.stop()
            return True
        try:
            track = None

            if self.autoplay or self.keep_connected:
                try:
                    track = await self.get_autoqueue_tracks()
                except Exception:
                    logger.exception("Falha em _pn_next_track")
                    self.locked = False
                    await asyncio.sleep(60)
                    if not self.current and (self.autoplay or self.keep_connected):
                        await self.process_next()
                    return True

            if not track:
                await self.stop()
                now = disnake.utils.utcnow()
                self.idle_start_timestamp = int(now.timestamp())
                self.idle_endtime = now + datetime.timedelta(seconds=self.bot.config["IDLE_TIMEOUT"])
                self.last_track = None
                self.idle_task = self.bot.loop.create_task(self.idling_mode())
                self.bot.dispatch("player_queue_end", player=self)
                return True

        except Exception:
            logger.exception("Falha em _pn_next_track")
            track = None

    if not track:
        await self.process_next()
        return True

    req.track = track
    return False


async def _pn_sources(self, req):
    """Resolve faixas parciais e fontes que o nó não suporta."""
    error_code = req.error_code
    track = req.track

    if track.info["sourceName"] not in self.node.info["sourceManagers"] and not isinstance(track, PartialTrack):
        track.id = ""

    if isinstance(track, PartialTrack) and track.info["sourceName"] != "youtube":
        if not track.id:
            try:
                await self.resolve_track(track)
            except Exception as e:
                error_code = log_unexpected_error(logger, e, f"resolução da faixa parcial {track.uri}")
                try:
                    await self.text_channel.send(
                        embed=disnake.Embed(
                            description=f"Houve um problema ao tentar processar a música {md_link(track.title, track.uri, code=False)}..."
                            f"{error_code_text(error_code)}",
                            color=self.bot.get_color(),
                        )
                    )
                except Exception:
                    logger.exception("Falha em _pn_sources")

                self.locked = False
                self.native_yt = True

                await self.process_next()
                return True

            if not track.id:
                try:
                    await self.text_channel.send(
                        embed=disnake.Embed(
                            description=f"A música {md_link(track.title, track.uri, code=False)} não está disponível...\n"
                            f"Pulando para a próxima música...",
                            color=self.bot.get_color(),
                        ),
                        delete_after=10,
                    )
                except Exception:
                    logger.exception("Falha em _pn_sources")

                await asyncio.sleep(10)

                self.locked = False

                await self.process_next()
                return True

    req.error_code = error_code
    return False


async def _pn_native(self, req):
    """Prepara a reprodução nativa do YouTube e a busca alternativa."""
    encoded_track = req.encoded_track
    track = req.track

    if not self.native_yt or not self.node.prefer_youtube_native_playback:
        cache_key = f"{track.info['sourceName']}:id:{track.identifier or track.id or track.ytid}"

        partial_data = self.bot.pool.partial_track_cache.get(cache_key)

        if track.info["sourceName"] == "youtube" or (partial_data and partial_data[0].info["sourceName"] == "youtube"):
            if track.is_stream or track.duration > 480000:
                if not self.native_yt:
                    self.played.append(track)
                    self.locked = False
                    self.native_yt = True
                    await self.process_next()
                    return True

                try:
                    encoded_track = partial_data[0].id
                except Exception:
                    pass

            else:
                tracks = []

                exceptions: list[Exception] = []

                if not partial_data:
                    for provider in self.node.search_providers:
                        if provider in ("ytsearch", "ytmsearch"):
                            continue

                        if track.author.endswith(" - topic"):
                            query = f"{provider}:{track.title} - {track.author[:-8]}"
                        else:
                            query = f"{provider}:{track.title}"

                        tracks = self.bot.pool.partial_track_cache.get(query)

                        if not tracks:
                            try:
                                tracks = await self.node.get_tracks(
                                    query, track_cls=LavalinkTrack, playlist_cls=LavalinkPlaylist
                                )
                            except Exception as e:
                                exceptions.append(e)
                                await asyncio.sleep(1)
                                continue

                            try:
                                tracks = tracks.tracks
                            except AttributeError:
                                pass

                            self.bot.pool.partial_track_cache[query] = tracks

                        if not any(i in track.title.lower() for i in exclude_tags):
                            final_result = []
                            for t in tracks:
                                if not any((i in t.title.lower()) for i in exclude_tags):
                                    final_result.append(t)
                            tracks = final_result or tracks

                        min_duration = track.duration - 7000
                        max_duration = track.duration + 7000

                        final_result = []

                        for t in tracks:
                            if (
                                t.is_stream
                                or not min_duration < t.duration < max_duration
                                or fuzz.token_set_ratio(t.title, track.title) < TITLE_MATCH_THRESHOLD
                            ):
                                continue
                            final_result.append(t)

                        if not final_result:
                            tracks = []
                            continue

                        tracks = final_result
                        break

                else:
                    tracks = partial_data

                if not tracks:
                    if not self.native_yt:
                        if exceptions:
                            logger.warning(
                                "Faixa sem resultado em outras plataformas (%s)",
                                ", ".join(type(e).__name__ for e in exceptions),
                            )
                            logger.debug("Exceções ao resolver a faixa: %r", exceptions)
                        self.played.append(track)
                        self.set_command_log(
                            emoji="⚠️",
                            text=f"A música {md_link(track.title[:15], track.uri)} será pulada devido a falta de resultado "
                            "em outras plataformas de música.",
                            controller=True,
                        )
                        await asyncio.sleep(3)
                        self.locked = False
                        self.native_yt = True
                        await self.process_next()
                        return True

                else:
                    alt_track = tracks[0]
                    encoded_track = alt_track.id
                    self.bot.pool.partial_track_cache[cache_key] = [alt_track]
                    self.set_command_log(
                        emoji="▶️",
                        text=f"Tocando música obtida via metadados: {md_link(alt_track.title, alt_track.uri, limit=20)} `| Por: {fix_characters(alt_track.author, 15)}`",
                        controller=True,
                    )
                    self.native_yt = True

    req.encoded_track = encoded_track
    return False


async def _pn_encoded(self, req):
    """Garante o identificador codificado da faixa."""
    encoded_track = req.encoded_track
    track = req.track

    if not encoded_track and not track.id:
        await self.resolve_track(track)

        if not track.id:
            try:
                await self.text_channel.send(
                    embed=disnake.Embed(
                        description=f"A música {md_link(track.title, track.uri, code=False)} não está disponível...\n"
                        "Pulando para a próxima música...",
                        color=self.bot.get_color(),
                    ),
                    delete_after=10,
                )
            except Exception:
                logger.exception("Falha em _pn_encoded")

            await asyncio.sleep(10)

            self.locked = False

            await self.process_next()
            return True
    return False


async def _pn_start(self, req):
    """Atualiza o estado do player, toca a faixa e atualiza a mensagem."""
    encoded_track = req.encoded_track
    force_np = req.force_np
    inter = req.inter
    start_position = req.start_position
    track = req.track

    self.last_track = track

    self.is_previows_music = False

    self.locked = False

    if track.is_stream:
        start_position = 0

    self.current = track
    self.last_update = 0
    self.last_position = start_position
    self.position_timestamp = 0
    self.paused = False

    self.process_hint()

    self.start_time = disnake.utils.utcnow()
    self.start_timestamp = self.start_time.timestamp()

    if self.auto_pause:
        self.last_update = time() * 1000
        self.current = track
        self.current_encoded = None
        self.start_auto_skip()
        self.bot.loop.create_task(self.node.on_event(TrackStart({"track": track, "player": self, "node": self.node})))
        self.set_command_log(
            emoji="🪫",
            text="O player está no modo **[economia de recursos]** (esse modo será desativado automaticamente quando "
            f"um membro entrar no canal <#{self.channel_id}>).",
            controller=True,
        )
    else:
        await self.play(track, start=start_position, temp_id=encoded_track)

    # TODO: rever essa parte caso adicione função de ativar track loops em músicas da fila
    if self.loop != "current" or force_np or (not self.controller_mode and self.current.track_loops == 0):
        if start_position:
            await asyncio.sleep(1)

        await self.invoke_np(
            interaction=inter,
            force=True if (self.static or not self.loop or not self.is_last_message()) else False,
            rpc_update=True,
        )

    req.start_position = start_position
