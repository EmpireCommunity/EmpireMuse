"""O filtro de IP dos links que o Lavalink vai buscar (``/play``, pedido digitado, painel, favoritos e playlists importadas).

O Lavalink faz a requisição de dentro da rede do bot, então um link para ``127.0.0.1``, para a rede interna, para o endereço de metadados
da nuvem ou para o relay local das rádios seria um SSRF. Antes de o link chegar ao Lavalink o nome é resolvido AQUI e TODO endereço
precisa ser público (``rules.playlists.is_public_address``: recusa privado, loopback, link-local, CGNAT, reservado, multicast e IPv4
dentro de IPv6 mapeado, 6to4, Teredo e NAT64). Falha de DNS, tempo esgotado e resposta vazia recusam (falha fechada).

O log leva só o host (nunca a URL inteira, que pode ter credencial em ``?token=``).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor

from empiremuse.rules.playlists import is_public_address
from empiremuse.rules.urls import lavalink_target

logger = logging.getLogger(__name__)

RESOLVE_TIMEOUT_S = 5.0
RESOLVER_THREADS = 4

# O DNS do sistema é bloqueante: roda numa fila PRÓPRIA e pequena, não no executor padrão do laço, onde um DNS lento seguraria o resto do bot
# (``asyncio.to_thread`` e demais). Se a fila enche, só este filtro espera.
_RESOLVER_POOL = ThreadPoolExecutor(max_workers=RESOLVER_THREADS, thread_name_prefix="url-guard-dns")

Resolve = Callable[[str, int], Awaitable[Sequence[str]]]


class UnsafeUrl(Exception):
    """O link aponta (ou pode apontar) para fora da internet pública. ``host`` é só o nome do host, já em minúsculas."""

    def __init__(self, host: str = "") -> None:
        super().__init__("unsafe url")
        self.host = host


async def system_resolve(host: str, port: int) -> list[str]:
    """Todos os endereços (A e AAAA) que o sistema dá para o nome."""
    infos = await asyncio.get_running_loop().run_in_executor(
        _RESOLVER_POOL, lambda: socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    )
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


async def ensure_public_url(
    query: object, *, resolve: Resolve = system_resolve, timeout: float = RESOLVE_TIMEOUT_S
) -> None:
    """Levanta ``UnsafeUrl`` se ``query`` é um link http(s) que não resolve SÓ para endereços públicos. Texto que não é link passa."""
    target = lavalink_target(query)
    if target is None:
        return
    if target.refused:
        raise UnsafeUrl(target.host)
    host = target.host
    if _is_ip_literal(host):
        if not is_public_address(host):
            raise UnsafeUrl(host)
        return
    try:
        ascii_host = host.encode("idna").decode("ascii")
        addresses = list(await asyncio.wait_for(resolve(ascii_host, 0), timeout))
    except (OSError, UnicodeError, TimeoutError) as error:
        raise UnsafeUrl(host) from error
    if not addresses or not all(is_public_address(address) for address in addresses):
        raise UnsafeUrl(
            host
        )  # um nome que também aponta para dentro (rebinding por várias respostas) é recusado inteiro


def log_refusal(error: UnsafeUrl) -> None:
    """O aviso para o log: só o host."""
    logger.warning("Link recusado pelo filtro de IP [%s]", error.host or "?")
