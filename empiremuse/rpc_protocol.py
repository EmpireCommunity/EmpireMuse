# -*- coding: utf-8 -*-
"""Protocolo do relé RPC (``/ws``): limites, campos permitidos e validações puras, sem I/O.

O servidor (``WebSocketHandler``) e o cliente interno (``WSClient``) em ``empiremuse/web_app.py`` usam este módulo.
"""

from __future__ import annotations

import hmac
import secrets
import time
from collections import deque
from typing import Any, Mapping, Optional

from packaging import version
from packaging.version import InvalidVersion, Version

from empiremuse.rules.rpc import TOKEN_LENGTH

RPC_KEY_HEADER = "X-RPC-Key"

# Códigos de fechamento do WebSocket.
CLOSE_REFUSED = 4200
CLOSE_MALFORMED = 4400
CLOSE_UNAUTHORIZED = 4401
CLOSE_RATE_LIMITED = 4429
CLOSE_TRY_AGAIN_LATER = 1013

MAX_MESSAGE_SIZE = 64 * 1024
# Ping do servidor ao cliente (segundos). A Cloudflare encerra WebSocket ocioso em ~100 s; 30 s mantém a conexão aberta
# atrás dela e do Traefik. O Tornado (6.5) não aceita timeout maior que o intervalo (limita e avisa no log), então o
# servidor derruba o cliente que não responde ao ping em 30 s.
WEBSOCKET_PING_INTERVAL = 30
WEBSOCKET_PING_TIMEOUT = WEBSOCKET_PING_INTERVAL
MAX_CONNECTIONS = 1000
MAX_USER_IDS = 3
MAX_BOT_IDS = 250
RATE_LIMIT_MESSAGES = 20
RATE_LIMIT_WINDOW = 10.0

# Prova do token no registro do usuário (T-533a): o tempo é bem abaixo do ping (30 s) porque, enquanto o registro espera, o laço de
# leitura da conexão não responde ao pong.
PROOF_TIMEOUT = 4.0
PROOF_MAX_IN_FLIGHT = 16
PROOF_DENIED_LIMIT = 5
PROOF_DENIED_WINDOW = 60.0
PROOF_DENIED_CACHE_SIZE = 4096
PROOF_LOG_INTERVAL = 60.0
PROOF_DENIED_REASON = "Token inválido ou ausente. Gere um token com /rich_presence, cole no app e reinicie o app."
PROOF_RATE_REASON = "Tentativas inválidas demais. Tente novamente mais tarde."
BUSY_REASON = "Servidor ocupado, tente novamente mais tarde."

MIN_ID = 10**14  # 15 dígitos
MAX_ID = 10**20  # exclusivo: no máximo 20 dígitos
MAX_VERSION_LENGTH = 32
TOKEN_LENGTHS = (0, TOKEN_LENGTH)

USER_OP = "rpc_update"
BOT_OPS = frozenset({"update", "idle", "close"})

USER_TO_BOT_FIELDS = ("op", "user_ids", "version")
# União dos campos de ``LavalinkPlayer.process_rpc`` (update, idle e close), sem ``token`` e ``auth_enabled``.
BOT_TO_USER_FIELDS = frozenset(
    {
        "op",
        "bot_id",
        "bot_name",
        "user",
        "thumb",
        "guild",
        "track",
        "listen_along_invite",
        "lastfm_user",
        "start_time",
        "idle_starttime",
        "idle_endtime",
        "invite_permissions",
        "public",
        "support_server",
    }
)

_ephemeral_secret: Optional[str] = None


def get_rpc_secret(config: Mapping[str, Any]) -> str:
    """Segredo do registro de bots: ``RPC_SERVER_SECRET`` ou, se vazio, um segredo efêmero único por processo."""
    global _ephemeral_secret

    configured = str(config.get("RPC_SERVER_SECRET") or "").strip()
    if configured:
        return configured

    if _ephemeral_secret is None:
        _ephemeral_secret = secrets.token_hex(32)
    return _ephemeral_secret


def same_secret(provided: Any, expected: str) -> bool:
    """Comparação em tempo constante; valor ausente, vazio ou que não seja texto nunca confere."""
    if not isinstance(provided, str) or not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def normalize_origin(origin: str) -> str:
    return origin.strip().lower().rstrip("/")


def parse_allowed_origins(value: Any) -> frozenset[str]:
    """Lista separada por vírgulas (``RPC_ALLOWED_ORIGINS``); entradas vazias são descartadas."""
    if not isinstance(value, str):
        return frozenset()
    return frozenset(o for o in (normalize_origin(p) for p in value.split(",")) if o)


def is_valid_id(value: Any) -> bool:
    """ID de usuário/bot: inteiro positivo de 15 a 20 dígitos (``bool`` não conta)."""
    return type(value) is int and MIN_ID <= value < MAX_ID


def is_valid_id_list(value: Any, limit: Optional[int] = None) -> bool:
    if not isinstance(value, list) or not value:
        return False
    if limit is not None and len(value) > limit:
        return False
    return all(is_valid_id(i) for i in value)


def parse_app_version(value: Any) -> Version:
    """Versão informada pelo app; ausente ou inválida vale ``0`` (abaixo de qualquer versão mínima)."""
    if isinstance(value, str) and len(value) <= MAX_VERSION_LENGTH:
        try:
            return version.parse(value)
        except InvalidVersion:
            pass
    return version.parse("0")


def pick_fields(data: Mapping[str, Any], allowed) -> dict:
    return {k: data[k] for k in allowed if k in data}


def same_token(provided: Any, expected: Any) -> bool:
    """Compara tokens do usuário (texto, ``""`` quando ausente) em tempo constante."""
    left = provided if isinstance(provided, str) else ""
    right = expected if isinstance(expected, str) else ""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def authenticates(provided: Any, expected: Any) -> bool:
    """Autenticação ligada: os dois tokens precisam ser texto não vazio e iguais."""
    if not (isinstance(provided, str) and provided and isinstance(expected, str) and expected):
        return False
    return same_token(provided, expected)


class RateLimiter:
    """Janela deslizante: no máximo ``limit`` mensagens em ``window`` segundos."""

    def __init__(self, limit: int = RATE_LIMIT_MESSAGES, window: float = RATE_LIMIT_WINDOW):
        self.limit = limit
        self.window = window
        self._stamps: deque[float] = deque()

    def allow(self) -> bool:
        now = time.monotonic()
        while self._stamps and now - self._stamps[0] >= self.window:
            self._stamps.popleft()

        if len(self._stamps) >= self.limit:
            return False

        self._stamps.append(now)
        return True
