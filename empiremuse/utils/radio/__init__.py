"""Rádio 24/7 (item 8, R1): o ``pool.radio`` reúne a store de rádios, o premium, os controles do dono e o estado em memória."""

from __future__ import annotations

import asyncio
import logging
from functools import cache
from typing import TYPE_CHECKING, Any

from cachetools import TTLCache

from empiremuse.utils.radio.control import BotRoles, RadioControl
from empiremuse.utils.radio.custom_source import STOCK_SLOTS, ResolvedTracks
from empiremuse.utils.radio.directory import Directory
from empiremuse.utils.radio.favorites import RadioFavorites
from empiremuse.utils.radio.lastfm_feed import LastFmFeed
from empiremuse.utils.radio.health import HealthChecker, StationHealth
from empiremuse.utils.radio.locations import LocationDataError, LocationTree
from empiremuse.utils.radio.manager import RadioManager
from empiremuse.utils.radio.premium import PremiumStore
from empiremuse.utils.radio.relay import RadioRelay
from empiremuse.utils.radio.service import RadioService
from empiremuse.utils.radio.stations import StationStore
from empiremuse.utils.radio.store import RadioStore

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool


NOTICE_SECONDS = 60

logger = logging.getLogger(__name__)


@cache
def load_locations() -> LocationTree | None:
    """A árvore de local fixa (R4), lida uma vez por processo; arquivo ausente ou inválido desliga o filtro e a busca por nome segue."""
    try:
        return LocationTree.load()
    except LocationDataError as error:
        logger.warning("Filtro de local das rádios desligado (%s)", error.reason)
        return None


class RadioHub:
    """``pool.radio.store`` (rádios), ``.stations`` (estações do servidor), ``.premium`` (concessões e contador) e ``.service`` (o que roda agora)."""

    def __init__(self, pool: BotPool) -> None:
        self.premium = PremiumStore(pool)
        self.control = RadioControl(pool)  # "pausar todas" (dono)
        self.roles = BotRoles(pool)  # bots só-rádio (dono)
        self.store = RadioStore(pool, self.premium)
        self.stations = StationStore(pool, self.store)  # estações do servidor para a rádio personalizada (R5)
        self.stations.on_updated = self.reload_station
        self.resolved = ResolvedTracks(
            pool.database
        )  # faixas resolvidas e sem stream, em memória e em ``global.radio_resolved`` (R5)
        self.stock_slots = asyncio.Semaphore(
            STOCK_SLOTS
        )  # adiantados (a próxima faixa) ao mesmo tempo em todas as rádios do processo
        self.lastfm = LastFmFeed(pool)  # listas de gênero e de artistas do Last.fm (R5)
        self.favorites = RadioFavorites(pool)  # estações favoritas do servidor (painel da rádio ao vivo)
        self.service = RadioService(pool)
        self.manager = RadioManager(self, self.service)  # quem faz as rádios rodarem neste processo
        self.locations = load_locations()  # Região → país → estado → cidade (R4); ``None`` = filtro desligado
        self.directory = Directory(
            state_index=self.locations.state_index if self.locations else None
        )  # Radio Browser (R3): busca, uuid -> estação, local, limites e cache
        self.relay = RadioRelay()  # o único endereço que o Lavalink recebe: http://127.0.0.1:<porta>/r/<token>
        self.health = StationHealth(pool)  # estações indisponíveis (R-10)
        self.health_checker = HealthChecker(
            self,
            self._probe_station,
            referenced=self.manager.referenced_stations,
            recovered=self.manager.station_recovered,
        )
        self._notices: TTLCache = TTLCache(maxsize=512, ttl=NOTICE_SECONDS)

    async def reload_station(self, station: Any) -> None:
        """Uma estação do servidor foi editada: cada rádio LIGADA que a toca recarrega o pool (a troca passa pelo recuo, sem prender quem editou)."""
        for radio in await self.stations.radios_using(station.guild_id, station.id):
            await self.manager.change_station(radio)

    async def _probe_station(self, station: Any) -> bool:
        return await self.relay.probe(station.url_resolved, station.codec)

    def claim_notice(self, key: object) -> bool:
        """Um aviso por ``key`` (canal) a cada ``NOTICE_SECONDS``: ``True`` só para quem chegou primeiro na janela."""
        if key in self._notices:
            return False
        self._notices[key] = True
        return True
