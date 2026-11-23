"""O estado de render de cada player, fora de ``player.py``: um registro fraco (``WeakKeyDictionary``) por player.

O valor é imutável (``PlayerRenderState``); cada mudança troca a entrada inteira. O player sumiu, a entrada some junto.
Só memória: nada daqui vai ao banco, ao ``player_resume``, ao RPC nem ao log (a chave guarda o ``CardSpec``, com a URL da capa).
"""

from __future__ import annotations

import weakref
from typing import Any, NamedTuple

from empiremuse.rules.card import RenderedKey


class PlayerRenderState(NamedTuple):
    last: RenderedKey | None = None  # a chave do anexo que está na mensagem
    upgrade_due: bool = False  # o GIF (ou a capa) foi adiado: o próximo ciclo do updater redesenha
    last_cold_gif_at: float | None = None  # relógio monotônico do último GIF frio deste player


EMPTY = PlayerRenderState()


class RenderStates:
    def __init__(self) -> None:
        self._states: weakref.WeakKeyDictionary[Any, PlayerRenderState] = weakref.WeakKeyDictionary()

    def get(self, player: Any) -> PlayerRenderState:
        return self._states.get(player, EMPTY)

    def replace(self, player: Any, **changes: Any) -> PlayerRenderState:
        state = self.get(player)._replace(**changes)
        self._states[player] = state
        return state

    def forget(self, player: Any) -> None:
        """A mensagem do player foi trocada (nova mensagem, skin de texto): o anexo antigo não vale mais."""
        self._states.pop(player, None)

    def __len__(self) -> int:
        return len(self._states)


RENDER_STATES = RenderStates()  # um registro por processo (players dos 5 bots)
