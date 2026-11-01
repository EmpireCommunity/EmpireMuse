"""Quem faz as rádios rodarem neste processo (item 8, R1): ativa e desativa os ``RadioRunner``, retoma tudo na subida e reage à pausa geral.

- **Subida / volta do gateway / restart do Lavalink (R-3):** ``resume_all`` primeiro reconcilia o banco (contador recalculado, excesso
  desligado) e enfileira as rádios ligadas na fila global do ``ProcessLimits``: 2 por vez, 1 s entre elas. Sai UM INFO com o total.
- **Pausa geral do dono:** o ouvinte do ``RadioControl`` manda um ``tick`` a todos os runners (a retomada também passa pela fila global).
- **Resumo (``logs_loop``):** ``counts()`` devolve ligadas, tocando e pausadas, sem ID nenhum.

O adaptador do Discord e do Lavalink (``ports_factory``) é injetado: sem ele nenhuma rádio sobe (falha fechada), e os testes usam dublês.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, NamedTuple, Optional

from empiremuse.rules.radio import Radio
from empiremuse.utils.radio.chat import ChatPorts, ChatSweeper
from empiremuse.utils.radio.runner import Phase, ProcessLimits, RadioPorts, RadioRunner
from empiremuse.utils.radio.service import RadioService

if TYPE_CHECKING:
    from empiremuse.utils.radio import RadioHub

logger = logging.getLogger(__name__)

TICK_SECONDS = 5.0
PortsFactory = Callable[[Radio], RadioPorts]
ChatPortsFactory = Callable[[Radio], ChatPorts]


class RadioCounts(NamedTuple):
    on: int  # ligadas neste processo (pausadas incluídas)
    playing: int
    paused: int  # pausadas por presença ou pelo dono


class RadioManager:
    def __init__(self, hub: RadioHub, service: RadioService, *, limits: Optional[ProcessLimits] = None) -> None:
        self.hub = hub
        self.service = service
        self.limits = limits or ProcessLimits()
        self.ports_factory: Optional[PortsFactory] = None
        self.chat_ports_factory: Optional[ChatPortsFactory] = None
        self._sweepers: dict[str, tuple[ChatSweeper, asyncio.Task]] = {}
        self._runners: dict[str, RadioRunner] = {}
        self._tasks: set[asyncio.Task] = set()
        self._ticker: Optional[asyncio.Task] = None
        hub.control.on_change(self._on_pause_all_change)

    # ------------------------------------------------------------------ tarefas de fundo

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def start_ticker(self) -> None:
        """Liga (uma vez) o passo periódico de presença e pausa de todas as rádios do processo."""
        if self._ticker is None or self._ticker.done():
            self._ticker = asyncio.get_running_loop().create_task(self._tick_loop())

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            await self.tick_all()

    # ------------------------------------------------------------------ eventos do player (ganchos de ``player_voice``)

    def note_presence(self, radio_id: str, listening: bool) -> None:
        """Alguém entrou, saiu ou ensurdeceu na call da rádio: registra e deixa o passo seguinte decidir."""
        runner = self._runners.get(radio_id)
        if runner is not None:
            self._spawn(runner.on_presence(1 if listening else 0))

    def note_disconnect(self, radio_id: str) -> None:
        """O bot caiu, foi movido ou desconectado (inclusive por membro): volta pelo recuo."""
        runner = self._runners.get(radio_id)
        if runner is not None:
            self._spawn(runner.on_disconnect())

    async def close(self) -> None:
        if self._ticker is not None:
            self._ticker.cancel()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for radio_id in list(self._sweepers):
            self._stop_sweeper(radio_id)
        for runner in list(self._runners.values()):
            runner.phase = Phase.OFF
        self._runners.clear()

    # ------------------------------------------------------------------ ativar / desativar

    def _runner_for(self, radio: Radio) -> Optional[RadioRunner]:
        if self.ports_factory is None:
            logger.warning("Rádio não iniciada: o adaptador do Discord não foi instalado")
            return None
        runner = RadioRunner(
            radio.id,
            self.ports_factory(radio),
            self.limits,
            publish=lambda state: self.service.publish(radio.id, state),
        )
        self._runners[radio.id] = runner
        return runner

    def is_running(self, radio_id: str) -> bool:
        runner = self._runners.get(radio_id)
        return runner is not None and runner.phase is not Phase.OFF

    async def activate(self, radio: Radio, *, queued: bool = False) -> None:
        """Liga o runner da rádio. ``queued`` (subida): entra na fila global de retomadas; o comando ``/radio ligar`` é imediato."""
        if self.is_running(radio.id):
            return
        runner = self._runner_for(radio)
        if runner is None:
            return
        self._start_sweeper(radio)
        if queued:
            self._spawn(self.limits.resumed(runner.ports, runner.start))
        else:
            await runner.start()

    def _start_sweeper(self, radio: Radio) -> None:
        """A varredura do chat da call (R-4) roda enquanto a rádio estiver ligada, pausada ou não; sem as portas do chat não há varredura."""
        if self.chat_ports_factory is None or radio.id in self._sweepers:
            return
        ports = self.chat_ports_factory(radio)
        sweeper = ChatSweeper(ports, radio.chat, stage=bool(getattr(ports, "stage", False)), limits=self.limits)
        self._sweepers[radio.id] = (sweeper, self._spawn(sweeper.run()))

    def update_chat_rules(self, radio: Radio) -> None:
        """A página web mudou as regras do chat: valem na próxima varredura."""
        entry = self._sweepers.get(radio.id)
        if entry is not None:
            entry[0].update(radio.chat)

    def _stop_sweeper(self, radio_id: str) -> None:
        entry = self._sweepers.pop(radio_id, None)
        if entry is not None:
            entry[0].running = False
            entry[1].cancel()

    async def deactivate(self, radio_id: str) -> None:
        self._stop_sweeper(radio_id)
        runner = self._runners.pop(radio_id, None)
        if runner is not None:
            await runner.stop()
        self.service.publish(radio_id, None)

    # ------------------------------------------------------------------ subida

    async def resume_all(self) -> int:
        """Reconcilia o banco e põe na fila global as rádios ligadas. Devolve quantas foram enfileiradas."""
        report = await self.hub.store.reconcile()
        await self.hub.control.load()
        await self.hub.roles.load()
        radios = await self.hub.store.list_enabled()
        for radio in radios:
            await self.activate(radio, queued=True)
        logger.info(
            "radio.toggle: %d ligadas na subida (%d sem premium e %d acima do teto desligadas, %d contadores corrigidos)",
            len(radios),
            report.without_premium,
            report.over_cap,
            report.counters_fixed,
        )
        return len(radios)

    # ------------------------------------------------------------------ pausa geral e presença

    def _on_pause_all_change(self, paused: bool) -> None:
        for runner in list(self._runners.values()):
            try:
                self._spawn(runner.tick())
            except RuntimeError:  # sem laço de eventos (teste síncrono ou encerramento)
                return

    async def tick_all(self) -> None:
        """Um passo de presença/pausa em todas as rádios (chamado de ``TICK_SECONDS`` em ``TICK_SECONDS``)."""
        for runner in list(self._runners.values()):
            try:
                await runner.tick()
            except Exception as error:  # noqa: BLE001 - uma rádio com defeito não para as outras
                logger.warning("Rádio com falha no passo de presença (%s)", type(error).__name__)

    def player_of(self, radio) -> object | None:
        """O player da rádio (se o bot dela tem um): o comando de desligar o destrói para o bot sair da call."""
        for bot in self.hub.store.pool.get_all_bots():
            if bot.user is not None and bot.user.id == radio.bot_id:
                return bot.music.players.get(radio.guild_id)
        return None

    def runner(self, radio_id: str) -> Optional[RadioRunner]:
        return self._runners.get(radio_id)

    # ------------------------------------------------------------------ resumo

    def counts(self) -> RadioCounts:
        phases = [runner.phase for runner in self._runners.values() if runner.phase is not Phase.OFF]
        playing = sum(phase is Phase.PLAYING for phase in phases)
        paused = sum(phase in (Phase.PAUSED_PRESENCE, Phase.PAUSED_OWNER) for phase in phases)
        return RadioCounts(on=len(phases), playing=playing, paused=paused)
