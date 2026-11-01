"""A varredura do chat da call de uma rádio (item 8, R1, R-4): apaga em lote o que passou da regra, nunca o que é protegido.

- **Escopo:** só o canal da própria rádio (a porta já nasce amarrada a ele), nunca o canal da Jukebox.
- **Nunca apaga:** a mensagem fixada, o painel da rádio nem os avisos do próprio bot (a porta marca como ``protected``).
- **Em lote:** uma busca (até 100 mensagens) e UMA exclusão em lote por varredura, só com mensagens de menos de 14 dias; ids só na memória.
- **Ritmo:** na voz a cada ``max(5, after/2)`` s; no palco no máximo a cada 30 s; no processo, 2 varreduras por vez (``ProcessLimits.sweep``).
- **Sem permissão:** sem ``manage_messages`` ou ``read_message_history`` o Discord responde 403; a regra desliga depois de UMA tentativa
  (403 em laço derruba o IP do pool inteiro) e quem chama avisa no painel.
- **Log:** só contagens.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Protocol

from empiremuse.rules.radio import (
    SWEEP_BATCH,
    stage_sweep_interval_s,
    sweep_victims,
    voice_sweep_interval_s,
)
from empiremuse.utils.radio.runner import ProcessLimits

logger = logging.getLogger(__name__)


class PermissionLost(Exception):
    """O Discord recusou (403): falta ``manage_messages`` ou ``read_message_history`` no canal da rádio."""


class ChatPorts(Protocol):
    def now(self) -> float: ...
    async def sleep(self, seconds: float) -> None: ...
    async def fetch_recent(self, limit: int) -> Sequence[tuple[int, float, bool]]:
        """``(id, criada_em, protegida)`` das mensagens mais recentes do canal da rádio. Levanta ``PermissionLost`` no 403."""
        ...

    async def bulk_delete(self, ids: Sequence[int]) -> None:
        """Apaga as mensagens de uma vez. Levanta ``PermissionLost`` no 403."""
        ...


class ChatSweeper:
    def __init__(self, ports: ChatPorts, chat: Mapping[str, int], *, stage: bool, limits: ProcessLimits) -> None:
        self.ports = ports
        self.chat = dict(chat)
        self.stage = stage
        self.limits = limits
        self.disabled = False  # sem permissão: uma tentativa e acabou
        self.running = True
        self.deleted_total = 0
        self.sweeps = 0

    @property
    def interval_s(self) -> float:
        return stage_sweep_interval_s() if self.stage else voice_sweep_interval_s(self.chat["voice_delete_after_s"])

    async def sweep_once(self) -> int:
        """Uma varredura. Devolve quantas mensagens apagou; ``0`` também quando a regra se desligou por falta de permissão."""
        if self.disabled:
            return 0
        async with self.limits.sweep:
            self.sweeps += 1
            try:
                messages = await self.ports.fetch_recent(SWEEP_BATCH)
                victims = sweep_victims(messages, self.ports.now(), stage=self.stage, chat=self.chat)
                if victims:
                    await self.ports.bulk_delete(victims)
            except PermissionLost:
                self.disabled = True
                logger.warning("Regras do chat da rádio desligadas: o Discord recusou a permissão")
                return 0
        self.deleted_total += len(victims)
        if victims:
            logger.debug("Chat da rádio: %d mensagens apagadas", len(victims))
        return len(victims)

    async def run(self) -> None:
        """O laço da varredura; termina quando a regra se desliga ou ``running`` vira falso."""
        while self.running and not self.disabled:
            await self.ports.sleep(self.interval_s)
            await self.sweep_once()

    def update(self, chat: Mapping[str, int]) -> None:
        """A página web mudou as regras: valem na próxima varredura."""
        self.chat = dict(chat)
