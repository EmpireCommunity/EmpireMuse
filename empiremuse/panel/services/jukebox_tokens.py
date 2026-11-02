"""Token assinado das confirmações da Jukebox (configurar e desligar): HMAC, 60 s como no Discord, uso único."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Callable, Optional

TOKEN_TTL_SECONDS = 60
MAX_USED_TOKENS = 10_000


class JukeboxTokens:
    """Token assinado (HMAC) ligado a usuário+servidor+escopo+alvo, com validade de 60 s e uso único."""

    def __init__(self, secret: str, clock: Callable[[], float] = time.monotonic) -> None:
        self._secret, self._clock = secret.encode("utf-8"), clock
        self._used: dict[str, float] = {}

    def _sign(self, payload: str) -> str:
        return hmac.new(self._secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]

    def issue(self, user_id: str, guild_id: int, scope: str, target: str) -> str:
        expires = int(self._clock()) + TOKEN_TTL_SECONDS
        payload = ".".join((user_id, str(guild_id), scope, target, str(expires), secrets.token_hex(8)))
        return f"{payload}~{self._sign(payload)}"

    def check(self, token: str, user_id: str, guild_id: int, scope: str, target: str) -> Optional[str]:
        """``None`` se vale; ``"invalido"`` (assinatura/validade/uso) ou ``"outro"`` (ligado a outro contexto)."""
        payload, _, signature = token.partition("~")
        if not signature or not hmac.compare_digest(signature, self._sign(payload)):
            return "invalido"
        fields = payload.split(".")
        if len(fields) != 6 or not fields[4].isdigit():
            return "invalido"
        self._forget_expired()
        if int(fields[4]) <= self._clock() or fields[5] in self._used:
            return "invalido"
        if fields[:4] != [user_id, str(guild_id), scope, target]:
            return "outro"
        return None

    def consume(self, token: str) -> None:
        payload = token.partition("~")[0].split(".")
        while len(self._used) >= MAX_USED_TOKENS:
            del self._used[next(iter(self._used))]
        self._used[payload[5]] = float(payload[4])

    def _forget_expired(self) -> None:
        now = self._clock()
        for nonce in [n for n, expires in self._used.items() if expires <= now]:
            del self._used[nonce]
