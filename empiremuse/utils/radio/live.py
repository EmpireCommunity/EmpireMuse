"""O runner da rádio AO VIVO (item 8, R3): o mesmo ciclo da R1 (presença, pausa do dono, premium, limites do processo) com outra fonte.

Muda só o que a fonte exige:

- **Tocar** é pedir ao ``ports.play_live()`` um token novo do relay e mandar o Lavalink tocar o endereço local; não há relógio virtual.
- **Queda** (o fluxo acabou, o relay encerrou, a estação não abriu): recuo de 2, 4, 8, 16 e 32 s com jitter de ±20 % e o semáforo de reconexão da R1.
  Cinco tentativas formam um CICLO. Ciclo esgotado registra a falha da estação (3 em 1 h a tornam indisponível) e tenta a outra: a principal vai
  para a reserva, a reserva volta para a principal, desde que a outra esteja saudável. Sem alternativa, a rádio fica na call em silêncio, "fora do
  ar", tentando a cada 60 s; a principal volta sozinha quando o health check a reabilita (``restore_primary``).
- **Título** (ICY, já limpo pela R-11): vira parte do estado publicado; a página e os painéis só escapam.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Optional

from empiremuse.rules.radio import LIVE_CYCLE_ATTEMPTS, backoff_s, live_backoff_s
from empiremuse.utils.radio.runner import Phase, ProcessLimits, RadioPorts, RadioRunner
from empiremuse.utils.radio.service import RadioState

logger = logging.getLogger(__name__)

OUTAGE_BACKOFF_STEP = 4  # índice de 60 s em ``BACKOFF_STEPS_S``: sem alternativa, uma tentativa por minuto


class LiveRadioRunner(RadioRunner):
    def __init__(
        self,
        radio_id: str,
        ports: RadioPorts,
        limits: ProcessLimits,
        *,
        publish: Callable[[RadioState | None], None],
    ) -> None:
        super().__init__(radio_id, ports, limits, publish=publish)
        self.failures = 0  # tentativas falhas do ciclo atual
        self.outage = False  # os dois lados esgotados: em silêncio até alguém voltar
        self.title: Optional[str] = None

    # ------------------------------------------------------------------ estado

    def _state(self, phase: Phase) -> RadioState:
        base = super()._state(phase)
        return RadioState(
            playing=base.playing,
            paused_by_owner=base.paused_by_owner,
            on_reserve=self.ports.on_reserve(),  # type: ignore[attr-defined]
            offline=base.offline or self.outage,
            title=self.title if phase is Phase.PLAYING else None,
            paused_by_admin=base.paused_by_admin,
        )

    def set_title(self, title: Optional[str]) -> None:
        """O título que o relay leu do fluxo (já limpo e com o teto de 1 a cada 15 s). Só vale tocando."""
        if title == self.title:
            return
        self.title = title
        if self.phase is Phase.PLAYING:
            self._set(Phase.PLAYING)

    # ------------------------------------------------------------------ fonte

    async def _load_source(self) -> None:
        return  # não há playlist para carregar

    async def _start_source(self) -> None:
        self.title = None
        await self.ports.play_live()  # type: ignore[attr-defined]
        self.failures = 0
        self.outage = False

    # ------------------------------------------------------------------ queda, ciclo e reserva

    def _backoff(self) -> float:
        if self.outage:
            return backoff_s(OUTAGE_BACKOFF_STEP, self.ports.jitter())
        return live_backoff_s(self.attempt, self.ports.jitter())

    async def _after_failed_attempt(self) -> None:
        self.failures += 1
        if self.failures < LIVE_CYCLE_ATTEMPTS or self.outage:
            return
        self.failures = 0
        self.attempt = 0
        await self.ports.cycle_exhausted()  # type: ignore[attr-defined]
        if await self.ports.switch_source():  # type: ignore[attr-defined]
            logger.warning("Rádio ao vivo trocou de estação")
            self._set(Phase.RECONNECTING)
            return
        self.outage = True
        self._set(Phase.RECONNECTING)

    async def on_stream_lost(self) -> None:
        """O Lavalink avisou que o fluxo acabou ou falhou (fim, exceção, travou). Só vale tocando; pausas nossas não passam por aqui."""
        if self._stopped or self.phase is not Phase.PLAYING:
            return
        await self.on_disconnect()

    async def restore_primary(self) -> bool:
        """O health check reabilitou a estação principal: se a rádio está na reserva (ou em silêncio), volta para ela. Tocando, a volta é pelo
        recuo (como qualquer troca de fluxo); pausada ou em recuo, a próxima tentativa já usa a principal."""
        if self._stopped or not (self.ports.on_reserve() or self.outage):  # type: ignore[attr-defined]
            return False
        if not await self.ports.use_primary():  # type: ignore[attr-defined]
            return False
        self.failures = 0
        self.outage = False
        if self.phase is Phase.PLAYING:
            self.attempt = 0
            await self.on_disconnect()
        return True
