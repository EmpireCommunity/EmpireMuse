# -*- coding: utf-8 -*-
"""Etapas de `LavalinkPlayer.hook_events` (um tratador por tipo de evento do Lavalink)."""

from __future__ import annotations

import asyncio
import logging
import pprint

import disnake

import empiremuse.wavelink as wavelink
from empiremuse.logs.context import log_context
from empiremuse.rules.error_code import safe_error_code
from empiremuse.rules.text import plain_snippet
from empiremuse.utils.music.converters import time_format, md_link, md_code
from empiremuse.utils.music.errors import code_suffix
from empiremuse.utils.music.models.helpers import (
    LAVALINK_LOG_LIMIT,
    get_start_pos,
    lavalink_text,
)
from empiremuse.utils.music.models.tracks import PartialTrack
from empiremuse.utils.usage import usage_of

logger = logging.getLogger("empiremuse.player")

# Causas reportadas pelo Lavalink que valem nova tentativa da mesma música (falhas de rede/decodificação).
RETRYABLE_ERROR_CAUSES = (
    "java.lang.IllegalStateException: Failed to get media URL: 2000: An error occurred while decoding track token",
    "java.net.SocketTimeoutException: Connect timed out",
    "java.net.SocketTimeoutException: Read timed out",
    "java.net.SocketException: Network is unreachable",
    "java.net.ConnectException: Connection refused",
    "java.lang.IllegalArgumentException: Invalid bitrate",
    "java.io.EOFException",
    "java.net.UnknownHostException:",
    "java.lang.IllegalStateException: Connection pool shut down",
    "java.lang.IllegalStateException: Error from decoder",
    "java.lang.IllegalStateException: Current position is beyond this element",
    "com.sedmelluq.discord.lavaplayer.tools.io.PersistentHttpStream$PersistentHttpException: Not success status code: 403",
)

# Marcas de falha de download do YouTube que valem UMA nova tentativa da mesma faixa (o Lavalink manda o `toString()` da exceção,
# que começa com o nome da classe do plugin, então a busca é por trecho e não por prefixo).
SAME_TRACK_RETRY_MARKERS = (
    "All clients failed",
    "Read timed out",
)
SAME_TRACK_RETRY_DELAY = 2
YOUTUBE_PLUGIN_CAUSE_MARKER = "dev.lavalink.youtube."

# Causas do plugin de espelhamento (lavasrc) quando nenhuma outra plataforma tem a música.
MIRROR_NOT_FOUND_CAUSES = (
    "com.github.topi314.lavasrc.mirror.TrackNotFoundException: No tracks found in playlist or search result for track",
    "com.github.topi314.lavasrc.mirror.TrackNotFoundException: Playlist is empty",
    "com.github.topi314.lavasrc.mirror.TrackNotFoundException: No mirror found for track",
)


def _he_source(track) -> str:
    return str(getattr(track, "source_name", None) or "desconhecida")


def _he_duration(track) -> str:
    duration = getattr(track, "duration", None)
    return time_format(duration) if isinstance(duration, int | float) else "—"


def _he_voice_channel_id(self):
    return getattr(self, "channel_id", None)


def _he_log_track_event(self, kind: str, track) -> None:
    """Início e fim de faixa: DEBUG (o título só aqui, D4); aparecem só com o servidor em ``LOG_DEBUG_GUILDS`` ou ``LOG_LEVEL=DEBUG``."""
    logger.debug(
        "%s: %s (duração %s, origem %s, canal %s)",
        kind,
        getattr(track, "title", None),
        _he_duration(track),
        _he_source(track),
        _he_voice_channel_id(self),
    )


def _he_same_node(self, event) -> bool:
    """O evento é do nó atual do player? (os de nó antigo são descartados, e o registro de uso também os ignora)"""
    return event.node.identifier == self.node.identifier


LIVE_LOST_REASONS = frozenset(
    {"FINISHED", "LOAD_FAILED"}
)  # fim e falha de um fluxo ao vivo; STOPPED/REPLACED/CLEANUP são nossos


def _he_live_radio(self, event) -> bool:
    """Rádio ao vivo (item 8, R3): fim, exceção e travada do fluxo NUNCA seguem o caminho de música (fila, autoplay, destroy); viram um aviso ao
    ``RadioManager``, que volta pelo recuo de 2 a 32 s. Devolve ``True`` se o evento foi consumido aqui."""
    if not getattr(self, "radio_live", False) or not isinstance(
        event, (wavelink.TrackEnd, wavelink.TrackException, wavelink.TrackStuck)
    ):
        return False
    lost = not isinstance(event, wavelink.TrackEnd) or str(getattr(event, "reason", "")).upper() in LIVE_LOST_REASONS
    if lost and (radio_id := getattr(self, "radio_id", None)):
        self.bot.pool.radio.manager.note_stream_lost(radio_id)
    return True


async def _he_dispatch(self, event) -> None:
    """Despacha o evento do Lavalink com o contexto de log (bot e servidor) e as métricas do pool."""
    with log_context(bot=getattr(self.bot, "identifier", None), guild=self.guild.id):
        if _he_live_radio(self, event):
            return
        metrics = getattr(getattr(self.bot, "pool", None), "metrics", None)
        if isinstance(event, wavelink.TrackEnd):
            if metrics:
                metrics.track_ended(self.guild.id, str(getattr(event, "reason", "")))
            if (usage := usage_of(self.bot)) and _he_same_node(self, event):
                usage.playback_ended(self.bot, self.guild.id, getattr(event, "reason", ""))
            _he_log_track_event(self, f"Faixa terminada ({getattr(event, 'reason', '?')})", self.last_track)
            await _he_track_end(self, event)
        elif isinstance(event, wavelink.TrackStart):
            if metrics:
                metrics.track_started(self.guild.id)
            if (usage := usage_of(self.bot)) and _he_same_node(self, event):
                usage.playback_started(self.bot, self, self.current)
            _he_log_track_event(self, "Faixa iniciada", self.current)
            await _he_track_start(self, event)
        elif isinstance(event, wavelink.TrackException):
            await _he_track_exception(self, event)
        elif isinstance(event, wavelink.TrackStuck):
            if metrics:
                metrics.error("track_stuck")
            # Sem o título (D4): servidor (contexto), canal, duração, origem e a posição de quando travou.
            logger.warning(
                "Faixa travada: canal %s, duração %s, origem %s, posição %s",
                _he_voice_channel_id(self),
                _he_duration(self.current),
                _he_source(self.current),
                time_format(getattr(self, "position", 0) or 0),
            )
            await _he_track_stuck(self)
        else:
            logger.debug("Evento Wavelink desconhecido: %s", type(event).__name__)


def _he_cancel_message_updater(self) -> None:
    try:
        self.message_updater_task.cancel()
    except Exception:
        pass


def _he_can_send_in_text_channel(self):
    if not self.text_channel:
        return False

    if isinstance(self.text_channel, disnake.Thread):
        return self.text_channel.parent.permissions_for(self.guild.me).send_messages_in_threads
    return self.text_channel.permissions_for(self.guild.me).send_messages


def _he_spawn_new_node_task(self, **kwargs) -> None:
    try:
        self._new_node_task.cancel()
    except Exception:
        pass
    self._new_node_task = self.bot.loop.create_task(self._wait_for_new_node(**kwargs))


async def _he_track_end(self, event) -> None:
    if event.node.identifier != self.node.identifier:
        return

    self.bot.dispatch("wavelink_track_end", player=self, track=self.last_track, reason=event.reason)

    if event.reason == "FINISHED":
        # Só o progresso real devolve a nova tentativa: o TrackStart sai antes de o Lavalink abrir o stream.
        _he_reset_retry_budget(self)

    if self.locked:
        return

    if event.reason == "FINISHED":
        self.set_command_log(controller=True)

    elif event.reason == "STOPPED":
        if len(self.queue) == 0:
            return

        self.ignore_np_once = True

    else:
        return

    _he_cancel_message_updater(self)

    await self.track_end()

    self.update = False

    await self.process_next()


async def _he_track_start(self, event) -> None:
    if event.node.identifier != self.node.identifier:
        return

    self.bot.dispatch("wavelink_track_start", player=self)

    if self.text_channel and not _he_can_send_in_text_channel(self):
        self.text_channel = None

    if not await _he_wait_for_bot_voice_state(self):
        return

    try:
        await self.process_save_queue()
    except asyncio.CancelledError:
        # cancelamento da task do hook_events: não há o que limpar, só propagar
        raise
    except Exception:
        logger.exception("Falha em _he_track_start")

    await asyncio.sleep(2)
    await self.update_stage_topic()


async def _he_wait_for_bot_voice_state(self) -> bool:
    if self.guild.me.voice:
        return True

    try:
        await self.bot.wait_for("voice_state_update", check=lambda m, b, a: m == self.guild.me and m.voice, timeout=7)
    except asyncio.TimeoutError:
        self.update = True
        return False

    return True


async def _he_track_stuck(self) -> None:
    _he_cancel_message_updater(self)

    await self.track_end()

    self.update = False

    try:
        self.set_command_log(
            text=f"A música {md_link(self.current.single_title, self.current.uri, code=False, limit=25)} travou.",
            emoji="⚠️",
            controller=True,
        )
    except Exception:
        pass

    await self.process_next()


def _he_log_exception(self, event, track) -> str:
    """Registra a falha da faixa (sem o título) e devolve o código curto que o usuário vê no embed.

    WARNING (ERROR com ``severity=fault``); causa do plugin de espelhamento sem resultado vai para DEBUG. Conta nas métricas.
    """
    code = safe_error_code()
    metrics = getattr(getattr(self.bot, "pool", None), "metrics", None)
    if metrics:
        metrics.error("track_exception")
    cause = str(event.cause or "")
    if cause.startswith(MIRROR_NOT_FOUND_CAUSES):
        level = logging.DEBUG
    elif str(event.severity).lower() == "fault":
        level = logging.ERROR
    else:
        level = logging.WARNING
    logger.log(
        level,
        "Falha ao reproduzir a música%s: canal %s, origem %s, nó %s, severidade %s, mensagem %s, causa %s",
        f" [{code}]" if code else "",
        _he_voice_channel_id(self),
        _he_source(track),
        event.node.identifier,
        event.severity,
        plain_snippet(event.message, LAVALINK_LOG_LIMIT),
        plain_snippet(cause, LAVALINK_LOG_LIMIT),
        # O agregador usa o código para apontar o traceback já impresso (K8/K15).
        extra={"error_code": code} if code else None,
    )
    return code


def _he_exception_embed(self, event, track):
    node_info = (
        md_code(event.node.identifier)
        if event.node.identifier == self.node.identifier
        else f"`{self.node.identifier} | {event.node.identifier}`"
    )
    error_code = _he_log_exception(self, event, track)
    embed = disnake.Embed(
        description=f"**Falha ao reproduzir música{' (parcial)' if isinstance(track, PartialTrack) else ''}:\n{md_link(track.title, track.uri or track.search_uri, code=False)}**\n"
        f"**Motivo:** {lavalink_text(event.message)}\n"
        f"**Nível:** {md_code(event.severity)}\n"
        f"**Servidor de música:** {node_info}{code_suffix(error_code)}",
        color=disnake.Colour.red(),
    )
    return embed, error_code


def _he_log_retry_while_locked(self, event, track, error_code) -> None:
    self.set_command_log(
        text=f"A reprodução da música falhou (tentando tocar novamente): {md_link(track.title, track.uri or track.search_uri, limit=15)}. **Motivo:** `{plain_snippet(event.message, 50)}`{code_suffix(error_code)}",
        controller=True,
    )
    self.update = True


def _he_mirror_not_found_embed(self, track):
    return disnake.Embed(
        description=f"`Ignorando a música` {md_link(track.title, track.url)}`. Pois não houve resultados em outras plataformas de música.`",
        color=self.bot.get_color(self.guild.me),
    ).set_thumbnail(track.thumb)


async def _he_report_exception(self, embed, track, error_format) -> None:
    logger.debug(
        "Erro ao reproduzir a música: %s | nó: %s\n%s",
        track.uri or track.search_uri,
        self.node.identifier,
        error_format,
    )

    await self.report_error(embed, track)


def _he_cause_text(event) -> str:
    return "\n".join((str(event.cause or ""), str(event.message or "")))


def _he_cause_has(event, markers) -> bool:
    """True quando alguma marca aparece em qualquer posição da causa ou da mensagem do evento."""
    text = _he_cause_text(event)
    return any(marker in text for marker in markers)


def _he_retry_key(track) -> str:
    return str(getattr(track, "uri", None) or getattr(track, "search_uri", None) or id(track))


def _he_take_once(self, attr: str, track) -> bool:
    """Concede UMA vez por faixa (conjunto `attr` do player) até alguma faixa terminar de verdade."""
    used = getattr(self, attr, None)
    if used is None:
        used = set()
        setattr(self, attr, used)
    key = _he_retry_key(track)
    if key in used:
        return False
    used.add(key)
    return True


def _he_reset_retry_budget(self) -> None:
    for attr in ("same_track_retries", "youtube_recoveries"):
        getattr(self, attr, set()).clear()


def _he_take_same_track_retry(self, event, track) -> bool:
    """A nova tentativa da mesma faixa nas falhas de download do YouTube (uma vez por faixa)."""
    return _he_cause_has(event, SAME_TRACK_RETRY_MARKERS) and _he_take_once(self, "same_track_retries", track)


def _he_other_youtube_node(self):
    """Outro nó disponível com busca do YouTube, ou None."""
    for n in self.bot.music.nodes.values():
        if n != self.node and n.is_available and "ytsearch" in n.search_providers:
            return n
    return None


def _he_is_youtube_failure(self, event, track) -> bool:
    """Faixa do YouTube, ou de outra origem espelhada no YouTube (causa do plugin do YouTube) quando há outro nó para trocar.

    Sem outro nó a faixa espelhada segue o caminho comum (relata e pula), para não voltar à fila sem fim.
    """
    if track.source_name == "youtube":
        return True
    return _he_is_youtube_plugin_cause(event) and _he_other_youtube_node(self) is not None


def _he_is_youtube_plugin_cause(event) -> bool:
    """A marca do plugin do YouTube aparece na causa ou na mensagem (a identificação independe de haver outro nó)."""
    return _he_cause_has(event, (YOUTUBE_PLUGIN_CAUSE_MARKER,))


async def _he_track_exception(self, event) -> None:
    track = self.current or self.last_track
    embed, error_code = _he_exception_embed(self, event, track)
    error_format = pprint.pformat(event.data)

    async def send_report():
        await _he_report_exception(self, embed, track, error_format)

    if event.node.identifier != self.node.identifier:
        await send_report()
        return

    if self.locked:
        _he_log_retry_while_locked(self, event, track, error_code)
        await send_report()
        return

    self.locked = True
    self.current = None
    self.current_encoded = None
    cooldown = 10
    start_position = 0

    if _he_take_same_track_retry(self, event, track):
        resume_position = get_start_pos(self, track)  # antes da espera, para não contar os segundos parados
        self.queue.appendleft(track)
        await asyncio.sleep(SAME_TRACK_RETRY_DELAY)
        self.locked = False
        await self.process_next(start_position=resume_position)
        return

    # Falha do plugin do YouTube: nunca vai ao retry geral. Troca de nó (uma vez por faixa) quando é possível; senão relata e pula.
    youtube_plugin_cause = _he_is_youtube_plugin_cause(event)
    youtube_failure = _he_is_youtube_failure(self, event, track)
    if youtube_plugin_cause and youtube_failure and _he_take_once(self, "youtube_recoveries", track):
        await _he_recover_from_youtube(self, track, send_report, get_start_pos(self, track))
        return

    if ignore_error_log := (not youtube_plugin_cause and _he_cause_has(event, RETRYABLE_ERROR_CAUSES)):
        outcome = _he_retry_after_general_error(self, track)
        if outcome == "wait":
            return

        if outcome == "retry":
            start_position = get_start_pos(self, track)
        else:
            # Faixa abandonada: o usuário recebe o aviso da falha (como na via comum).
            ignore_error_log = False
        cooldown = 4

    elif event.cause == "java.lang.InterruptedException":
        self.queue.appendleft(track)
        _he_spawn_new_node_task(self)
        await send_report()
        return

    elif not youtube_plugin_cause and youtube_failure and _he_take_once(self, "youtube_recoveries", track):
        await _he_recover_from_youtube(self, track, send_report, get_start_pos(self, track))
        return

    elif not track.track_loops:
        self.failed_tracks.append(track)

    elif self.keep_connected and not track.autoplay and len(self.queue) > 15:
        self.queue.append(track)

    if event.cause.startswith(MIRROR_NOT_FOUND_CAUSES):
        embed = _he_mirror_not_found_embed(self, track)

    send_message_perm = _he_can_send_in_text_channel(self)
    await send_report()

    if not ignore_error_log and embed and self.text_channel and send_message_perm:
        await self.text_channel.send(embed=embed, delete_after=10)

    await asyncio.sleep(cooldown)

    self.locked = False
    await self.process_next(start_position=start_position)


def _he_retry_after_general_error(self, track) -> str:
    """Decide o que fazer com a falha de rede: "wait" (esperar um novo nó), "skip" (desistir da faixa) ou "retry" (tentar de novo).

    Esgotadas as tentativas com o nó disponível não há o que esperar (``_wait_for_new_node`` voltaria na hora e o player
    ficaria travado), então a faixa é descartada e o chamador relata e segue.
    """

    if not hasattr(self, "retries_general_errors"):
        self.retries_general_errors = {
            "counter": 6,
            "last_node": self.node.identifier,
            "last_time": disnake.utils.utcnow(),
        }

    self.queue.appendleft(track)

    if (
        self.retries_general_errors["counter"] < 1
        and self.node.identifier == self.retries_general_errors["last_node"]
        and (disnake.utils.utcnow() - self.retries_general_errors["last_time"]).total_seconds() < 180
    ):
        if self.node.is_available:
            self.queue.popleft()
            self.failed_tracks.append(track)
            # O orçamento geral é do episódio: ao abandonar a faixa ele recomeça para a próxima.
            del self.retries_general_errors
            return "skip"
        _he_spawn_new_node_task(self, ignore_node=self.node)
        return "wait"

    self.retries_general_errors["last_time"] = disnake.utils.utcnow()

    if self.retries_general_errors["last_node"] == self.node.identifier:
        self.retries_general_errors["counter"] -= 1
    else:
        self.retries_general_errors = {
            "counter": 6,
            "last_node": self.node.identifier,
            "last_time": disnake.utils.utcnow(),
        }

    return "retry"


async def _he_recover_from_youtube(self, track, send_report, start_position: int | float = 0) -> None:
    self.current = None
    self.current_encoded = None
    self.native_yt = False
    self.queue.appendleft(track)

    try:
        new_node = [
            n
            for n in self.bot.music.nodes.values()
            if n != self.node and n.is_available and "ytsearch" in n.search_providers
        ][0]
    except Exception:
        new_node = None

    if new_node:
        await _he_move_to_native_youtube(self, new_node, start_position)
        await send_report()
        return

    if not getattr(self, "yt_warn", None):
        await _he_warn_youtube_fallback(self, send_report)

    self.locked = False
    await self.process_next(start_position=start_position)


async def _he_move_to_native_youtube(self, new_node, start_position: int | float = 0) -> None:
    self.native_yt = True
    txt = f"Devido a restrições do youtube no servidor {md_code(self.node.identifier)} o player foi movido para o servidor {md_code(new_node.identifier)}."
    if self.controller_mode:
        self.set_command_log(txt, emoji="⚠️", controller=True)
    elif self.text_channel:
        try:
            await self.text_channel.send(
                embed=disnake.Embed(description=f"-# `⚠️ -` {txt}", color=self.bot.get_color(self.guild.me)),
                delete_after=10,
            )
        except Exception:
            logger.exception("Falha em _he_move_to_native_youtube")
    await asyncio.sleep(5)
    await self.change_node(new_node.identifier)
    self.locked = False
    await self.process_next(start_position=start_position)


async def _he_warn_youtube_fallback(self, send_report) -> None:
    txt = (
        f"Devido a restrições do youtube no servidor {md_code(self.node.identifier)}. Durante a sessão atual "
        "será feito uma tentativa de obter a mesma música em outras plataformas de música usando o nome "
        "das músicas do youtube que estão na fila (talvez a música tocada seja diferente do esperado "
        "ou até mesmo ignoradas caso não retorne resultados)."
    )
    try:
        await self.text_channel.send(
            embed=disnake.Embed(description=txt, color=self.bot.get_color(self.guild.me)),
            delete_after=30,
        )
    except Exception:
        self.set_command_log(text=txt, emoji="⚠️", controller=True)
    self.yt_warn = True
    await send_report()
