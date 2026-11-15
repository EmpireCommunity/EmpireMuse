"""Chave de serviço do painel externo e a chave anterior da rotação (contrato T-456 §2, m1)."""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Optional

from empiremuse.rpc_protocol import same_secret

logger = logging.getLogger(__name__)

_FINGERPRINT_PREFIX = b"empiremuse-panel-api-key:"


def key_fingerprint(key: str) -> str:
    """A impressão (SHA-256) de uma chave: é o que a sessão guarda no lugar da chave."""
    return hashlib.sha256(_FINGERPRINT_PREFIX + key.encode("utf-8")).hexdigest()


class ServiceKeys:
    """A chave atual e, durante a rotação, a anterior (só até ``previous_expires_at``, conferido a cada uso)."""

    def __init__(self, current: str, previous: str = "", previous_expires_at: Optional[float] = None) -> None:
        self._current = current
        self._previous = previous
        self._previous_expires_at = previous_expires_at

    def match(self, provided: Any, now: float) -> Optional[str]:
        """A impressão da chave que confere, ou ``None``. As duas comparações rodam sempre (sem curto-circuito)."""
        is_current = same_secret(provided, self._current)
        is_previous = same_secret(provided, self._previous)
        previous_valid = self._previous_expires_at is not None and now < self._previous_expires_at
        if is_current:
            return key_fingerprint(self._current)
        if is_previous and previous_valid:
            logger.warning("API do painel externo: a chave anterior está em uso; conclua a rotação e remova-a.")
            return key_fingerprint(self._previous)
        return None
