"""O ciclo de vida de UMA rádio (item 8, R1): reconexão com recuo, pausa e retomada por presença, pausa geral do dono e os limites do processo.

É um núcleo sem Discord nem Lavalink: tudo que toca o mundo entra pelas ``RadioPorts`` (conectar, tocar a faixa virtual na posição, parar o
fluxo, contar ouvintes, conferir premium e canal, relógio e sono). Assim as garantias de segurança são testáveis com relógio falso:

- **R-1:** cada retomada e cada reconexão confere de novo o premium (cache de 30 s do ``PremiumStore``); sem premium, canal apagado ou sem
  permissão de conectar a rádio vira DESLIGADA (a transição do contador é da store) e o log leva só um WARNING sem IDs.
- **R-3:** retomadas em fila global (2 por vez, 1 s entre elas), reconexão de voz com semáforo global de 2 e retomada por presença com
  semáforo global de 3; o recuo tem jitter de ±20 %.
- **R-5:** pausa só depois de 30 s sem ouvinte (não ensurdecido) e retomada 5 s depois de um ouvinte chegar; toda queda, mudança de canal ou
  desconexão, inclusive a feita por membro, passa pelo recuo (5, 10, 20, 40, 60 s e depois 60 s). Nunca há volta imediata.
- **Pausa geral do dono:** a rádio para o fluxo, fica na call e nada a retoma (presença, botão, ``ligar``, subida) até o dono retomar.

O relógio virtual da rádio personalizada nunca para: a posição ao voltar a tocar é calculada pelo tempo decorrido desde que ela ligou.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from empiremuse.rules.radio import backoff_s, should_pause, should_resume, virtual_position
from empiremuse.utils.radio.service import RadioState

logger = logging.getLogger(__name__)

RESUME_CONCURRENCY = 2
RESUME_SPACING_S = 1.0
RECONNECT_CONCURRENCY = 2
PRESENCE_CONCURRENCY = 3
SWEEP_CONCURRENCY = 2
STABLE_AFTER_S = 120.0  # conectada por tanto tempo, a próxima queda recomeça o recuo do início


class Phase(StrEnum):
    OFF = "off"
    CONNECTING = "connecting"
    PLAYING = "playing"
    PAUSED_PRESENCE = "paused_presence"  # sem ouvinte: o fluxo está parado, o bot segue na call
    PAUSED_OWNER = "paused_owner"  # pausa geral do dono
    RECONNECTING = "reconnecting"


class Allowed(StrEnum):
    YES = "yes"
    NO_PREMIUM = "no_premium"
    NO_CHANNEL = "no_channel"
    NO_PERMISSION = "no_permission"


class RadioPorts(Protocol):
    """Tudo que o runner pede ao mundo. O adaptador real (Discord + Lavalink) e o dublê dos testes implementam isto."""

    def now(self) -> float: ...
    def jitter(self) -> float: ...
    async def sleep(self, seconds: float) -> None: ...
    async def allowed(self) -> Allowed: ...
    async def connect(self) -> bool: ...
    async def play_virtual(self, index: int, position_ms: int) -> None: ...
    async def stop_stream(self) -> None: ...
    def listeners(self) -> int: ...
    def paused_all(self) -> bool: ...
    async def turn_off(self, reason: Allowed) -> None: ...
    def durations_ms(self) -> list[int]: ...


@dataclass
class ProcessLimits:
    """Os limites globais do processo, compartilhados por todas as rádios."""

    resume: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(RESUME_CONCURRENCY))
    reconnect: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(RECONNECT_CONCURRENCY))
    presence: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(PRESENCE_CONCURRENCY))
    sweep: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(SWEEP_CONCURRENCY))
    spacing_s: float = RESUME_SPACING_S

    async def resumed(self, ports: RadioPorts, action: Callable[[], Awaitable[None]]) -> None:
        """Uma retomada pela fila global: 2 por vez, com ``spacing_s`` entre o fim de uma e a vaga seguinte."""
        async with self.resume:
            await action()
            await ports.sleep(self.spacing_s)


class RadioRunner:
    def __init__(
        self,
        radio_id: str,
        ports: RadioPorts,
        limits: ProcessLimits,
        *,
        publish: Callable[[RadioState | None], None],
    ) -> None:
        self.radio_id = radio_id
        self.ports = ports
        self.limits = limits
        self._publish = publish
        self.phase = Phase.OFF
        self.attempt = 0
        self.connected_at: float | None = None
        self.empty_since: float | None = None
        self.listening_since: float | None = None
        self.virtual_started_at: float | None = None

    # ------------------------------------------------------------------ estado

    def _set(self, phase: Phase) -> None:
        self.phase = phase
        if phase is Phase.OFF:
            self._publish(None)
            return
        self._publish(
            RadioState(
                playing=phase is Phase.PLAYING,
                paused_by_owner=phase is Phase.PAUSED_OWNER,
                offline=phase in (Phase.CONNECTING, Phase.RECONNECTING),
            )
        )

    async def _verdict(self) -> Allowed | None:
        """A conferência de premium/canal/permissão. Falha do banco ou do Discord NÃO desliga a rádio: devolve ``None`` ("não sei agora") e
        quem chama tenta de novo depois do recuo ou espera a próxima oportunidade."""
        try:
            return await self.ports.allowed()
        except Exception as error:  # noqa: BLE001 - só o tipo vai ao log (R-12)
            logger.warning("Rádio sem conferir o direito agora (%s)", type(error).__name__)
            return None

    # ------------------------------------------------------------------ ligar / desligar

    async def start(self) -> None:
        """Liga a rádio: confere o que pode, conecta e toca (a pausa geral do dono deixa conectada e parada)."""
        verdict = await self._verdict()
        if verdict is not None and verdict is not Allowed.YES:
            await self._shutdown(verdict)
            return
        self._set(Phase.CONNECTING)
        self.virtual_started_at = self.ports.now()
        await self._connect_with_backoff(wait_first=verdict is None)

    async def stop(self) -> None:
        """Desligada por quem manda (comando, painel): para o fluxo e esquece o estado. O contador é da store."""
        await self.ports.stop_stream()
        self._set(Phase.OFF)

    async def _shutdown(self, reason: Allowed) -> None:
        logger.warning("Rádio desligada (%s)", reason.value)  # sem IDs, sem nomes (R-12)
        await self.ports.stop_stream()
        self._set(Phase.OFF)
        await self.ports.turn_off(reason)

    # ------------------------------------------------------------------ reconexão (R-5, R-3, R-1)

    async def _connect_with_backoff(self, *, wait_first: bool = False) -> None:
        """Conecta; falha ou queda espera o recuo (5, 10, 20, 40, 60 s...) e confere premium/canal/permissão antes de cada volta.

        ``wait_first``: depois de uma queda a PRIMEIRA volta também espera o recuo (nunca há volta imediata); só a primeira conexão, ao
        ligar, é imediata.
        """
        first = not wait_first
        while self.phase is not Phase.OFF:
            if not first:
                await self.ports.sleep(backoff_s(self.attempt, self.ports.jitter()))
                self.attempt += 1
                verdict = await self._verdict()
                if verdict is None:
                    continue  # não deu para conferir: não conecta às cegas, espera o próximo recuo
                if verdict is not Allowed.YES:
                    await self._shutdown(verdict)
                    return
            first = False
            async with self.limits.reconnect:
                connected = await self.ports.connect()
            if connected:
                self.connected_at = self.ports.now()
                await self._after_connected()
                return
            self._set(Phase.RECONNECTING)

    async def on_disconnect(self) -> None:
        """Queda, mudança de canal ou desconexão (inclusive por membro): sempre pelo recuo, nunca volta imediata."""
        if self.phase in (Phase.OFF, Phase.RECONNECTING, Phase.CONNECTING):
            return  # já voltando: os eventos seguintes se juntam à volta em curso
        now = self.ports.now()
        if self.connected_at is not None and now - self.connected_at >= STABLE_AFTER_S:
            self.attempt = 0
        self._set(Phase.RECONNECTING)
        await self.ports.stop_stream()
        await self._connect_with_backoff(wait_first=True)

    # ------------------------------------------------------------------ depois de conectar

    async def _after_connected(self) -> None:
        if self.ports.paused_all():
            self._set(Phase.PAUSED_OWNER)
            return
        if self.ports.listeners() == 0:
            self.empty_since = self.ports.now()
            self.listening_since = None
            self._set(Phase.PAUSED_PRESENCE)
            return
        await self._play()

    async def _play(self) -> None:
        elapsed_ms = int((self.ports.now() - (self.virtual_started_at or self.ports.now())) * 1000)
        index, position = virtual_position(self.ports.durations_ms(), elapsed_ms)
        await self.ports.play_virtual(index, position)
        self.empty_since = self.listening_since = None
        self._set(Phase.PLAYING)

    # ------------------------------------------------------------------ presença (R-5)

    async def on_presence(self, listeners: int) -> None:
        """Alguém entrou, saiu ou ensurdeceu: só registra desde quando a call está vazia/ocupada; a decisão é do ``tick``."""
        now = self.ports.now()
        if listeners == 0:
            if self.empty_since is None:
                self.empty_since = now
            self.listening_since = None
        else:
            if self.listening_since is None:
                self.listening_since = now
            self.empty_since = None

    async def tick(self) -> None:
        """Avalia pausa e retomada por presença e pelo dono. Chamado de tempos em tempos (e depois de cada evento)."""
        now = self.ports.now()
        if self.phase in (Phase.OFF, Phase.CONNECTING, Phase.RECONNECTING):
            return
        if self.ports.paused_all():
            if self.phase is not Phase.PAUSED_OWNER:
                await self.ports.stop_stream()
                self._set(Phase.PAUSED_OWNER)
            return
        if self.phase is Phase.PAUSED_OWNER:
            await self.limits.resumed(self.ports, self._resume_from_owner)  # fila global: 2 por vez, 1 s entre elas
            return
        if self.phase is Phase.PLAYING and should_pause(self.empty_since, now):
            await self.ports.stop_stream()
            self._set(Phase.PAUSED_PRESENCE)
        elif self.phase is Phase.PAUSED_PRESENCE and should_resume(self.listening_since, now):
            await self._resume_by_presence()

    async def _resume_from_owner(self) -> None:
        verdict = await self._verdict()
        if verdict is None:
            return  # sem conferir não retoma; o próximo passo tenta de novo
        if verdict is not Allowed.YES:
            await self._shutdown(verdict)
            return
        if self.ports.listeners() == 0:
            self.empty_since = self.ports.now()
            self._set(Phase.PAUSED_PRESENCE)
            return
        await self._play()

    async def _resume_by_presence(self) -> None:
        async with self.limits.presence:
            verdict = await self._verdict()
            if verdict is None:
                return
            if verdict is not Allowed.YES:
                await self._shutdown(verdict)
                return
            await self._play()
