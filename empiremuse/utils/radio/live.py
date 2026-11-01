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
from empiremuse.utils.radio.panel import LiveView
from empiremuse.utils.radio.runner import STABLE_AFTER_S, Phase, ProcessLimits, RadioPorts, RadioRunner
from empiremuse.utils.radio.service import RadioState

logger = logging.getLogger(__name__)

OUTAGE_BACKOFF_STEP = 4  # índice de 60 s em ``BACKOFF_STEPS_S``: sem alternativa, uma tentativa por minuto


class StationFailure(Exception):
    """A falha foi da ESTAÇÃO (recusada pelo relay, sem áudio, sumiu do diretório). Só ela conta para o ciclo e para a saúde; falha do Discord, do
    Lavalink ou do diretório fora do ar não marca estação nenhuma como indisponível para todos os servidores."""


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
        self._station_fault = False  # a última tentativa falhou por culpa da estação?
        self._playing_since: Optional[float] = None

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

    def _set(self, phase: Phase) -> None:
        super()._set(phase)
        if phase is Phase.OFF:
            return
        state = self._state(phase)
        show = getattr(self.ports, "update_panel", None)
        if show is not None:  # o painel da call espelha título, reserva no ar e fora do ar (contrato §2 e §3)
            show(LiveView(title=state.title, on_reserve=state.on_reserve, offline=state.offline))

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
        self._station_fault = False
        try:
            await self.ports.play_live()  # type: ignore[attr-defined]
        except StationFailure:
            self._station_fault = True
            raise
        self._playing_since = self.ports.now()
        self.outage = False

    # ------------------------------------------------------------------ queda, ciclo e reserva

    def _backoff(self) -> float:
        if self.outage:
            return backoff_s(OUTAGE_BACKOFF_STEP, self.ports.jitter())
        return live_backoff_s(self.attempt, self.ports.jitter())

    async def _after_failed_attempt(self) -> None:
        fault, self._station_fault = self._station_fault, False
        if not fault:
            return  # conexão de voz, Lavalink ou diretório: não é a estação
        self.failures += 1
        await self._close_cycle_if_due()
        self._set(Phase.RECONNECTING)  # republica (reserva no ar, fora do ar)

    async def _close_cycle_if_due(self) -> None:
        """Cinco falhas da estação fecham o ciclo: registra a falha (3 em 1 h a marcam) e tenta a outra estação, se saudável. Não mexe na fase:
        quem chama já está (ou vai para) o recuo. Em silêncio ("fora do ar") o ciclo também fecha, e assim a outra estação é tentada quando voltar a ser saudável."""
        if self.failures < LIVE_CYCLE_ATTEMPTS:
            return
        self.failures = 0
        self.attempt = 0
        await self.ports.cycle_exhausted()  # type: ignore[attr-defined]
        if await self.ports.switch_source():  # type: ignore[attr-defined]
            logger.warning("Rádio ao vivo trocou de estação")
            return
        self.outage = True

    async def on_stream_lost(self) -> None:
        """O Lavalink avisou que o fluxo acabou ou falhou (fim, exceção, travou). Só vale tocando; pausas nossas não passam por aqui."""
        if self._stopped or self.phase is not Phase.PLAYING:
            return
        if self._is_stable():
            self.failures = self.attempt = 0
        else:
            self.failures += 1  # a estação aceitou e caiu logo: conta como falha, senão o ciclo nunca fecha e ela nunca é trocada nem marcada
            await self._close_cycle_if_due()
        await self.on_disconnect()

    def _is_stable(self) -> bool:
        return self._playing_since is not None and self.ports.now() - self._playing_since >= STABLE_AFTER_S

    async def _tick(self) -> None:
        await super()._tick()
        if self.phase is Phase.PLAYING and self.failures and self._is_stable():
            self.failures = self.attempt = 0  # tocando de forma estável: o ciclo de falhas recomeça

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
