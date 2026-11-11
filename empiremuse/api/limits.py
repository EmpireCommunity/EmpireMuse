"""Limites da API (contrato T-456 §7 m7): memória, processo único, janela de 1 minuto."""

from __future__ import annotations

import time
from typing import Callable

from empiremuse.panel.ratelimit import RateLimiter

USER_READ_PER_MINUTE = 120
USER_WRITE_PER_MINUTE = 20
KEY_PER_MINUTE = 600
SESSIONS_PER_MINUTE = 120
KEY_FAILURES_PER_MINUTE = 30
WARN_WINDOW_SECONDS = 60
MAX_WARNED_PEERS = 1000


class ApiLimits:
    """Os baldes: leitura e escrita por ``user_id`` (conferidos antes do da chave), a chave de serviço, a criação de
    sessões e as falhas de chave por par TCP (só elas contam; ``401`` de sessão nunca entra aqui)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.user_read = RateLimiter(USER_READ_PER_MINUTE, clock=clock)
        self.user_write = RateLimiter(USER_WRITE_PER_MINUTE, clock=clock)
        self.key = RateLimiter(KEY_PER_MINUTE, clock=clock)
        self.sessions = RateLimiter(SESSIONS_PER_MINUTE, clock=clock)
        self.key_failures = RateLimiter(KEY_FAILURES_PER_MINUTE, clock=clock)
        self._clock = clock
        self._warned_until: dict[str, float] = {}

    def should_warn(self, peer: str) -> bool:
        """Um aviso por par e por janela (nunca um por requisição bloqueada)."""
        now = self._clock()
        if len(self._warned_until) >= MAX_WARNED_PEERS:
            self._warned_until = {p: t for p, t in self._warned_until.items() if t > now}
        if self._warned_until.get(peer, 0.0) > now:
            return False
        self._warned_until[peer] = now + WARN_WINDOW_SECONDS
        return True
