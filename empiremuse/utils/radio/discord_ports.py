"""As portas reais do ``RadioRunner`` (item 8, R1): Discord e Lavalink de UM bot reserva tocando UMA rádio.

Isto é só cola fina: as decisões (recuo, presença, limites, premium) são do ``RadioRunner``. Cada rádio é um ``LavalinkPlayer`` comum,
marcado com ``radio_id`` (os ganchos de ``player_voice`` mandam presença e queda ao ``RadioManager``), sem painel de skin, com
``keep_connected`` e a fila dando a volta (``loop = "queue"``). A faixa virtual é a lista de faixas da playlist do servidor girando no
tempo desde que a rádio ligou (``rules.radio.virtual_position``): ao voltar de uma pausa a fila é rotacionada para a faixa e a posição
certas. O texto externo (título, nome de playlist) nunca é publicado por aqui.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Sequence
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Optional

import disnake

from empiremuse.rules.radio import Radio
from empiremuse.utils.radio.chat import PermissionLost
from empiremuse.utils.radio.runner import Allowed

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore, BotPool

logger = logging.getLogger(__name__)


class RadioBotUnavailable(Exception):
    """O bot da rádio (ou o servidor dele) ainda não está pronto: o runner trata como "não sei agora" e tenta de novo."""


class DiscordRadioPorts:
    def __init__(self, pool: BotPool, radio: Radio) -> None:
        self.pool = pool
        self.radio = radio
        self._tracks: list[Any] = []

    # ------------------------------------------------------------------ o mundo

    def _bot(self) -> Optional[BotCore]:
        return next(
            (b for b in self.pool.get_all_bots() if b.user is not None and b.user.id == self.radio.bot_id), None
        )

    def _guild(self) -> Optional[disnake.Guild]:
        bot = self._bot()
        return bot.get_guild(self.radio.guild_id) if bot is not None else None

    def _channel(self) -> Any:
        guild = self._guild()
        return guild.get_channel(self.radio.channel_id) if guild is not None else None

    def _player(self) -> Any:
        bot = self._bot()
        return bot.music.players.get(self.radio.guild_id) if bot is not None else None

    # ------------------------------------------------------------------ RadioPorts

    def now(self) -> float:
        return time.monotonic()

    def wall(self) -> float:
        return time.time()

    def clock_origin(self) -> float | None:
        """A época gravada em que a programação virtual começou (sobrevive ao reinício do bot)."""
        return self.radio.clock_started_at

    def jitter(self) -> float:
        return random.random()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def allowed(self) -> Allowed:
        """R-1: o premium (cache de 30 s) e o canal/permissão de agora. Falha do banco propaga e o runner trata como "não sei"."""
        bot, guild = self._bot(), self._guild()
        if bot is None or not getattr(bot, "bot_ready", True) or guild is None:
            # na subida o bot reserva (ou o servidor) ainda não está no cache: "não sei agora", NUNCA "canal apagado"
            raise RadioBotUnavailable
        if await self.pool.radio.premium.get(self.radio.guild_id) is None:
            return Allowed.NO_PREMIUM
        stored = await self.pool.radio.store.get(self.radio.id)
        if stored is None or not stored.enabled:
            return Allowed.DISABLED  # desligada por fora (comando, painel, revogação): este runner não pode seguir
        channel = self._channel()
        if channel is None:
            return Allowed.NO_CHANNEL
        permissions = channel.permissions_for(guild.me)
        if not (permissions.view_channel and permissions.connect):
            return Allowed.NO_PERMISSION
        return Allowed.YES

    async def connect(self) -> bool:
        """Entra (ou volta) na call; no palco pede a palavra UMA vez por conexão. Qualquer falha é ``False`` e o runner recua."""
        bot, guild, channel = self._bot(), self._guild(), self._channel()
        if bot is None or guild is None or channel is None:
            return False
        try:
            player = self._player() or await self._create_player(bot, guild, channel)
            await player.connect(channel.id, self_deaf=True)
            if isinstance(channel, disnake.StageChannel):
                await self._speak(guild, channel, player)
        except Exception as error:  # noqa: BLE001 - só o tipo vai ao log (R-12)
            logger.warning("Rádio não conectou (%s)", type(error).__name__)
            return False
        return True

    async def _create_player(self, bot: BotCore, guild: disnake.Guild, channel: Any) -> Any:
        from empiremuse.utils.music.models import LavalinkPlayer  # tarde: modelos importam o cliente

        node = await self._pick_node(bot)
        player = bot.music.get_player(
            guild_id=guild.id,
            cls=LavalinkPlayer,
            node_id=node.identifier,
            guild=guild,
            channel=channel,
            player_creator=bot.user.id,
            static=False,
            keep_connected=True,
        )
        player.radio_id = self.radio.id
        player.controller_mode = False
        self._configure_player(player)
        player.tr = await bot.pool.i18n.for_guild(guild)
        return player

    async def _pick_node(self, bot: BotCore) -> Any:
        return await bot.get_cog("Music").get_best_node(bot)

    def _configure_player(self, player: Any) -> None:
        player.loop = "queue"  # a playlist dá a volta; a ao vivo troca isto (``LiveRadioPorts``)

    async def _speak(self, guild: disnake.Guild, channel: disnake.StageChannel, player: Any) -> None:
        """Palco: ``suppress=False`` com ``MUTE_MEMBERS``; sem isso, ``request_to_speak``. Um 403 vale UMA tentativa por conexão (R-5)."""
        try:
            if channel.permissions_for(guild.me).mute_members:
                await guild.me.edit(suppress=False)
            else:
                await guild.me.request_to_speak()
        except disnake.Forbidden:
            logger.warning("Rádio sem permissão para falar no palco")

    async def _load_tracks(self) -> list[Any]:
        if self._tracks:
            return self._tracks
        bot, guild = self._bot(), self._guild()
        playlist = await self.pool.servers.playlists.find(self.radio.guild_id, self.radio.source.get("playlist", ""))
        if bot is None or guild is None or playlist is None:
            return []
        music = bot.get_cog("Music")
        shim = SimpleNamespace(guild=guild, guild_id=guild.id, author=guild.me, bot=bot, channel=self._channel())
        player = self._player()
        # source=False: a URL exata, sem "ytsearch:" nem Deezer/Spotify; no nó do player quando há (como o controlador, controller_embed.py)
        found, _ = await music.get_tracks(
            playlist.url, shim, guild.me, source=False, node=getattr(player, "node", None), bot=bot
        )
        self._tracks = list(getattr(found, "tracks", found) or [])
        return self._tracks

    async def prepare(self) -> None:
        if not await self._load_tracks():
            raise RuntimeError("playlist sem faixas")

    async def play_virtual(self, index: int, position_ms: int) -> None:
        player = self._player()
        tracks = await self._load_tracks()
        if player is None or not tracks:
            raise RuntimeError("sem player ou sem faixas")
        player.queue.clear()
        player.queue.extend(tracks[index:] + tracks[:index])
        await player.process_next(start_position=position_ms)

    async def skip_track(self) -> None:
        """Botão "próxima": a fila da rádio dá a volta (``loop = "queue"``), então o próximo da fila toca; o relógio virtual segue."""
        player = self._player()
        if player is not None:
            await player.process_next()

    async def _panel_name(self, current: Radio) -> object:
        return current.source.get("playlist", "")

    def _panel_view(self) -> Any:
        return None  # a ao vivo mostra título, reserva no ar e fora do ar (``LiveRadioPorts``)

    async def ensure_panel(self) -> None:
        """Garante a mensagem de controle no chat da call: mantém a que ainda existe, manda uma nova se sumiu. Sem permissão de enviar: só avisa."""
        bot, guild, channel = self._bot(), self._guild(), self._channel()
        hub = self.pool.radio
        current = await hub.store.get(self.radio.id)
        if bot is None or guild is None or channel is None or current is None:
            return
        if current.panel_message_id:
            try:
                existing = await channel.fetch_message(current.panel_message_id)
                hub.manager.note_panel(self.radio.id, existing.id)
                return
            except disnake.NotFound:
                pass  # sumiu: manda outra
            except disnake.Forbidden:
                logger.warning("Painel da rádio sem permissão para ler o chat da call")
                return
        from empiremuse.utils.radio.panel import panel_payload  # tarde: o painel importa a camada de texto

        tr = await bot.pool.i18n.for_guild(guild)
        payload = panel_payload(
            tr,
            playlist_name=await self._panel_name(current),
            color=bot.get_color(guild.me),
            live=self.radio.kind == "live",
            view=self._panel_view(),
        )
        try:
            message = await channel.send(allowed_mentions=disnake.AllowedMentions.none(), **payload)
        except disnake.Forbidden:
            logger.warning("Painel da rádio sem permissão para enviar no chat da call")
            return
        await hub.store.set_panel_message(self.radio.id, message.id)
        hub.manager.note_panel(self.radio.id, message.id)

    async def stop_stream(self) -> None:
        player = self._player()
        if player is None:
            return
        try:
            player.queue.clear()
            await player.stop()
        except Exception as error:  # noqa: BLE001
            logger.warning("Rádio sem fechar o fluxo (%s)", type(error).__name__)

    def listeners(self) -> int:
        channel = self._channel()
        members = getattr(channel, "members", None) or []
        return sum(1 for m in members if not m.bot and not (m.voice.deaf or m.voice.self_deaf))

    def paused_all(self) -> bool:
        return self.pool.radio.control.paused_all

    async def turn_off(self, reason: Allowed) -> None:
        """Perdeu o direito: a rádio vira DESLIGADA (a transição do contador é da store) e o bot sai da call."""
        hub = self.pool.radio
        try:
            await hub.store.set_enabled(self.radio.id, False, updated_by="system")
        finally:
            await hub.manager.deactivate(self.radio.id)
            player = self._player()
            if player is not None:
                try:
                    await player.destroy()
                except Exception as error:  # noqa: BLE001
                    logger.warning("Rádio sem sair da call (%s)", type(error).__name__)

    def durations_ms(self) -> list[int]:
        return [int(getattr(t, "duration", 0) or 0) for t in self._tracks]


class DiscordChatPorts:
    """O chat da call de UMA rádio para o ``ChatSweeper``: amarrado ao canal da rádio (nunca ao da Jukebox), 403 vira ``PermissionLost``."""

    def __init__(self, pool: BotPool, radio: Radio) -> None:
        self.pool = pool
        self.radio = radio

    def _channel(self) -> Any:
        bot = next((b for b in self.pool.get_all_bots() if b.user is not None and b.user.id == self.radio.bot_id), None)
        guild = bot.get_guild(self.radio.guild_id) if bot is not None else None
        return guild.get_channel(self.radio.channel_id) if guild is not None else None

    @property
    def stage(self) -> bool:
        return isinstance(self._channel(), disnake.StageChannel)

    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def fetch_recent(self, limit: int) -> list[tuple[int, float, bool]]:
        """``(id, criada_em, protegida)``. Protegidas: a fixada, o painel da rádio, o que qualquer bot do pool escreveu e mensagem de sistema."""
        channel = self._channel()
        if channel is None:
            return []
        current = await self.pool.radio.store.get(self.radio.id)  # o id do painel pode ter mudado depois da ativação
        panel_id = current.panel_message_id if current is not None else self.radio.panel_message_id
        pool_bots = {
            b.user.id for b in self.pool.get_all_bots() if b.user
        }  # o painel da Jukebox e os avisos de outros bots do pool ficam
        try:
            messages = [m async for m in channel.history(limit=limit)]
        except disnake.Forbidden as error:
            raise PermissionLost from error
        return [
            (
                m.id,
                m.created_at.timestamp(),
                bool(
                    m.pinned
                    or m.id == panel_id
                    or m.author.id == self.radio.bot_id
                    or m.author.id in pool_bots
                    or m.is_system()
                ),
            )
            for m in messages
        ]

    async def bulk_delete(self, ids: Sequence[int]) -> None:
        channel = self._channel()
        if channel is None or not ids:
            return
        try:
            await channel.delete_messages([disnake.Object(id=i) for i in ids])
        except disnake.Forbidden as error:
            raise PermissionLost from error
