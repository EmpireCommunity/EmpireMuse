"""As portas reais da rádio AO VIVO (item 8, R3): as de Discord e Lavalink da R1, trocando só a fonte.

O Lavalink nunca recebe a URL da estação: cada play pede um token ao relay (``utils/radio/relay.py``) e manda tocar ``http://127.0.0.1:<porta>/r/<token>``.
A estação vem do diretório pelo uuid a cada play (``fresh_for_play`` registra o clique e confere a URL de novo, R-7). O título do fluxo chega do relay
já limpo e vai ao ``RadioManager``. Nada de texto externo (nome, título) é publicado por aqui sem a função única da R-11.
"""

from __future__ import annotations

import asyncio
import logging

import disnake
from typing import TYPE_CHECKING, Any, Optional

from empiremuse.rules.radio import Radio
from empiremuse.utils.radio.directory import DirectoryError
from empiremuse.utils.radio.discord_ports import DiscordRadioPorts
from empiremuse.utils.radio.live import StationFailure
from empiremuse.utils.radio.panel import LiveView, panel_payload

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool

logger = logging.getLogger(__name__)

PANEL_EDIT_INTERVAL_S = 15.0


class LiveRadioPorts(DiscordRadioPorts):
    def __init__(self, pool: BotPool, radio: Radio) -> None:
        super().__init__(pool, radio)
        self.primary: str = radio.source["station"]
        self.reserve: Optional[str] = radio.source.get("reserve")
        self.active: str = self.primary
        self._token: Optional[str] = None
        self._names: dict[str, str] = {}
        self._view: LiveView = LiveView()  # o que o painel deve mostrar (o último pedido)
        self._shown: Optional[LiveView] = None  # o que o painel mostra de fato
        self._last_edit: Optional[float] = None
        self._panel_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------ estado da fonte

    def on_reserve(self) -> bool:
        return self.active != self.primary

    def _configure_player(self, player: Any) -> None:
        player.loop = False  # um fluxo só: quando acaba, quem decide é o runner (recuo), nunca a fila
        player.radio_live = True

    async def _panel_name(self, current: Radio) -> object:
        known = self._names.get(self.active)
        if known is not None:
            return known
        station = await self.pool.radio.directory.by_uuid(self.active)
        return station.name if station is not None else "—"

    # ------------------------------------------------------------------ RadioPorts (fonte ao vivo)

    async def prepare(self) -> None:
        return

    async def play_live(self) -> None:
        """Abre a estação ativa pelo relay e manda o Lavalink tocá-la. Qualquer falha propaga e o runner recua."""
        hub = self.pool.radio
        await self._release()
        try:
            station = await hub.directory.fresh_for_play(self.active)
        except DirectoryError as error:  # diretório fora do ar ou sem fichas: não é culpa da estação
            raise RuntimeError("diretório indisponível") from error
        if station is None:
            raise StationFailure  # o diretório respondeu e a estação não existe mais ou deixou de passar nas regras
        self._names[self.active] = station.name
        bot, guild, player = self._bot(), self._guild(), self._player()
        if bot is None or guild is None or player is None:
            raise RuntimeError("sem player")
        from empiremuse.utils.music.models import (
            LavalinkPlaylist,
            LavalinkTrack,
        )  # tarde: os modelos importam o cliente

        self._local_node(bot)  # sem nó local nada é aberto (e isso não é falha da estação)
        await hub.relay.ensure_started()
        token = hub.relay.register(
            station.url_resolved, codec=station.codec, bitrate=station.bitrate, on_title=self._on_title
        )
        self._token = token
        try:
            # SÓ o nó local e a URL exata (R-8): o get_tracks genérico trocaria a consulta por "ytsearch:<url>" e, sem resultado, a mandaria a
            # Deezer, Spotify e aos outros nós, vazando o endereço e o token e tocando outra coisa no lugar do fluxo
            found = await self._local_node(
                bot
            ).get_tracks(
                hub.relay.local_url(token),
                track_cls=LavalinkTrack,
                playlist_cls=LavalinkPlaylist,
                requester=bot.user.id,
                trusted_local=True,  # só aqui: o filtro de IP do nó recusaria o 127.0.0.1 do relay (que é nosso, com token) e a faixa tocaria sem conferência
            )
            tracks = list(getattr(found, "tracks", found) or [])
            if not tracks:
                raise RuntimeError("o fluxo não abriu")
            track = tracks[0]
            track.info["uri"] = (
                ""  # o endereço local (com o token) só existe para o Lavalink: nenhuma tela, comando ou RPC o mostra
            )
            track.info["title"] = station.name
            player.queue.clear()
            player.queue.append(track)
            await player.process_next()
        except BaseException as error:
            station_fault = hub.relay.touched(token) and not isinstance(error, asyncio.CancelledError)
            await self._release()
            if station_fault and not isinstance(error, StationFailure):
                raise StationFailure from error  # o Lavalink chegou ao relay e o fluxo não abriu: a estação falhou
            raise

    @staticmethod
    def _local_node(bot: Any) -> Any:
        node = bot.music.nodes.get("LOCAL")
        if node is None:
            raise RuntimeError("sem nó local")  # o relay só atende o Lavalink deste host
        return node

    async def _pick_node(self, bot: Any) -> Any:
        return self._local_node(bot)

    # ------------------------------------------------------------------ painel da call (título, reserva no ar, fora do ar)

    def _panel_view(self) -> LiveView:
        """Um painel recém-criado já nasce com o estado de agora (e conta como mostrado)."""
        self._shown = self._view
        return self._view

    def update_panel(self, view: LiveView) -> None:
        """Pede a edição do painel. No máximo UMA edição a cada ``PANEL_EDIT_INTERVAL_S``: mudanças dentro da janela se juntam numa tarefa só,
        que edita uma vez com o valor mais recente (título a cada 100 ms = 1 edição por 15 s)."""
        self._view = view
        if view == self._shown or (self._panel_task is not None and not self._panel_task.done()):
            return
        self._panel_task = asyncio.get_running_loop().create_task(self._edit_when_due())

    async def _edit_when_due(self) -> None:
        if self._last_edit is not None:
            wait = self._last_edit + PANEL_EDIT_INTERVAL_S - self.now()
            if wait > 0:
                await self.sleep(wait)
        view, self._shown, self._last_edit = self._view, self._view, self.now()
        try:
            await self._edit_panel(view)
        except Exception as error:  # noqa: BLE001 - o painel é conveniência: só o tipo vai ao log (R-12)
            logger.warning("Painel da rádio ao vivo não editado (%s)", type(error).__name__)

    async def _edit_panel(self, view: LiveView) -> None:
        bot, guild, channel = self._bot(), self._guild(), self._channel()
        current = await self.pool.radio.store.get(self.radio.id)
        if bot is None or guild is None or channel is None or current is None or not current.panel_message_id:
            return
        try:
            message = await channel.fetch_message(current.panel_message_id)
        except disnake.NotFound, disnake.Forbidden:
            return  # o painel sumiu: o varredor o recria e a próxima edição o encontra
        tr = await bot.pool.i18n.for_guild(guild)
        payload = panel_payload(
            tr, playlist_name=await self._panel_name(current), color=bot.get_color(guild.me), live=True, view=view
        )
        await message.edit(allowed_mentions=disnake.AllowedMentions.none(), **payload)

    async def _release(self) -> None:
        token, self._token = self._token, None
        if token is not None:
            await self.pool.radio.relay.revoke(token)

    async def stop_stream(self) -> None:
        await self._release()
        await super().stop_stream()

    def _on_title(self, title: str) -> None:
        self.pool.radio.manager.note_title(self.radio.id, title)

    async def cycle_exhausted(self) -> None:
        """Um ciclo de cinco tentativas acabou sem tocar a estação ativa (R-10)."""
        health = self.pool.radio.health
        if await health.record_exhausted(self.active):
            logger.warning(
                "Estação marcada como indisponível"
            )  # sem uuid nem nome: o log fica sem texto de terceiros (R-12)

    async def switch_source(self) -> bool:
        """Principal -> reserva ou reserva -> principal, só se a outra existe e está saudável. ``True`` se trocou."""
        await self._reload_reserve()
        other = self.reserve if self.active == self.primary else self.primary
        health = self.pool.radio.health
        if other is None or health.unavailable(other):
            return False
        self.active = other
        return True

    async def _reload_reserve(self) -> None:
        """A reserva pode ter mudado pelo seletor da página Rádios (``RadioStore.set_reserve``): relê o documento; banco fora mantém a que já se conhece."""
        try:
            current = await self.pool.radio.store.get(self.radio.id)
        except Exception as error:  # noqa: BLE001 - só o tipo vai ao log (R-12)
            logger.warning("Reserva da rádio não relida (%s)", type(error).__name__)
            return
        if current is not None and current.kind == "live":
            self.reserve = current.source.get("reserve")
            if self.active == self.reserve:  # a reserva nova é a estação em que ela já está: nada a trocar
                self.reserve = None

    async def use_primary(self) -> bool:
        """O health check reabilitou a principal: volta para ela (a reserva só vale enquanto a principal estiver fora)."""
        if self.pool.radio.health.unavailable(self.primary):
            return False
        self.active = self.primary
        return True

    def durations_ms(self) -> list[int]:
        return []
