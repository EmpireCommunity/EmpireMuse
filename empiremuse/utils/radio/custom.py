"""O runner da rádio PERSONALIZADA por estação (item 8, R5): o mesmo ciclo da R1 (presença, pausa do dono, premium, limites do processo) com outra fonte.

Muda só o que a fonte exige:

- **Tocar** é sortear uma faixa da estação (``ports.play_next``), sem relógio virtual: por ser aleatória, não há posição para calcular, e a retomada toca uma
  faixa nova desde o começo.
- **Adiantado:** a cada passo do ``tick`` (5 s) as portas deixam a PRÓXIMA faixa já resolvida na fila do player, e retomam sozinhas se a fila secou.
- **Sem faixas tocáveis:** cinco falhas seguidas pausam a estação ("sem faixas tocáveis") e a nova tentativa vem em 10 min pelo recuo.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Optional

from empiremuse.rules.radio import backoff_s
from empiremuse.utils.radio.custom_source import RETRY_AFTER_S, EmptyStation, NoPlayableTracks
from empiremuse.utils.radio.runner import Phase, ProcessLimits, RadioPorts, RadioRunner
from empiremuse.utils.radio.service import RadioState

logger = logging.getLogger(__name__)


class CustomRadioRunner(RadioRunner):
    def __init__(
        self,
        radio_id: str,
        ports: RadioPorts,
        limits: ProcessLimits,
        *,
        publish: Callable[[RadioState | None], None],
    ) -> None:
        super().__init__(radio_id, ports, limits, publish=publish)
        self.no_tracks = False  # a estação não tem faixa tocável agora: pausada, com nova tentativa em 10 min

    def _state(self, phase: Phase) -> RadioState:
        base = super()._state(phase)
        return RadioState(
            playing=base.playing,
            paused_by_owner=base.paused_by_owner,
            on_reserve=False,
            offline=base.offline or self.no_tracks,
            title=None,
            paused_by_admin=base.paused_by_admin,
        )

    def _backoff(self) -> float:
        if self.no_tracks:
            return float(RETRY_AFTER_S)
        return backoff_s(self.attempt, self.ports.jitter())

    # ------------------------------------------------------------------ fonte

    async def _load_source(self) -> None:
        try:
            await self.ports.prepare()  # type: ignore[attr-defined]
        except EmptyStation, NoPlayableTracks:
            self.no_tracks = True
            raise

    async def _start_source(self) -> None:
        try:
            await self.ports.play_next()  # type: ignore[attr-defined]
        except NoPlayableTracks:
            self.no_tracks = True
            raise
        self.no_tracks = False

    async def _tick(self) -> None:
        await super()._tick()
        if self.phase is Phase.PLAYING and not self._stopped:
            keep: Optional[Callable] = getattr(self.ports, "keep_playing", None)
            if keep is not None:
                try:
                    await keep()
                except NoPlayableTracks:
                    self.no_tracks = True
                    self._set(Phase.PLAYING)
                except Exception as error:  # noqa: BLE001 - uma falha de adiantar não derruba a rádio: o próximo passo tenta de novo (R-12)
                    logger.warning("Rádio personalizada sem adiantar a faixa (%s)", type(error).__name__)

    async def station_changed(self) -> None:
        """A estação da rádio mudou (painel da call, comando ou página): recomeça pelo recuo; pausada ou em recuo, a próxima tentativa já usa a nova."""
        if self._stopped:
            return
        self.no_tracks = False
        if self.phase is Phase.PLAYING:
            self.attempt = 0
            await (
                self.on_disconnect()
            )  # a troca é uma volta pelo recuo, como a da ao vivo: nunca uma volta imediata às cegas
