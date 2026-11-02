"""As portas reais da rádio PERSONALIZADA por estação (item 8, R5): Discord e Lavalink de UM bot reserva sorteando faixas de uma estação do servidor.

Herda o que a R1 já faz (conectar, painel, chat, desligar) e troca a fonte: em vez de uma playlist girando num relógio virtual, cada faixa é sorteada do pool da
estação (``custom_source``), resolvida como ``PartialTrack`` pelo MESMO caminho do autoplay e deixada na fila do player. A faixa seguinte já espera resolvida
enquanto a atual toca. O player de rádio nunca puxa autoplay (``playback_steps``): fila vazia é com o ``tick`` do runner, que sorteia de novo.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Sequence
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Optional

import disnake

from empiremuse.rules.radio import Radio
from empiremuse.rules.radio_shuffle import Track
from empiremuse.rules.radio_station import KIND_ARTISTS, KIND_GENRE, CustomStation
from empiremuse.utils.radio.custom_source import CustomSession, load_pool
from empiremuse.utils.radio.discord_ports import DiscordRadioPorts
from empiremuse.utils.radio.panel import CustomView

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool

logger = logging.getLogger(__name__)

SOURCE_NAME = "last.fm"  # o ``PartialTrack`` por metadados: o nó o resolve pelos provedores de busca, como o autoplay


class CustomRadioPorts(DiscordRadioPorts):
    def __init__(self, pool: BotPool, radio: Radio) -> None:
        super().__init__(pool, radio)
        self.station: Optional[CustomStation] = None
        self.session: Optional[CustomSession] = None
        self.unknown: tuple[str, ...] = ()
        self.current: Optional[Track] = None
        self.no_tracks = False
        self._stocking = asyncio.Lock()
        self._panel_task: Optional[asyncio.Task] = None

    def _configure_player(self, player: Any) -> None:
        player.loop = False  # cada faixa é sorteada pelo runner; a fila nunca dá a volta
        player.radio_custom = True

    # ------------------------------------------------------------------ a estação e o pool

    async def prepare(self) -> None:
        """Lê a estação (ela pode ter sido editada ou apagada), monta o pool e zera o sorteio. ``EmptyStation`` se não há faixa; Last.fm fora sobe como erro
        de quem chama (o runner recua)."""
        hub = self.pool.radio
        station = await hub.stations.get(self.radio.guild_id, self.radio.source.get("station"))
        if station is None:
            raise RuntimeError("estação não existe mais")
        loaded = await load_pool(hub, station, self._playlist_tracks)
        self.station = station
        self.unknown = loaded.unknown
        self.session = CustomSession(loaded.pool, hub.resolved, random.Random())
        if loaded.unknown != station.unknown and station.kind == KIND_ARTISTS:
            await hub.stations.set_unknown(station.guild_id, station.id, loaded.unknown)
        self.no_tracks = False

    async def _playlist_tracks(self, names: Sequence[str]) -> list[Track]:
        bot, guild = self._bot(), self._guild()
        if bot is None or guild is None:
            return []
        music = bot.get_cog("Music")
        shim = SimpleNamespace(guild=guild, guild_id=guild.id, author=guild.me, bot=bot, channel=self._channel())
        player = self._player()
        found: list[Track] = []
        for name in names:
            playlist = await self.pool.servers.playlists.find(self.radio.guild_id, name)
            if playlist is None:
                continue
            # source=False: a URL exata, sem "ytsearch:" nem Deezer/Spotify (como o controlador); no nó do player quando há
            result, _ = await music.get_tracks(
                playlist.url, shim, guild.me, source=False, node=getattr(player, "node", None), bot=bot
            )
            for track in list(getattr(result, "tracks", result) or []):
                author, title = str(getattr(track, "author", "") or ""), str(getattr(track, "title", "") or "")
                if author.strip() and title.strip():
                    found.append(Track(author, title))
        return found

    # ------------------------------------------------------------------ tocar

    async def _resolve(self, track: Track) -> Any | None:
        """A faixa como ``PartialTrack`` pronta para tocar, ou ``None`` se nenhum stream tocável. Primeiro o cache de faixas resolvidas (7 dias, vale depois
        de um reinício); só sem ele o nó busca (mesmo caminho do autoplay) e o resultado é guardado."""
        from empiremuse.utils.music.models import PartialTrack  # tarde: os modelos importam o cliente

        player = self._player()
        if player is None:
            raise RuntimeError("sem player")
        cached = await self._from_cache(player, track)
        if cached is not None:
            return cached
        partial = PartialTrack(
            title=track.title, author=track.artist, source_name=SOURCE_NAME, requester=self.radio.bot_id
        )
        try:
            await player.resolve_track(partial)
        except Exception as error:  # noqa: BLE001 - uma faixa que não resolve é só a próxima sorteada: só o tipo vai ao log (R-12)
            logger.warning("Faixa da rádio não resolvida (%s)", type(error).__name__)
            return None
        if not partial.id:
            return None
        await self.pool.radio.resolved.store(track, partial)
        return partial

    async def _from_cache(self, player: Any, track: Track) -> Any | None:
        """A faixa do cache de resolvidas, se o nó ainda decodifica o ``encoded`` guardado; senão a entrada é apagada e a busca segue."""
        from empiremuse.utils.music.models import PartialTrack

        cache = self.pool.radio.resolved
        entry = await cache.get(track)
        if entry is None:
            return None
        try:
            await player.node.build_track(entry["encoded"])
        except Exception as error:  # noqa: BLE001 - só o tipo vai ao log (R-12)
            logger.info("Faixa guardada sem decodificar (%s)", type(error).__name__)
            await cache.forget(track)
            return None
        partial = PartialTrack(
            title=track.title,
            author=track.artist,
            uri=str(entry.get("uri") or ""),
            duration=int(entry.get("length_ms") or 0),
            source_name=str(entry.get("source") or SOURCE_NAME),
            requester=self.radio.bot_id,
        )
        partial.id = partial.info["id"] = entry["encoded"]
        return partial

    async def play_next(self) -> None:
        """Sorteia, resolve e toca uma faixa NOVA agora (ligar, retomar, trocar de estação). Depois adianta a seguinte."""
        player = self._player()
        if player is None or self.session is None:
            raise RuntimeError("sem player ou sem estação")
        try:
            track, partial = await self.session.next_playable(self._resolve)
        except Exception:
            self.no_tracks = True
            self._refresh_panel()
            raise
        self.no_tracks = False
        player.queue.clear()
        player.queue.append(partial)
        self.current = track
        await player.process_next()
        self._refresh_panel()
        await self._stock()

    async def _stock(self) -> None:
        """Deixa a próxima faixa resolvida na fila (uma por vez); sem estação ou com a fila cheia não faz nada."""
        player = self._player()
        if player is None or self.session is None or len(player.queue) >= 1:
            return
        async with self._stocking:
            if len(player.queue) >= 1:
                return
            async with (
                self.pool.radio.stock_slots
            ):  # no máximo 2 adiantados ao mesmo tempo em todas as rádios do processo
                _, partial = await self.session.next_playable(self._resolve)
            player.queue.append(partial)

    async def keep_playing(self) -> None:
        """Chamado a cada passo do runner enquanto toca: a fila seca ou o player parado (faixa acabou sem próxima) volta a tocar."""
        player = self._player()
        if player is None or self.session is None:
            return
        if getattr(player, "current", None) is None and not player.queue:
            await self.play_next()
        else:
            await self._stock()

    async def skip_track(self) -> None:
        """Botão "pular faixa": passa para a que já espera na fila (ou sorteia agora); o runner já limitou a 1 a cada 5 s."""
        player = self._player()
        if player is None or self.session is None:
            return
        if not player.queue:
            await self.play_next()
            return
        await player.process_next()
        self._refresh_panel()
        await self._stock()

    async def apply_station(self, radio: Radio) -> None:
        """A estação da rádio mudou (``set_custom_station``): descarta o sorteio velho; a próxima partida relê a estação."""
        self.radio = radio
        self.station, self.session, self.current, self.no_tracks = None, None, None, False
        self._refresh_panel()

    def durations_ms(self) -> list[int]:
        return []  # sem relógio virtual: a faixa é sorteada, não calculada pelo tempo

    async def play_virtual(self, index: int, position_ms: int) -> None:
        await (
            self.play_next()
        )  # o runner de estação nunca chama isto; se algo chamar, o resultado é o mesmo: uma faixa nova

    # ------------------------------------------------------------------ painel

    async def _panel_name(self, current: Radio) -> object:
        return self.station.name if self.station is not None else "—"

    async def _custom_view(self) -> CustomView:
        station = self.station
        stations = await self.pool.radio.stations.list_for_guild(self.radio.guild_id) if station else []
        return CustomView(
            station=station.name if station else "—",
            artist=self.current.artist if self.current and not self.no_tracks else "",
            title=self.current.title if self.current and not self.no_tracks else "",
            no_tracks=self.no_tracks,
            has_list=len(stations) >= 2,
            credit=bool(station and station.kind in (KIND_GENRE, KIND_ARTISTS)),
            unknown=self.unknown,
        )

    def _refresh_panel(self) -> None:
        """Refaz o painel (faixa no ar, sem faixas) em segundo plano; no máximo uma edição por vez."""
        if self._panel_task is not None and not self._panel_task.done():
            return
        self._panel_task = asyncio.get_running_loop().create_task(self._edit_panel_now())

    async def _edit_panel_now(self) -> None:
        bot, guild, channel = self._bot(), self._guild(), self._channel()
        current = await self.pool.radio.store.get(self.radio.id)
        if bot is None or guild is None or channel is None or current is None or not current.panel_message_id:
            return
        try:
            message = await channel.fetch_message(current.panel_message_id)
            payload = await self._panel_payload(bot, guild, current)
            await message.edit(allowed_mentions=disnake.AllowedMentions.none(), **self.edit_kwargs(payload))
        except Exception as error:  # noqa: BLE001 - o painel é conveniência: só o tipo vai ao log (R-12)
            logger.warning("Painel da rádio personalizada não editado (%s)", type(error).__name__)
