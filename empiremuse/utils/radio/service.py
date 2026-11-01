"""O que está rodando agora (item 8, R1): o estado em memória das rádios do processo.

``state(radio_id)`` é síncrono e devolve ``None`` para rádio desconhecida ou desligada neste processo; é o que a página "Rádios" mostra.
O título ao vivo (R3) já chega pela função única de texto externo (R-11): quem mostra só escapa.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool


@dataclass(frozen=True)
class RadioState:
    playing: bool = False
    paused_by_owner: bool = False
    on_reserve: bool = False  # tocando a estação reserva (R3)
    offline: bool = False  # o bot não está na call (reconectando) ou a fonte caiu
    title: Optional[str] = None


class RadioService:
    def __init__(self, pool: BotPool) -> None:
        self.pool = pool
        self._states: dict[str, RadioState] = {}

    def state(self, radio_id: str) -> Optional[RadioState]:
        return self._states.get(radio_id)

    def publish(self, radio_id: str, state: Optional[RadioState]) -> None:
        """Quem roda a rádio atualiza o estado; ``None`` esquece a rádio (desligada)."""
        if state is None:
            self._states.pop(radio_id, None)
        else:
            self._states[radio_id] = state
