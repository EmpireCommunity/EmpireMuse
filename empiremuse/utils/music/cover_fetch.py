"""A busca da capa de uma faixa, no molde do ``LogoFetcher`` (``utils/radio/logo.py``). A URL é de terceiro.

Em ordem, e qualquer falha é ``CoverError(code)`` com um código estável (nunca a URL):

1. ``cover_url``: só ``https``, porta 443 ou ausente, host presente, sem usuário/senha, até 2048 caracteres antes e depois do
   parse por ``yarl``, UMA vez; o mesmo ``yarl.URL`` vai na requisição e o ``sha256`` dele é a chave do cache.
2. Host fora de ``COVER_HOSTS``: recusa sem resolver nem conectar.
3. Resolução única (A e AAAA, pela fila própria do ``url_guard.system_resolve``, com prazo); TODO endereço precisa ser público.
4. Conector com os endereços fixados (``PinnedResolver``), sem cache de DNS, uma conexão, fechada no fim; sem proxy do ambiente,
   sem cookie, TLS conferido; sem redirecionamento (3xx = falha).
5. Corpo sem compressão (``Accept-Encoding: identity``), contado em pedaços até 2 MiB; só status 200; 5 s no total.
"""

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import Awaitable, Callable, Sequence
from typing import Optional

import aiohttp
import yarl

from empiremuse.rules.cover import (
    COVER_CONNECT_TIMEOUT_S,
    COVER_MAX_BYTES,
    COVER_READ_CHUNK,
    COVER_RESOLVE_TIMEOUT_S,
    COVER_TOTAL_TIMEOUT_S,
    COVER_URL_MAX,
    HostRule,
    host_allowed,
)
from empiremuse.rules.playlists import is_public_address
from empiremuse.utils.server_playlists_fetch import PinnedResolver
from empiremuse.utils.url_guard import system_resolve
from empiremuse.version import image_version

Resolve = Callable[[str, int], Awaitable[Sequence[str]]]
HTTPS_PORT = 443


class CoverError(Exception):
    """Falha ao buscar ou decodificar a capa; ``code`` está em ``rules.cover.REASONS`` (nunca a URL nem texto de fora)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def cover_url(raw: object) -> Optional[yarl.URL]:
    """A URL da capa se ela serve para buscar; senão ``None``. ``http`` nunca vira ``https``."""
    if not isinstance(raw, str) or not raw or len(raw) > COVER_URL_MAX:
        return None
    try:
        url = yarl.URL(raw)
    except ValueError, TypeError:
        return None
    if (
        len(str(url)) > COVER_URL_MAX  # o ``yarl`` codifica (``%XX``): o texto que sai na requisição também tem o teto
        or url.scheme != "https"
        or not url.raw_host
        or url.user is not None
        or url.password is not None
        or url.explicit_port not in (None, HTTPS_PORT)
    ):
        return None
    return url


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


class CoverFetcher:
    """Baixa o corpo da capa com os endereços fixados. ``scheme``/``port`` existem só para os testes com servidor local."""

    def __init__(
        self,
        hosts: HostRule,
        *,
        resolve: Resolve = system_resolve,
        public: Callable[[str], bool] = is_public_address,
        resolve_timeout: float = COVER_RESOLVE_TIMEOUT_S,
        scheme: Optional[str] = None,
        port: Optional[int] = None,
    ) -> None:
        self._hosts = hosts
        self._resolve, self._public, self._resolve_timeout = resolve, public, resolve_timeout
        self._scheme, self._port = scheme, port

    async def _addresses(self, host: str) -> list[str]:
        if _is_ip(host.strip("[]")):
            return [host.strip("[]")]
        try:
            return list(await asyncio.wait_for(self._resolve(host, HTTPS_PORT), self._resolve_timeout))
        except (OSError, UnicodeError, TimeoutError) as error:
            raise CoverError("address") from error

    async def __call__(self, url: yarl.URL) -> bytes:
        host = url.raw_host or ""
        if not host or not host_allowed(self._hosts, host):
            raise CoverError("host")
        addresses = await self._addresses(host)
        if not addresses or not all(self._public(a) for a in addresses):
            raise CoverError("address")
        target = url if self._scheme is None else url.with_scheme(self._scheme).with_port(self._port)
        connector = aiohttp.TCPConnector(
            resolver=PinnedResolver(host, addresses), use_dns_cache=False, limit=1, force_close=True
        )
        headers = {
            "User-Agent": f"EmpireMuse/{image_version() or 'dev'}",
            "Accept-Encoding": "identity",
            "Accept": "image/jpeg,image/png,image/webp",
        }
        async with aiohttp.ClientSession(
            connector=connector,
            trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            auto_decompress=False,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=COVER_TOTAL_TIMEOUT_S, connect=COVER_CONNECT_TIMEOUT_S),
        ) as session:
            async with session.get(target, allow_redirects=False) as response:
                return await _read_body(response)


async def _read_body(response: aiohttp.ClientResponse) -> bytes:
    if 300 <= response.status < 400:
        raise CoverError("redirect")
    if response.status != 200:
        raise CoverError("status")
    if response.headers.get("Content-Encoding", "identity").strip().lower() not in ("", "identity"):
        raise CoverError("encoding")
    declared = response.headers.get("Content-Length", "")
    if declared.isdecimal() and int(declared) > COVER_MAX_BYTES:
        raise CoverError("size")
    body = bytearray()
    async for chunk in response.content.iter_chunked(COVER_READ_CHUNK):
        body += chunk
        if len(body) > COVER_MAX_BYTES:  # o cabeçalho pode mentir ou faltar: conta o que chega
            raise CoverError("size")
    return bytes(body)
