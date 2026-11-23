"""A capa de uma faixa para as skins de imagem: busca limitada, decodificação isolada e cache por bytes. Nunca levanta.

- **Quem pede:** o render, FORA da vaga de desenho, com prazo (``COVER_WAIT_S``). Passou do prazo: ``CoverResult(None, pending=True)``
  (o cartão sai com o logo e o próximo ciclo do updater redesenha) e a busca segue em voo, sem fila nova.
- **Saída de rede:** no máximo 2 buscas ao mesmo tempo e um balde de cerca de 2 por segundo; sem ficha, logo agora (``pending``).
  Uma busca por URL exata (*single-flight*). Falha em cache negativo por 1 h, com teto de entradas.
- **Decodificação:** um processo filho por capa (``cover_worker.py``), um de cada vez, sem ambiente e com limites; o pai recebe
  só 240 × 240 pixels RGB crus e confere o tamanho. Nenhum decodificador roda aqui sobre bytes de fora.
- **Cache:** ``LRUCache`` limitado por bytes (8 MiB), só os 172.800 bytes de cada capa. Chave (dos dois caches e dos voos) =
  ``sha256`` da URL exata depois do ``yarl`` (32 bytes, qualquer que seja o tamanho da URL); texto inválido só pelo digest.
- **Log:** só o código do motivo (``rules.cover.REASONS``). Nada de URL, query, ``repr`` de exceção nem ``exc_info``.
- **Call privada e modo R:** o ``CardSpec`` já vem sem ``cover``: ``None`` aqui não custa rede nem entrada de cache.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, NamedTuple, Optional

import aiohttp
import yarl
from cachetools import LRUCache, TTLCache

from empiremuse.rules.cover import (
    COVER_CACHE_BYTES,
    COVER_EGRESS_PARALLEL,
    COVER_EGRESS_PER_SECOND,
    COVER_FAILURE_ENTRIES,
    COVER_FAILURE_TTL_S,
    COVER_FORMATS,
    COVER_RAW_BYTES,
    COVER_READ_CHUNK,
    COVER_TOTAL_TIMEOUT_S,
    COVER_WAIT_S,
    COVER_WORKER_TIMEOUT_S,
    HostRule,
    host_allowed,
    parse_cover_hosts,
)
from empiremuse.utils import sandbox
from empiremuse.utils.music.cover_fetch import CoverError, CoverFetcher, cover_url
from empiremuse.utils.radio.logo import detect_format

logger = logging.getLogger(__name__)

_WORKER = Path(__file__).with_name("cover_worker.py")

Fetch = Callable[[yarl.URL], Awaitable[bytes]]
Decode = Callable[[str, bytes], Awaitable[bytes]]
Spawn = Callable[..., Awaitable[Any]]


class CoverResult(NamedTuple):
    """``pixels`` = 240 × 240 RGB crus, ou ``None`` (logo). ``pending`` = ainda pode chegar: redesenhar no próximo ciclo."""

    pixels: Optional[bytes] = None
    pending: bool = False


# ---------------------------------------------------------------------- decodificação (processo filho)


async def decode_in_worker(
    fmt: str,
    data: bytes,
    *,
    spawn: Spawn = asyncio.create_subprocess_exec,
    timeout_s: float = COVER_WORKER_TIMEOUT_S,
    worker: Optional[Path] = None,
) -> bytes:
    """Os pixels crus da capa, decodificada num processo filho isolado (sandbox dos decodificadores). ``CoverError`` em
    qualquer falha (``sandbox`` sem o sandbox, nos dois lados); o filho que passa do prazo é morto com o grupo e o ``returncode``
    dele é coletado."""
    if not sandbox.may_spawn(logger):
        raise CoverError("sandbox")
    try:
        process = await spawn(
            sys.executable,
            "-I",
            str(worker or _WORKER),
            fmt,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={},
            cwd="/",
            close_fds=True,
            start_new_session=True,
        )
    except OSError as error:
        raise CoverError("decode") from error
    out = bytearray()
    try:
        async with asyncio.timeout(timeout_s):
            try:
                process.stdin.write(data)
                await process.stdin.drain()
                process.stdin.close()
            except BrokenPipeError, ConnectionResetError:
                pass  # o filho já saiu (formato quebrado): o código de saída conta a história
            while len(out) <= COVER_RAW_BYTES:
                chunk = await process.stdout.read(COVER_READ_CHUNK)
                if not chunk:
                    break
                out += chunk
            if len(out) > COVER_RAW_BYTES:
                raise CoverError("decode")
            await process.wait()
    except TimeoutError as error:
        raise CoverError("deadline") from error
    finally:
        if process.returncode is None:
            sandbox.kill_worker(process)  # o grupo inteiro (``start_new_session``), não só o PID
            await process.wait()
    if sandbox.refused(process.returncode, logger):
        raise CoverError("sandbox")
    if process.returncode != 0 or len(out) != COVER_RAW_BYTES:
        raise CoverError("decode")
    return bytes(out)


# ---------------------------------------------------------------------- o serviço


def _sandbox_ready() -> bool:
    return sandbox.may_spawn(logger)


def _always() -> bool:
    return True


def cover_key(text: str) -> bytes:
    """A chave de cache de uma URL exata (ou de um texto inválido): 32 bytes, sem guardar o texto de fora."""
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).digest()


class CoverService:
    def __init__(
        self,
        hosts: HostRule,
        *,
        fetch: Optional[Fetch] = None,
        decode: Optional[Decode] = None,
        clock: Callable[[], float] = time.monotonic,
        ready: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._hosts = hosts
        self._fetch: Fetch = fetch or CoverFetcher(hosts)
        self._decode: Decode = decode or decode_in_worker
        # o decodificador pode rodar aqui? Conferido ANTES de buscar a imagem; com outro ``decode`` (testes), sempre
        self._ready: Callable[[], bool] = ready or (_sandbox_ready if decode is None else _always)
        self._clock = clock
        self._pixels: LRUCache[bytes, bytes] = LRUCache(maxsize=COVER_CACHE_BYTES, getsizeof=len)
        self._misses: TTLCache[bytes, bool] = TTLCache(
            maxsize=COVER_FAILURE_ENTRIES, ttl=COVER_FAILURE_TTL_S, timer=clock
        )
        self._flights: dict[bytes, asyncio.Task[Optional[bytes]]] = {}
        self._inflight = 0
        self._tokens, self._at = COVER_EGRESS_PER_SECOND, clock()
        self._worker_lock = asyncio.Lock()
        self.fetches = 0  # buscas que de fato saíram (para os testes)

    @classmethod
    def from_config(cls, value: object, **kwargs: Any) -> "CoverService":
        """``value`` = ``COVER_HOSTS`` já validado pelo ``config_loader``."""
        return cls(parse_cover_hosts(value), **kwargs)

    @property
    def cached_bytes(self) -> int:
        return int(self._pixels.currsize)

    def _admit(self) -> bool:
        """No máximo ``COVER_EGRESS_PARALLEL`` ao mesmo tempo e cerca de ``COVER_EGRESS_PER_SECOND`` (rajada de 2)."""
        now = self._clock()
        burst = COVER_EGRESS_PER_SECOND
        self._tokens = min(burst, self._tokens + (now - self._at) * COVER_EGRESS_PER_SECOND)
        self._at = now
        if self._inflight >= COVER_EGRESS_PARALLEL or self._tokens < 1:
            return False
        self._tokens -= 1
        return True

    async def pixels(self, raw: object, *, wait_s: float = COVER_WAIT_S) -> CoverResult:
        """A capa de ``raw`` (a URL do ``CardSpec``) em até ``wait_s`` segundos, ou o logo. Nunca levanta."""
        if raw is None:
            return CoverResult()
        url = cover_url(raw)
        if url is None:
            if not isinstance(raw, str):
                return CoverResult()  # nem texto: não vira entrada de cache
            bad = cover_key(raw)
            if bad in self._misses:
                return CoverResult()
            return self._failed(bad, "url")  # lembrada por 1 h (só o digest): um redesenho repetido não repete o log
        key = cover_key(str(url))
        if (cached := self._pixels.get(key)) is not None:
            return CoverResult(cached)
        if key in self._misses:
            return CoverResult()
        if not host_allowed(self._hosts, url.raw_host or ""):
            return self._failed(key, "host")
        flight = self._flights.get(key)
        if flight is None:
            if not self._ready():
                return CoverResult()  # sem sandbox: nem busca, nem gasta ficha, nem entra no cache de falhas
            if not self._admit():
                return CoverResult(pending=True)
            self._inflight += 1  # conta na admissão: uma 3ª busca nunca entra antes de a tarefa começar a rodar
            flight = asyncio.get_running_loop().create_task(self._load(key, url))
            self._flights[key] = flight
            flight.add_done_callback(lambda _done, key=key: self._landed(key))
        try:
            pixels = await asyncio.wait_for(asyncio.shield(flight), wait_s)
        except TimeoutError:
            return CoverResult(pending=True)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if flight.cancelled() and (current is None or current.cancelling() == 0):
                return CoverResult()  # o voo foi cancelado (desligamento): quem esperava não herda o cancelamento
            raise
        return CoverResult(pixels)

    async def aclose(self) -> None:
        """Cancela os voos em andamento e espera cada um terminar (o worker de cada um é morto e coletado). O dono chama ao
        descarregar; quem esperava um voo cancelado recebe o logo."""
        flights = list(self._flights.values())
        for flight in flights:
            flight.cancel()
        await asyncio.gather(*flights, return_exceptions=True)

    def _landed(self, key: bytes) -> None:
        """O voo terminou (com capa, falha ou cancelamento): libera a vaga de saída e a chave."""
        self._inflight -= 1
        self._flights.pop(key, None)

    async def _load(self, key: bytes, url: yarl.URL) -> Optional[bytes]:
        try:
            return await self._fetch_and_decode(key, url)
        except Exception:  # noqa: BLE001 - quem pede nunca vê exceção; só o código fixo vai ao log
            return self._failed(key, "internal").pixels

    async def _fetch_and_decode(self, key: bytes, url: yarl.URL) -> Optional[bytes]:
        try:
            self.fetches += 1
            body = await asyncio.wait_for(self._fetch(url), COVER_TOTAL_TIMEOUT_S + 1)
            fmt = detect_format(body[:16])
            if fmt not in COVER_FORMATS:
                raise CoverError("format")
            async with self._worker_lock:  # um decodificador de cada vez
                pixels = await self._decode(fmt, body)
            if len(pixels) != COVER_RAW_BYTES:
                raise CoverError("decode")
        except CoverError as error:
            return self._failed(key, error.code).pixels
        except TimeoutError:
            return self._failed(key, "deadline").pixels
        except OSError, aiohttp.ClientError:
            return self._failed(key, "network").pixels
        try:
            self._pixels[key] = pixels
        except ValueError:  # maior que o teto inteiro do cache: não guarda, segue
            pass
        return pixels

    def _failed(self, key: bytes, code: str) -> CoverResult:
        self._misses[key] = True
        logger.warning("Capa sem imagem (%s)", code)  # só o código: a URL é de terceiro e pode ter credencial
        return CoverResult()
