"""O logo de uma estação para o painel (item 8): logo via proxy do próprio painel, formatos raster, re-codificado.

O ``favicon`` de uma estação é uma URL que qualquer pessoa cadastra no Radio Browser, então o servidor só a toca nestas condições:

- **Quando:** só para um uuid que já está na memória (resultado de busca recente ou estação já vista) ou, para uma rádio configurada depois de um reinício,
  com UMA consulta ao diretório. Uuid desconhecido: sem rede. Uma busca de cada vez por uuid, no máximo 2 ao mesmo tempo e cerca de 1 por segundo; sem ficha
  a resposta sai na hora como "sem logo" (``transient``), sem fila.
- **Como:** a URL (``https``, porta 443, já limpa pelo ``favicon_thumbnail``) é resolvida UMA vez, todo endereço precisa ser público e fica fixado no conector;
  TLS conferido, sem redirecionamento, sem cookie nem proxy do ambiente, corpo sem compressão e contado até 256 KiB, 5 s no total.
- **O que:** só PNG, JPEG, GIF, WEBP e ICO, pela assinatura dos primeiros bytes (o ``Content-Type`` de fora é ignorado; SVG nunca). A decodificação roda
  num processo filho sem segredos e com limites (``logo_worker``), um por vez; o que sai é um PNG de até 128 px sem metadados, conferido pelo pai.
- **Depois:** acerto em cache com teto de entradas e de bytes; falha em cache por 1 h; o log leva só o código do motivo, nunca a URL.

Nada aqui levanta para quem chama: qualquer falha é ``LogoResult(None)`` (o painel mostra o ícone padrão).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import NamedTuple, Optional, Protocol

import aiohttp
import yarl
from cachetools import TTLCache

from empiremuse.rules.playlists import is_public_address
from empiremuse.rules.radio_stations import favicon_thumbnail, valid_uuid
from empiremuse.utils.server_playlists_fetch import PinnedResolver, system_resolve
from empiremuse.version import image_version

logger = logging.getLogger(__name__)

TOTAL_TIMEOUT_S = 5.0
CONNECT_TIMEOUT_S = 3.0
MAX_BODY_BYTES = 256 * 1024
MAX_OUT_BYTES = 128 * 1024
MAX_SIDE = 128
WORKER_TIMEOUT_S = 6.0
CACHE_BYTES = 8 * 1024 * 1024
CACHE_ENTRIES = 512
CACHE_TTL_S = 6 * 3600
FAILURE_TTL_S = 3600
FAILURE_ENTRIES = 4096
EGRESS_PARALLEL = 2
EGRESS_PER_SECOND = 1.0
READ_CHUNK = 8192
URL_LIMIT = 2048
SEARCH_RESERVE = 1.0

_WORKER = Path(__file__).with_name("logo_worker.py")
_PNG = b"\x89PNG\r\n\x1a\n"


class LogoResult(NamedTuple):
    """``png`` = PNG RGBA de até 128 px pronto para servir, ou ``None`` (ícone padrão). ``transient`` = não houve ficha nem espaço agora (não é falha da estação)."""

    png: Optional[bytes] = None
    transient: bool = False


class LogoError(Exception):
    """Uma falha ao buscar ou decodificar o logo; ``code`` é um código estável (nunca a URL nem texto de fora)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Source(Protocol):
    """O que o serviço pergunta ao diretório: a URL já validada que ele guardou e, para uma rádio configurada, uma consulta por uuid."""

    def favicon_of(self, uuid: str) -> Optional[str]: ...
    async def learn(self, uuid: str) -> None: ...


Fetch = Callable[[str], Awaitable[bytes]]
Decode = Callable[[str, bytes], Awaitable[bytes]]


# ---------------------------------------------------------------------- formato e URL


def detect_format(head: bytes) -> Optional[str]:
    """O formato pela assinatura dos primeiros bytes (PNG, JPEG, GIF, WEBP ou ICO); qualquer outra coisa, inclusive SVG, é ``None``."""
    if head.startswith(_PNG):
        return "PNG"
    if head.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "GIF"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "WEBP"
    if head[:4] == b"\x00\x00\x01\x00" and int.from_bytes(head[4:6], "little") > 0:
        return "ICO"
    return None


def logo_url(raw: object) -> Optional[yarl.URL]:
    """A URL do logo se ela serve: ``https``, porta 443, sem credencial, com host e passando pelo ``favicon_thumbnail``; senão ``None``."""
    checked = favicon_thumbnail(raw)
    if checked is None or len(checked) > URL_LIMIT:
        return None
    try:
        url = yarl.URL(checked)
    except ValueError:
        return None
    if url.scheme != "https" or not url.raw_host or url.user or url.password or url.explicit_port not in (None, 443):
        return None
    return url


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------- busca


class LogoFetcher:
    """Baixa o corpo do logo com os endereços fixados. ``scheme``/``port`` existem para os testes com servidor local."""

    def __init__(
        self,
        *,
        resolve: Callable[[str, int], Awaitable[Sequence[str]]] = system_resolve,
        public: Callable[[str], bool] = is_public_address,
        scheme: Optional[str] = None,
        port: Optional[int] = None,
    ) -> None:
        self._resolve, self._public, self._scheme, self._port = resolve, public, scheme, port

    async def __call__(self, raw: str) -> bytes:
        url = logo_url(raw)
        if url is None:
            raise LogoError("url")
        host = url.raw_host or ""
        try:
            addresses = [host] if _is_ip(host) else list(await self._resolve(host, 443))
        except OSError as error:
            raise LogoError("address") from error
        if not addresses or not all(self._public(a) for a in addresses):
            raise LogoError("address")
        if self._scheme is not None:
            url = url.with_scheme(self._scheme).with_port(self._port)
        connector = aiohttp.TCPConnector(
            resolver=PinnedResolver(host, addresses), use_dns_cache=False, limit=1, force_close=True
        )
        headers = {
            "User-Agent": f"EmpireMuse/{image_version() or 'dev'}",
            "Accept-Encoding": "identity",
            "Accept": "image/png,image/jpeg,image/gif,image/webp,image/x-icon",
        }
        async with aiohttp.ClientSession(
            connector=connector,
            trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            auto_decompress=False,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=TOTAL_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
        ) as session:
            async with session.get(url, allow_redirects=False) as response:
                if 300 <= response.status < 400:
                    raise LogoError("redirect")
                if response.status != 200:
                    raise LogoError("status")
                if response.headers.get("Content-Encoding", "identity").strip().lower() not in ("", "identity"):
                    raise LogoError("encoding")
                declared = response.headers.get("Content-Length", "")
                if declared.isdecimal() and int(declared) > MAX_BODY_BYTES:
                    raise LogoError("size")
                body = bytearray()
                async for chunk in response.content.iter_chunked(READ_CHUNK):
                    body += chunk
                    if len(body) > MAX_BODY_BYTES:  # o cabeçalho pode mentir ou faltar: conta o que chega
                        raise LogoError("size")
        return bytes(body)


# ---------------------------------------------------------------------- decodificação (processo filho)


def _checked_png(out: bytes) -> bytes:
    """O pai só confere a assinatura PNG, o tamanho e as dimensões declaradas; o resto é do filho, que não tem segredo nenhum."""
    if len(out) > MAX_OUT_BYTES or not out.startswith(_PNG) or out[12:16] != b"IHDR":
        raise LogoError("output")
    width, height = int.from_bytes(out[16:20], "big"), int.from_bytes(out[20:24], "big")
    if not (1 <= width <= MAX_SIDE and 1 <= height <= MAX_SIDE):
        raise LogoError("output")
    return out


async def decode_in_worker(fmt: str, data: bytes) -> bytes:
    """Decodifica ``data`` num processo filho (``python -I``, ``env={}``, limites de memória e CPU, prazo) e devolve o PNG pequeno. ``LogoError`` em qualquer falha."""
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            str(_WORKER),
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
        raise LogoError("worker") from error
    out = bytearray()
    try:
        async with asyncio.timeout(WORKER_TIMEOUT_S):
            assert process.stdin is not None and process.stdout is not None
            try:
                process.stdin.write(data)
                await process.stdin.drain()
                process.stdin.close()
            except BrokenPipeError, ConnectionResetError:
                pass  # o filho já saiu (formato quebrado): o código de saída conta a história
            while len(out) <= MAX_OUT_BYTES:
                chunk = await process.stdout.read(READ_CHUNK)
                if not chunk:
                    break
                out += chunk
            if len(out) > MAX_OUT_BYTES:
                raise LogoError("output")
            await process.wait()
    except TimeoutError as error:
        raise LogoError("deadline") from error
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode != 0:
        raise LogoError("decode")
    return _checked_png(bytes(out))


# ---------------------------------------------------------------------- o serviço


class LogoService:
    def __init__(
        self,
        source: Source,
        *,
        fetch: Optional[Fetch] = None,
        decode: Optional[Decode] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._source = source
        self._fetch: Fetch = fetch or LogoFetcher()
        self._decode: Decode = decode or decode_in_worker
        self._clock = clock
        self._hits: TTLCache = TTLCache(maxsize=CACHE_BYTES, ttl=CACHE_TTL_S, timer=clock, getsizeof=len)
        self._misses: TTLCache = TTLCache(maxsize=FAILURE_ENTRIES, ttl=FAILURE_TTL_S, timer=clock)
        self._flights: dict[str, asyncio.Future[LogoResult]] = {}
        self._inflight = 0
        self._tokens, self._at = EGRESS_PER_SECOND * 2, clock()
        self._worker_lock = asyncio.Lock()
        self.fetches = 0  # buscas de logo que de fato saíram (para os testes)

    def _admit(self) -> bool:
        """Saída global: no máximo ``EGRESS_PARALLEL`` ao mesmo tempo e cerca de uma por segundo (rajada de 2). Sem ficha, ``False`` na hora."""
        now = self._clock()
        self._tokens = min(EGRESS_PER_SECOND * 2, self._tokens + (now - self._at) * EGRESS_PER_SECOND)
        self._at = now
        if self._inflight >= EGRESS_PARALLEL or self._tokens < 1:
            return False
        self._tokens -= 1
        return True

    async def png(self, uuid: object, *, configured: bool = False) -> LogoResult:
        """O logo da estação ``uuid`` ou ``LogoResult(None)``. Nunca levanta. ``configured`` = a estação é de uma rádio configurada do servidor da sessão
        (permite UMA consulta ao diretório quando nada dela está na memória)."""
        if not isinstance(uuid, str) or not valid_uuid(uuid):
            return LogoResult()
        if (cached := self._hits.get(uuid)) is not None:
            return LogoResult(cached)
        if uuid in self._misses:
            return LogoResult()
        if self._source.favicon_of(uuid) is None and not configured:
            return LogoResult()  # estação que não está na memória: nenhum pedido por causa de um uuid de fora
        flight = self._flights.get(uuid)
        if flight is not None:
            return await asyncio.shield(flight)
        flight = asyncio.get_running_loop().create_future()
        self._flights[uuid] = flight
        try:
            result = await self._load(uuid, configured)
        except asyncio.CancelledError:
            flight.set_result(LogoResult())  # quem esperava o voo do outro não herda o cancelamento: recebe "sem logo"
            raise
        except Exception as error:  # noqa: BLE001 - quem chama nunca vê exceção; só o tipo vai ao log (F-7)
            logger.warning("Logo da estação não carregado (%s)", type(error).__name__)
            result = LogoResult()
        finally:
            self._flights.pop(uuid, None)
        if not flight.done():
            flight.set_result(result)
        return result

    async def _load(self, uuid: str, configured: bool) -> LogoResult:
        if not self._admit():
            return LogoResult(transient=True)
        self._inflight += 1
        try:
            url = self._source.favicon_of(uuid)
            if url is None:
                try:
                    await self._source.learn(uuid)
                except Exception:  # noqa: BLE001 - diretório fora do ar ou sem ficha: não é falha da estação, não entra no cache de falhas
                    return LogoResult(transient=True)
                url = self._source.favicon_of(uuid)
            if url is None:
                self._misses[uuid] = True
                return LogoResult()
            return await self._fetch_and_decode(uuid, url)
        finally:
            self._inflight -= 1

    async def _fetch_and_decode(self, uuid: str, url: str) -> LogoResult:
        try:
            self.fetches += 1
            body = await asyncio.wait_for(self._fetch(url), TOTAL_TIMEOUT_S + 1)
            fmt = detect_format(body[:16])
            if fmt is None:
                raise LogoError("format")
            async with self._worker_lock:  # um decodificador de cada vez
                png = await self._decode(fmt, body)
        except LogoError as error:
            return self._failed(uuid, error.code)
        except TimeoutError:
            return self._failed(uuid, "deadline")
        except OSError, aiohttp.ClientError:
            return self._failed(uuid, "network")
        self._hits[uuid] = png
        while len(self._hits) > CACHE_ENTRIES:
            self._hits.popitem()
        return LogoResult(png)

    def _failed(self, uuid: str, code: str) -> LogoResult:
        self._misses[uuid] = True
        logger.warning(
            "Logo da estação sem imagem (%s)", code
        )  # só o código: a URL pode ter credencial e é de terceiros
        return LogoResult()
