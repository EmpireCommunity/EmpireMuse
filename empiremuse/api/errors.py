"""Erros da API: ``code`` estável, sem texto de usuário final nem rastreio (contrato T-456 §4, Q7)."""

from __future__ import annotations

from typing import Optional, Sequence

import tornado.web

VALIDATION_FAILED = "validation_failed"
CONFLICT = "conflict"
FORBIDDEN = "forbidden"
NOT_FOUND = "not_found"
RATE_LIMITED = "rate_limited"
REAUTH_REQUIRED = "reauth_required"
REAUTH_USER_MISMATCH = "reauth_user_mismatch"
SESSION_EXPIRED = "session_expired"
DISCORD_UNAVAILABLE = "discord_unavailable"
BOT_UNAVAILABLE = "bot_unavailable"


class ApiError(tornado.web.HTTPError):
    """Falha esperada de uma rota: vira ``{"error": {"code", "fields"?, "request_id"}}`` com o status dado."""

    def __init__(
        self, status: int, code: str, *, fields: Optional[Sequence[str]] = None, retry_after: Optional[int] = None
    ) -> None:
        super().__init__(status)
        self.code = code
        self.fields = list(fields) if fields else None
        self.retry_after = retry_after


def validation_failed(*fields: str, status: int = 400) -> ApiError:
    return ApiError(status, VALIDATION_FAILED, fields=fields)


def rate_limited(retry_after: int) -> ApiError:
    return ApiError(429, RATE_LIMITED, retry_after=max(1, retry_after))
