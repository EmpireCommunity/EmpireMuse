"""Rádio 24/7 (item 8, R1): o ``pool.radio`` reúne a store de rádios, o premium, os controles do dono e o estado em memória."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cachetools import TTLCache

from empiremuse.utils.radio.control import BotRoles, RadioControl
from empiremuse.utils.radio.directory import Directory
from empiremuse.utils.radio.health import HealthChecker, StationHealth
from empiremuse.utils.radio.manager import RadioManager
from empiremuse.utils.radio.premium import PremiumStore
from empiremuse.utils.radio.relay import RadioRelay
from empiremuse.utils.radio.service import RadioService
from empiremuse.utils.radio.store import RadioStore

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool


NOTICE_SECONDS = 60


class RadioHub:
    """``pool.radio.store`` (rádios), ``.premium`` (concessões e contador) e ``.service`` (o que roda agora)."""

    def __init__(self, pool: BotPool) -> None:
        self.premium = PremiumStore(pool)
        self.control = RadioControl(pool)  # "pausar todas" (dono)
        self.roles = BotRoles(pool)  # bots só-rádio (dono)
        self.store = RadioStore(pool, self.premium)
        self.service = RadioService(pool)
        self.manager = RadioManager(self, self.service)  # quem faz as rádios rodarem neste processo
        self.directory = Directory()  # Radio Browser (R3): busca, uuid -> estação, limites e cache
        self.relay = RadioRelay()  # o único endereço que o Lavalink recebe: http://127.0.0.1:<porta>/r/<token>
        self.health = StationHealth(pool)  # estações indisponíveis (R-10)
        self.health_checker = HealthChecker(
            self,
            self._probe_station,
            referenced=self.manager.referenced_stations,
            recovered=self.manager.station_recovered,
        )
        self._notices: TTLCache = TTLCache(maxsize=512, ttl=NOTICE_SECONDS)

    async def _probe_station(self, station: Any) -> bool:
        return await self.relay.probe(station.url_resolved, station.codec)

    def claim_notice(self, key: object) -> bool:
        """Um aviso por ``key`` (canal) a cada ``NOTICE_SECONDS``: ``True`` só para quem chegou primeiro na janela."""
        if key in self._notices:
            return False
        self._notices[key] = True
        return True
