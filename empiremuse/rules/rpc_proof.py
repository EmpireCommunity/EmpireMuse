# -*- coding: utf-8 -*-
"""Regras puras da prova do token no registro do relé RPC: a chave por IP e o orçamento de provas negadas.

Só biblioteca padrão e relógio injetável. A chave é só o IP (nunca o ID nem o token do registro).
"""

from __future__ import annotations

import ipaddress
from collections import OrderedDict
from collections.abc import Callable
from typing import Optional

IPV6_PREFIX_BITS = 64


def proof_ip_key(remote_ip: Optional[str], trusted: bool) -> Optional[str]:
    """A chave do orçamento de provas negadas: o IP (IPv4) ou o prefixo ``/64`` (IPv6); ``None`` se o IP não serve de chave.

    ``None`` para texto que não é um IP simples (par não lido, lixo, endereço com escopo) e, sem proxy confiável, para IP que
    não é público: o IP do par de uma rede privada seria o do proxy e juntaria todos os visitantes num balde só.
    """
    if not remote_ip:
        return None
    try:
        address = ipaddress.ip_address(remote_ip.strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.scope_id:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = (
            address.ipv4_mapped
        )  # como em ``client_ip``: o par IPv4 mapeado é o IPv4, senão todos cairiam em ``::/64``
    if not (trusted or address.is_global):
        return None
    if isinstance(address, ipaddress.IPv6Address):
        return str(ipaddress.ip_network((address, IPV6_PREFIX_BITS), strict=False))
    return str(address)


class DeniedBudget:
    """Conta as provas NEGADAS por chave numa janela deslizante, com no máximo ``maxsize`` chaves (a mais antiga sai primeiro).

    ``exhausted`` só lê; ``record`` conta. Quem chama conta só a prova negada, nunca a indisponível.
    """

    def __init__(self, limit: int, window: float, maxsize: int, timer: Callable[[], float]):
        self.limit = limit
        self.window = window
        self.maxsize = maxsize
        self._timer = timer
        self._stamps: OrderedDict[str, tuple[float, ...]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._stamps)

    def _recent(self, key: str, now: float) -> tuple[float, ...]:
        return tuple(stamp for stamp in self._stamps.get(key, ()) if now - stamp < self.window)

    def exhausted(self, key: str) -> bool:
        return len(self._recent(key, self._timer())) >= self.limit

    def record(self, key: str) -> None:
        now = self._timer()
        stamps = (*self._recent(key, now), now)[-self.limit :]
        self._stamps.pop(key, None)
        self._stamps[key] = stamps
        while len(self._stamps) > self.maxsize:
            self._stamps.popitem(last=False)


class LogThrottle:
    """No máximo um aviso por ``interval`` segundos; o aviso que passa leva a contagem dos que foram suprimidos."""

    def __init__(self, interval: float, timer: Callable[[], float]):
        self.interval = interval
        self._timer = timer
        self._last: Optional[float] = None
        self._suppressed = 0

    def take(self) -> Optional[int]:
        """``None`` se este aviso deve ser suprimido; senão quantos foram suprimidos desde o último que passou."""
        now = self._timer()
        if self._last is not None and now - self._last < self.interval:
            self._suppressed += 1
            return None
        suppressed, self._suppressed, self._last = self._suppressed, 0, now
        return suppressed
