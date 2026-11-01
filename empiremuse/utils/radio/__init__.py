"""Rádio 24/7 (item 8, R1): o ``pool.radio`` reúne a store de rádios, o premium, os controles do dono e o estado em memória."""

from __future__ import annotations

from typing import TYPE_CHECKING

from empiremuse.utils.radio.control import BotRoles, RadioControl
from empiremuse.utils.radio.manager import RadioManager
from empiremuse.utils.radio.premium import PremiumStore
from empiremuse.utils.radio.service import RadioService
from empiremuse.utils.radio.store import RadioStore

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool


class RadioHub:
    """``pool.radio.store`` (rádios), ``.premium`` (concessões e contador) e ``.service`` (o que roda agora)."""

    def __init__(self, pool: BotPool) -> None:
        self.premium = PremiumStore(pool)
        self.control = RadioControl(pool)  # "pausar todas" (dono)
        self.roles = BotRoles(pool)  # bots só-rádio (dono)
        self.store = RadioStore(pool, self.premium)
        self.service = RadioService(pool)
        self.manager = RadioManager(self, self.service)  # quem faz as rádios rodarem neste processo
