"""Concessões do comando do dono (§3.4): o dono PEDE, só o clique do titular apaga.

A concessão é de uso único, vale ``OWNER_GRANT_TTL_S``, é amarrada ao ID do titular e mora só em memória (um reinício a invalida e o dono
reemite). O ``custom_id`` do botão carrega apenas o código dela. Esta classe não apaga nada nem decide quem pode ser dono: guarda, confere e
conta. Limites: ``OWNER_GRANTS_PER_HOUR`` por dono e 1 pendente por titular.
"""

from __future__ import annotations

import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from empiremuse.rules.erasure import OWNER_GRANT_TTL_S, OWNER_GRANTS_PER_HOUR, RATE_WINDOW_S, SNOWFLAKE_LIMIT

GRANT_CODE_BYTES = 16
_CODE_SHAPE = re.compile(r"[A-Za-z0-9_-]{16,64}")  # token_urlsafe(16) tem 22; nada de espaço, quebra de linha ou vazio


@dataclass(frozen=True)
class Grant:
    grant_id: str
    owner_id: int
    target_id: int
    expires_at: float


class GrantLimited(Exception):
    """Não foi emitida: ``reason`` é ``owner_limit`` (10/h por dono) ou ``target_pending`` (já há pedido pendente para o titular)."""

    def __init__(self, reason: str, retry_after_s: float = 0.0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after_s = retry_after_s


def _valid_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < SNOWFLAKE_LIMIT


def valid_grant_code(code: object) -> bool:
    """Código no formato emitido: texto não vazio, sem espaço em volta nem interno (``" "`` e ``"   "`` são recusados)."""
    return isinstance(code, str) and _CODE_SHAPE.fullmatch(code) is not None


class OwnerGrants:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl_s: float = OWNER_GRANT_TTL_S,
        per_hour: int = OWNER_GRANTS_PER_HOUR,
    ) -> None:
        self._clock = clock
        self._ttl = ttl_s
        self._per_hour = per_hour
        self._by_code: dict[str, Grant] = {}
        self._pending_code: dict[int, str] = {}  # titular -> código
        self._issued: dict[int, list[float]] = {}  # dono -> instantes de emissão na última hora

    def _prune(self, now: float) -> None:
        for code, grant in list(self._by_code.items()):
            if grant.expires_at <= now:
                self._drop(grant)
        for owner, stamps in list(self._issued.items()):
            kept = [stamp for stamp in stamps if now - stamp < RATE_WINDOW_S]
            if kept:
                self._issued[owner] = kept
            else:
                del self._issued[owner]

    def _drop(self, grant: Grant) -> None:
        self._by_code.pop(grant.grant_id, None)
        if self._pending_code.get(grant.target_id) == grant.grant_id:
            del self._pending_code[grant.target_id]

    def issue(self, owner_id: int, target_id: int) -> Grant:
        """Nova concessão do dono para o titular. Levanta ``GrantLimited`` (nada é emitido) e ``ValueError`` para ID inválido."""
        if not _valid_id(owner_id) or not _valid_id(target_id):
            raise ValueError("ID inválido")
        now = self._clock()
        self._prune(now)
        if target_id in self._pending_code:
            raise GrantLimited("target_pending", self._by_code[self._pending_code[target_id]].expires_at - now)
        stamps = self._issued.get(owner_id, [])
        if len(stamps) >= self._per_hour:
            raise GrantLimited("owner_limit", max(0.0, min(stamps) + RATE_WINDOW_S - now))
        grant = Grant(secrets.token_urlsafe(GRANT_CODE_BYTES), owner_id, target_id, now + self._ttl)
        self._by_code[grant.grant_id] = grant
        self._pending_code[target_id] = grant.grant_id
        self._issued[owner_id] = [*stamps, now]
        return grant

    def redeem(self, grant_id: str, clicker_id: int) -> Grant | None:
        """Consome a concessão se ela existe, não expirou e ``clicker_id`` é o titular; senão ``None`` e NADA é consumido.

        Quem não é o titular não gasta a concessão (ela continua valendo para o titular). Código fora do formato (vazio, em branco) é recusado.
        """
        if not valid_grant_code(grant_id) or not _valid_id(clicker_id):
            return None
        self._prune(self._clock())
        grant = self._by_code.get(grant_id)
        if grant is None or grant.target_id != clicker_id:
            return None
        self._drop(grant)
        return grant

    def restore(self, grant: Grant) -> bool:
        """Devolve uma concessão consumida quando NADA foi apagado (limite ou serviço ocupado). Expirada ou já substituída não volta."""
        now = self._clock()
        self._prune(now)
        if grant.expires_at <= now or grant.target_id in self._pending_code or grant.grant_id in self._by_code:
            return False
        self._by_code[grant.grant_id] = grant
        self._pending_code[grant.target_id] = grant.grant_id
        return True

    def revoke(self, grant_id: str) -> None:
        """Cancela (o titular recusou ou a DM não saiu): libera o pendente do titular."""
        grant = self._by_code.get(grant_id) if isinstance(grant_id, str) else None
        if grant is not None:
            self._drop(grant)
