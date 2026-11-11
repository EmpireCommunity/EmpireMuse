"""``Idempotency-Key`` das escritas (contrato T-456 §4 m4f): memória, processo único, relógio injetável.

O escopo é usuário + chave; a impressão guardada junto cobre método, caminho (servidor e rota) e o corpo. A mesma chave
com outra impressão, ou ainda em andamento, é ``409``: nunca devolve a resposta de outro escopo. Só respostas de sucesso
são guardadas; um erro (inclusive ``5xx``, que não diz se a escrita ocorreu) libera a chave para a nova tentativa, e a
versão esperada (CAS) impede que ela grave duas vezes.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Callable, NamedTuple, Optional

TTL_SECONDS = 10 * 60
MAX_KEYS_PER_USER = 100
MAX_KEYS_TOTAL = 2000
MAX_STORED_BYTES = 32 * 1024
KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,64}", re.ASCII)
SUCCESS_BELOW = 300


class Reply(NamedTuple):
    """Uma resposta pronta para sair (e, se for sucesso, para ser repetida): status e corpo JSON (``None`` = sem corpo)."""

    status: int
    body: Optional[dict[str, Any]]


class Ticket(NamedTuple):
    user_id: str
    key: str
    serial: int


class Claim(NamedTuple):
    replay: Optional[Reply]  # já respondida: repita
    ticket: Optional[Ticket]  # nova: execute e depois ``settle``


class IdempotencyConflict(Exception):
    """A chave já foi usada com outro pedido, ou o pedido original ainda está em andamento."""


def request_fingerprint(method: str, path: str, body: Any) -> str:
    """Método, caminho (que leva o servidor e a rota) e o corpo em JSON canônico: a ordem das chaves não importa."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{method}\n{path}\n{canonical}".encode()).hexdigest()


@dataclass
class _Entry:
    fingerprint: str
    expires_at: float
    serial: int
    reply: Optional[Reply] = None  # ``None`` = em andamento


class IdempotencyStore:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._entries: OrderedDict[tuple[str, str], _Entry] = OrderedDict()  # em ordem de criação = de vencimento
        self._user_keys: dict[str, deque[str]] = {}
        self._serial = 0

    def claim(self, user_id: str, key: str, fingerprint: str) -> Claim:
        now = self._clock()
        self._sweep(now)
        entry = self._entries.get((user_id, key))
        if entry is not None:
            if entry.fingerprint != fingerprint or entry.reply is None:
                raise IdempotencyConflict
            return Claim(entry.reply, None)
        self._make_room(user_id)
        self._serial += 1
        self._entries[(user_id, key)] = _Entry(fingerprint, now + TTL_SECONDS, self._serial)
        self._user_keys.setdefault(user_id, deque()).append(key)
        return Claim(None, Ticket(user_id, key, self._serial))

    def settle(self, ticket: Ticket, reply: Optional[Reply]) -> None:
        """Fecha o pedido: guarda só o sucesso (e que caiba); qualquer outra coisa libera a chave."""
        entry = self._entries.get((ticket.user_id, ticket.key))
        if entry is None or entry.serial != ticket.serial:
            return  # a vaga foi tomada (teto) ou venceu enquanto o pedido rodava
        if reply is None or reply.status >= SUCCESS_BELOW or _too_big(reply):
            self._drop((ticket.user_id, ticket.key))
            return
        entry.reply = reply

    def purge_user(self, user_id: str) -> None:
        """Descarta tudo do usuário (apagamento de dados): respostas guardadas e pedidos em andamento. Um pedido que fecha
        depois não encontra a entrada e não guarda nada."""
        for key in list(self._user_keys.get(user_id, ())):
            self._drop((user_id, key))

    def _sweep(self, now: float) -> None:
        while self._entries:
            oldest = next(iter(self._entries))
            if self._entries[oldest].expires_at > now:
                return
            self._drop(oldest)

    def _make_room(self, user_id: str) -> None:
        keys = self._user_keys.get(user_id)
        while keys and len(keys) >= MAX_KEYS_PER_USER:
            self._drop((user_id, keys[0]))
            keys = self._user_keys.get(user_id)
        while len(self._entries) >= MAX_KEYS_TOTAL:
            self._drop(next(iter(self._entries)))

    def _drop(self, entry_key: tuple[str, str]) -> None:
        self._entries.pop(entry_key, None)
        user_id, key = entry_key
        keys = self._user_keys.get(user_id)
        if keys is None:
            return
        if key in keys:
            keys.remove(key)
        if not keys:
            del self._user_keys[user_id]


def _too_big(reply: Reply) -> bool:
    return reply.body is not None and len(json.dumps(reply.body)) > MAX_STORED_BYTES
