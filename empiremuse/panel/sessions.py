"""Sessões do painel e tentativas de login, em memória. Nunca guardam tokens do Discord."""

from __future__ import annotations

import hashlib
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Optional

from empiremuse.panel.settings import PanelSettings

MAX_SESSIONS_PER_USER = 5
MAX_SESSIONS_TOTAL = 10_000
# 36 bytes = 48 caracteres: o base64 do cookie assinado não leva "=" (que forçaria aspas no valor do cookie)
SESSION_ID_BYTES = 36
ATTEMPT_TTL_SECONDS = 10 * 60
MAX_PENDING_ATTEMPTS = 10_000
SWEEP_INTERVAL_SECONDS = 60
# 256 bits, opaco: o token da API do painel externo (``X-EM-Session``); o store só guarda o SHA-256 dele
API_TOKEN_BYTES = 32

Clock = Callable[[], float]


@dataclass(frozen=True)
class PanelUser:
    id: str
    username: str
    global_name: Optional[str]
    avatar: Optional[str]


@dataclass(frozen=True)
class ManagedGuild:
    id: str
    name: str
    icon: Optional[str]
    permissions: int
    owner: bool


@dataclass
class Session:
    user: PanelUser
    guilds: tuple[ManagedGuild, ...]
    created_at: float
    last_seen: float
    consented_at: Optional[float] = None  # só quando o login saiu com ``prompt=consent`` (apagar dados exige isso)
    key_fingerprint: Optional[str] = None  # só nas sessões da API externa: a impressão da chave de serviço que a criou


class SessionStore:
    """Sessões com TTL absoluto, ociosidade e limites (5 por usuário, 10.000 no total).

    Ao exceder um limite remove a sessão mais antiga; a nova nunca é recusada.
    Expiradas são removidas no acesso e por uma varredura preguiçosa (no máximo 1 por minuto).
    """

    def __init__(self, settings: PanelSettings, clock: Clock = time.monotonic) -> None:
        self._ttl = settings.session_ttl_hours * 3600
        self._idle = settings.session_idle_minutes * 60
        self._clock = clock
        self._sessions: OrderedDict[str, Session] = OrderedDict()
        self._next_sweep = clock() + SWEEP_INTERVAL_SECONDS

    def create(self, user: PanelUser, guilds: tuple[ManagedGuild, ...], *, consented: bool = False) -> str:
        session_id = secrets.token_urlsafe(SESSION_ID_BYTES)
        self._insert(session_id, user, guilds, consented=consented)
        return session_id

    def create_api(self, user: PanelUser, guilds: tuple[ManagedGuild, ...], key_fingerprint: str) -> str:
        """Sessão da API externa: devolve o token (mostrado uma única vez); no store fica só o SHA-256 dele, preso à chave."""
        token = secrets.token_urlsafe(API_TOKEN_BYTES)
        self._insert(_api_id(token), user, guilds, key_fingerprint=key_fingerprint)
        return token

    def _insert(
        self,
        session_id: str,
        user: PanelUser,
        guilds: tuple[ManagedGuild, ...],
        *,
        consented: bool = False,
        key_fingerprint: Optional[str] = None,
    ) -> None:
        now = self._clock()
        self._sweep(now)
        own = [sid for sid, s in self._sessions.items() if s.user.id == user.id]
        for sid in own[: max(0, len(own) - MAX_SESSIONS_PER_USER + 1)]:
            del self._sessions[sid]
        while len(self._sessions) >= MAX_SESSIONS_TOTAL:
            self._sessions.popitem(last=False)
        self._sessions[session_id] = Session(
            user,
            tuple(guilds),
            created_at=now,
            last_seen=now,
            consented_at=now if consented else None,
            key_fingerprint=key_fingerprint,
        )

    def get(self, session_id: Optional[str]) -> Optional[Session]:
        """Sessão do cookie do painel genérico (uma sessão da API nunca sai por aqui)."""
        return self._lookup(session_id, None)

    def get_api(self, token: Optional[str], key_fingerprint: str) -> Optional[Session]:
        """Sessão da API pelo token; só vale para a chave de serviço que a criou (outra impressão = ausente)."""
        return self._lookup(_api_id(token) if token else None, key_fingerprint)

    def _lookup(self, session_id: Optional[str], key_fingerprint: Optional[str]) -> Optional[Session]:
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is None or session.key_fingerprint != key_fingerprint:
            return None
        now = self._clock()
        if self._is_expired(session, now):
            del self._sessions[session_id]
            return None
        session.last_seen = now
        return session

    def expires_at(self, session: Session) -> float:
        """O fim absoluto da sessão (TTL), no relógio do store; a ociosidade pode encerrá-la antes."""
        return session.created_at + self._ttl

    def destroy(self, session_id: Optional[str]) -> None:
        if session_id:
            self._sessions.pop(session_id, None)

    def destroy_api(self, token: Optional[str]) -> None:
        if token:
            self._sessions.pop(_api_id(token), None)

    def destroy_user(self, user_id: str) -> int:
        """Destrói todas as sessões do usuário (e só as dele); devolve quantas eram."""
        mine = [sid for sid, session in self._sessions.items() if session.user.id == str(user_id)]
        for sid in mine:
            del self._sessions[sid]
        return len(mine)

    def _is_expired(self, session: Session, now: float) -> bool:
        return now - session.created_at >= self._ttl or now - session.last_seen > self._idle

    def _sweep(self, now: float) -> None:
        if now < self._next_sweep:
            return
        self._next_sweep = now + SWEEP_INTERVAL_SECONDS
        for sid in [sid for sid, s in self._sessions.items() if self._is_expired(s, now)]:
            del self._sessions[sid]


def _api_id(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LoginAttempt:
    state: str
    code_verifier: str
    next_path: str
    expires_at: float
    consent: bool = False  # o login saiu com ``prompt=consent``: decidido no início, nunca relido do callback


class LoginAttemptStore:
    """Tentativas de login pendentes: uso único e validade de 10 minutos."""

    def __init__(self, clock: Clock = time.monotonic) -> None:
        self._clock = clock
        self._attempts: OrderedDict[str, LoginAttempt] = OrderedDict()

    def create(self, state: str, code_verifier: str, next_path: str, consent: bool = False) -> str:
        now = self._clock()
        while self._attempts and (
            len(self._attempts) >= MAX_PENDING_ATTEMPTS or next(iter(self._attempts.values())).expires_at <= now
        ):
            self._attempts.popitem(last=False)
        attempt_id = secrets.token_urlsafe(SESSION_ID_BYTES)
        self._attempts[attempt_id] = LoginAttempt(state, code_verifier, next_path, now + ATTEMPT_TTL_SECONDS, consent)
        return attempt_id

    def consume(self, attempt_id: Optional[str]) -> Optional[LoginAttempt]:
        """Remove e devolve a tentativa (None se inexistente, já usada ou expirada)."""
        if not attempt_id:
            return None
        attempt = self._attempts.pop(attempt_id, None)
        if attempt is None or attempt.expires_at <= self._clock():
            return None
        return attempt
