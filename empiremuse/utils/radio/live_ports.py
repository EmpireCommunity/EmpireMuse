"""As portas reais da rádio AO VIVO (item 8, R3): as de Discord e Lavalink da R1, trocando só a fonte.

O Lavalink nunca recebe a URL da estação: cada play pede um token ao relay (``utils/radio/relay.py``) e manda tocar ``http://127.0.0.1:<porta>/r/<token>``.
A estação vem do diretório pelo uuid a cada play (``fresh_for_play`` registra o clique e confere a URL de novo, R-7). O título do fluxo chega do relay
já limpo e vai ao ``RadioManager``. Nada de texto externo (nome, título) é publicado por aqui sem a função única da R-11.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Optional

from empiremuse.rules.radio import Radio
from empiremuse.utils.radio.discord_ports import DiscordRadioPorts

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool

logger = logging.getLogger(__name__)


class LiveRadioPorts(DiscordRadioPorts):
    def __init__(self, pool: BotPool, radio: Radio) -> None:
        super().__init__(pool, radio)
        self.primary: str = radio.source["station"]
        self.reserve: Optional[str] = radio.source.get("reserve")
        self.active: str = self.primary
        self._token: Optional[str] = None
        self._names: dict[str, str] = {}

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
        station = await hub.directory.fresh_for_play(self.active)
        if station is None:
            raise RuntimeError("estação indisponível")
        self._names[self.active] = station.name
        bot, guild, player = self._bot(), self._guild(), self._player()
        if bot is None or guild is None or player is None:
            raise RuntimeError("sem player")
        await hub.relay.ensure_started()
        token = hub.relay.register(
            station.url_resolved, codec=station.codec, bitrate=station.bitrate, on_title=self._on_title
        )
        self._token = token
        try:
            music = bot.get_cog("Music")
            shim = SimpleNamespace(guild=guild, guild_id=guild.id, author=guild.me, bot=bot, channel=self._channel())
            found, _ = await music.get_tracks(hub.relay.local_url(token), shim, guild.me, bot=bot)
            tracks = list(getattr(found, "tracks", found) or [])
            if not tracks:
                raise RuntimeError("o fluxo não abriu")
            player.queue.clear()
            player.queue.append(tracks[0])
            await player.process_next()
        except BaseException:
            await self._release()
            raise

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
        other = self.reserve if self.active == self.primary else self.primary
        health = self.pool.radio.health
        if other is None or health.unavailable(other):
            return False
        self.active = other
        return True

    async def use_primary(self) -> bool:
        """O health check reabilitou a principal: volta para ela (a reserva só vale enquanto a principal estiver fora)."""
        if self.pool.radio.health.unavailable(self.primary):
            return False
        self.active = self.primary
        return True

    def durations_ms(self) -> list[int]:
        return []
