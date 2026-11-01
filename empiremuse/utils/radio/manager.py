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
TICK_TIMEOUT_S = 60.0  # um passo travado de uma rádio não segura o das outras
BOOT_RETRY_START_S = 30.0
BOOT_RETRY_MAX_S = 300.0
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
        self._radios: dict[str, Radio] = {}
        self._starts: dict[str, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()
        self._ticker: Optional[asyncio.Task] = None
        hub.control.on_change(self._on_pause_all_change)

    # ------------------------------------------------------------------ tarefas de fundo

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task) -> None:
        """Tarefa de fundo terminou: sai da lista e, se morreu de exceção, o tipo vai ao log (nunca morrer calada)."""
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.warning("Tarefa de rádio terminou com erro (%s)", type(error).__name__)

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
            runner._stopped = True
            runner.phase = Phase.OFF
        self._runners.clear()
        self._radios.clear()
        self._starts.clear()

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
        self._radios[radio.id] = radio
        return runner

    def is_running(self, radio_id: str) -> bool:
        """Há um runner para esta rádio (inclusive o que ainda espera a vez na fila da subida)?"""
        runner = self._runners.get(radio_id)
        return runner is not None and not runner.stopped

    async def activate(self, radio: Radio, *, queued: bool = False, wait: bool = True) -> None:
        """Liga o runner da rádio. ``queued`` (subida): entra na fila global de retomadas; ``wait=False`` (comando): devolve na hora e a
        partida segue em segundo plano (um canal sem permissão não prende a resposta do comando até o token expirar)."""
        if self.is_running(radio.id):
            return
        runner = self._runner_for(radio)
        if runner is None:
            return
        self._start_sweeper(radio)
        if queued:
            self._starts[radio.id] = self._spawn(self._boot(runner))
        elif wait:
            await runner.start()
        else:
            self._starts[radio.id] = self._spawn(runner.start())

    async def _boot(self, runner: RadioRunner) -> None:
        """Subida: UMA tentativa pela fila global (2 por vez, 1 s entre elas); quem não conseguiu recua FORA da fila, sem segurar vaga."""
        if not await self.limits.resumed(runner.ports, runner.try_start):
            await runner.retry()

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
        """Para a rádio de verdade: cancela a partida agendada (recuo ou fila da subida) e marca o runner como parado, então nenhuma volta
        que já estava no ar a religa."""
        self._stop_sweeper(radio_id)
        start = self._starts.pop(radio_id, None)
        runner = self._runners.pop(radio_id, None)
        self._radios.pop(radio_id, None)
        if runner is not None:
            await runner.stop()
        if start is not None and not start.done():
            start.cancel()
        self.service.publish(radio_id, None)

    async def stop_radios(self, radios, *, destroy_players: bool = True) -> None:
        """Para as rádios dadas e faz o bot de cada uma sair da call (``destroy``); uma falha não impede as outras."""
        for radio in radios:
            try:
                await self.deactivate(radio.id)
            finally:
                if destroy_players:
                    player = self.player_of(radio)
                    if player is not None:
                        try:
                            await player.destroy()
                        except Exception as error:  # noqa: BLE001
                            logger.warning("Rádio sem sair da call (%s)", type(error).__name__)

    def running_in(self, guild_id: int, channel_id: int | None = None) -> list[Radio]:
        """As rádios deste processo no servidor (e canal, se dado), vistas pelo gerenciador, não pelo banco."""
        return [
            r
            for r in self._radios.values()
            if r.guild_id == guild_id and (channel_id is None or r.channel_id == channel_id)
        ]

    async def revoke_premium(self, guild_id: int, *, updated_by: str = "owner") -> int:
        """R-1 inteira: apaga o premium, desliga as rádios no banco E para os runners e players do servidor. Devolve quantas desligou."""
        try:
            disabled = await self.hub.store.revoke_premium(guild_id, updated_by=updated_by)
        finally:
            await self.stop_radios(
                self.running_in(guild_id)
            )  # mesmo que o banco falhe no meio, nada segue tocando sem direito
        return len(disabled)

    # ------------------------------------------------------------------ subida

    async def resume_all_with_retry(self) -> int:
        """``resume_all`` até dar certo (30 s, 60 s... até 5 min entre tentativas): uma falha passageira do banco na subida não deixa as
        rádios caídas até o próximo restart nem a pausa geral sem ser lida."""
        wait = BOOT_RETRY_START_S
        while True:
            try:
                return await self.resume_all()
            except Exception as error:  # noqa: BLE001 - só o tipo vai ao log (R-12)
                logger.warning(
                    "Rádios não retomadas na subida (%s); nova tentativa em %.0fs", type(error).__name__, wait
                )
                await asyncio.sleep(wait)
                wait = min(wait * 2, BOOT_RETRY_MAX_S)

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
        """Um passo de presença/pausa em todas as rádios, em paralelo (chamado de ``TICK_SECONDS`` em ``TICK_SECONDS``)."""

        async def one(runner: RadioRunner) -> None:
            try:
                await asyncio.wait_for(runner.tick(), TICK_TIMEOUT_S)
            except Exception as error:  # noqa: BLE001 - uma rádio com defeito ou travada não para as outras
                logger.warning("Rádio com falha no passo de presença (%s)", type(error).__name__)

        await asyncio.gather(*(one(r) for r in list(self._runners.values())))

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
