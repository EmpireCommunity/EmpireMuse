"""O cliente do Radio Browser (item 8, R3, R-7): um diretório público onde QUALQUER pessoa cadastra estação, então tudo que volta é hostil.

- **Espelhos:** descobertos por DNS em ``all.api.radio-browser.info`` (A, depois PTR de cada IP), em ordem embaralhada. O nome que vai no TLS
  precisa casar ``^[a-z0-9-]+\\.api\\.radio-browser\\.info$`` e o certificado é conferido; o PTR (ou o nome) fora do padrão é descartado. Nunca
  ``ssl=False``, nunca conexão por IP com SNI inventado: a sessão fixa os IPs conferidos (todos públicos) e mantém o NOME para SNI e certificado.
- **Requisição:** sem redirecionamento, ``trust_env=False``, sem cookie, sem descompressão, 8 s no total, corpo cortado em 256 KiB, ``User-Agent:
  EmpireMuse/<versão>``, URL montada pelo ``yarl`` (nunca por concatenação).
- **Busca:** ``/json/stations/search`` com ``hidebroken=true`` e ``limit=25``; termo de 2 a 64 caracteres. Cada item passa por ``check_station`` e
  o que falha é descartado em silêncio (um item ruim não derruba a lista).
- **Limites (R-3):** token bucket global de 2 req/s e LRU de até 500 consultas, 10 min cada.
- **Play:** a rádio guarda só o ``uuid``. Cada play pede ``/json/url/{uuid}`` (que registra o clique) e o resultado passa DE NOVO por ``check_station``.
  O health check usa ``/json/stations/byuuid``, que não registra clique (R-10).
- Qualquer falha (rede, TLS, JSON, tamanho) devolve vazio/``None``: o diretório é conveniência, nunca uma dependência que derrube a rádio.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import socket
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import aiohttp
import yarl
from cachetools import TTLCache

from empiremuse.rules.errors import RuleError
from empiremuse.rules.playlists import is_public_address
from empiremuse.rules.radio_stations import Station, check_search_term, check_station, valid_mirror, valid_uuid
from empiremuse.utils.server_playlists_fetch import PinnedResolver
from empiremuse.version import image_version

logger = logging.getLogger(__name__)

DISCOVERY_NAME = "all.api.radio-browser.info"
SEARCH_LIMIT = 25
TOTAL_TIMEOUT_S = 8.0
CONNECT_TIMEOUT_S = 4.0
MAX_BODY_BYTES = 256 * 1024
CACHE_SIZE = 500
CACHE_TTL_S = 600
MIRROR_TTL_S = 3600
MIRROR_RETRY_S = 60.0  # descoberta que falhou (ou veio vazia) não se repete antes disto
DISCOVERY_TIMEOUT_S = 5.0
MIRRORS_PER_REQUEST = 3
SEARCH_RESERVE = 1.0  # a busca deixa sempre uma ficha do balde global para o play e o health check
SEARCH_PER_KEY_RATE = 0.5  # por servidor: uma busca nova a cada 2 s, com rajada de 3
SEARCH_PER_KEY_BURST = 3
MIRROR_CANDIDATES = 12  # quantos IPs do nome de descoberta se resolvem ao contrário
RATE_PER_S = 2.0
CHUNK = 8192

Fetcher = Callable[[str, str, dict[str, str]], Awaitable[Any]]
MirrorSource = Callable[[], Awaitable[list[str]]]


class DirectoryError(Exception):
    """Uma falha do diretório (rede, TLS, tamanho, JSON); ``reason`` é um código estável, nunca o texto vindo de fora."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TokenBucket:
    """Token bucket: ``rate`` por segundo, rajada de até ``capacity``. O relógio vem de fora."""

    def __init__(
        self, rate: float = RATE_PER_S, capacity: float = RATE_PER_S, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.rate, self.capacity, self._clock = rate, capacity, clock
        self._tokens = capacity
        self._at = clock()

    def try_acquire(self, reserve: float = 0.0) -> bool:
        """Pega uma ficha se sobrar ao menos ``reserve`` depois dela (a reserva é de quem tem prioridade)."""
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._at) * self.rate)
        self._at = now
        if self._tokens >= 1 + reserve:
            self._tokens -= 1
            return True
        return False


async def discover_mirrors() -> list[str]:
    """Os espelhos de verdade: A de ``all.api...``, PTR de cada IP, só o que casa o padrão (o resto é descartado), sem repetir."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(DISCOVERY_NAME, 443, type=socket.SOCK_STREAM)
    names: list[str] = []
    for address in list(dict.fromkeys(str(info[4][0]) for info in infos))[:MIRROR_CANDIDATES]:
        try:
            host, _, _ = await loop.run_in_executor(None, socket.gethostbyaddr, address)
        except OSError:
            continue
        if valid_mirror(host) and host not in names:
            names.append(host)
    return names


class AiohttpJsonFetcher:
    """Busca um JSON de um espelho com os IPs fixados. ``scheme``/``port`` existem para os testes com servidor local."""

    def __init__(
        self,
        *,
        resolve: Callable[[str, int], Awaitable[Sequence[str]]] | None = None,
        public: Callable[[str], bool] = is_public_address,
        scheme: str = "https",
        port: int | None = None,
        timeout: float = TOTAL_TIMEOUT_S,
    ) -> None:
        self._resolve = resolve or _system_resolve
        self._public = public
        self._scheme = scheme
        self._port = port
        self._timeout = timeout

    async def __call__(self, mirror: str, path: str, query: dict[str, str]) -> Any:
        if not valid_mirror(mirror):
            raise DirectoryError("mirror")
        port = self._port or 443
        addresses = list(await self._resolve(mirror, port))
        if not addresses or not all(self._public(a) for a in addresses):
            raise DirectoryError("address")
        url = yarl.URL.build(scheme=self._scheme, host=mirror, port=self._port, path=path, query=query)
        version = image_version()
        connector = aiohttp.TCPConnector(
            resolver=PinnedResolver(mirror, addresses), use_dns_cache=False, limit=1, force_close=True
        )
        async with aiohttp.ClientSession(
            connector=connector,
            trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            auto_decompress=False,
            headers={
                "User-Agent": f"EmpireMuse/{version or 'dev'}",
                "Accept-Encoding": "identity",
                "Accept": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=self._timeout, connect=CONNECT_TIMEOUT_S),
        ) as session:
            async with session.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    raise DirectoryError("status")
                if response.headers.get("Content-Encoding", "identity").strip().lower() not in ("", "identity"):
                    raise DirectoryError("encoding")
                declared = response.headers.get("Content-Length", "")
                if declared.isdecimal() and int(declared) > MAX_BODY_BYTES:
                    raise DirectoryError("size")
                body = bytearray()
                async for chunk in response.content.iter_chunked(CHUNK):
                    body += chunk
                    if len(body) > MAX_BODY_BYTES:  # o cabeçalho pode mentir ou faltar: conta o que chega
                        raise DirectoryError("size")
        try:
            return json.loads(bytes(body))
        except (ValueError, RecursionError) as error:  # "[" * 100000 estoura a recursão; lixo não é JSON
            raise DirectoryError("json") from error


async def _system_resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


class Directory:
    def __init__(
        self,
        *,
        fetcher: Fetcher | None = None,
        mirrors: MirrorSource = discover_mirrors,
        bucket: TokenBucket | None = None,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self._fetch = fetcher or AiohttpJsonFetcher()
        self._mirror_source = mirrors
        self._bucket = bucket or TokenBucket(clock=clock)
        self._clock = clock
        self._rng = rng or random.Random()
        self._cache: TTLCache = TTLCache(maxsize=CACHE_SIZE, ttl=CACHE_TTL_S, timer=clock)
        self._mirrors: list[str] = []
        self._mirrors_at: float | None = None
        self._mirror_lock = asyncio.Lock()
        self._key_buckets: TTLCache = TTLCache(maxsize=CACHE_SIZE, ttl=CACHE_TTL_S, timer=clock)
        self.requests = 0  # as que de fato saíram (para os testes e para o resumo)

    # ------------------------------------------------------------------ espelhos e requisição

    async def _mirror_list(self) -> list[str]:
        """Os espelhos conhecidos. Uma descoberta por vez (trava), com prazo de 5 s, e a que falhou ou veio vazia só se repete após 1 min: um
        DNS lento não vira uma descoberta nova por tecla do autocomplete, prendendo as threads do executor."""
        async with self._mirror_lock:
            now = self._clock()
            ttl = MIRROR_TTL_S if self._mirrors else MIRROR_RETRY_S
            if self._mirrors_at is None or now - self._mirrors_at > ttl:
                try:
                    found = [
                        m for m in await asyncio.wait_for(self._mirror_source(), DISCOVERY_TIMEOUT_S) if valid_mirror(m)
                    ]
                except Exception as error:  # noqa: BLE001 - só o tipo vai ao log (R-12)
                    logger.warning("Espelhos do Radio Browser não descobertos (%s)", type(error).__name__)
                    found = []
                self._rng.shuffle(found)
                self._mirrors, self._mirrors_at = found, now
            return list(self._mirrors)

    async def _get_strict(self, path: str, query: dict[str, str], *, reserve: float = 0.0) -> Any:
        """Pede a um dos espelhos (até 3, em ordem embaralhada). ``DirectoryError`` se o bucket global está vazio ou nenhum espelho respondeu."""
        if not self._bucket.try_acquire(reserve):
            raise DirectoryError("throttled")
        for mirror in (await self._mirror_list())[:MIRRORS_PER_REQUEST]:
            self.requests += 1
            try:
                return await self._fetch(mirror, path, query)
            except DirectoryError as error:
                logger.warning("Radio Browser: espelho falhou (%s)", error.reason)
            except Exception as error:  # noqa: BLE001
                logger.warning("Radio Browser: espelho falhou (%s)", type(error).__name__)
        raise DirectoryError("unavailable")

    async def _get(self, path: str, query: dict[str, str], *, reserve: float = 0.0) -> Any | None:
        """Como ``_get_strict``, mas qualquer falha vira ``None`` (busca e autocomplete nunca levantam)."""
        try:
            return await self._get_strict(path, query, reserve=reserve)
        except DirectoryError:
            return None

    @staticmethod
    def _stations(payload: Any) -> list[Station]:
        """Os itens aceitos de uma lista; o que não é lista ou não passa em ``check_station`` é descartado em silêncio."""
        if not isinstance(payload, list):
            return []
        found: list[Station] = []
        for item in payload[: SEARCH_LIMIT * 2]:
            station = check_station(item)
            if not isinstance(station, RuleError):
                found.append(station)
            if len(found) >= SEARCH_LIMIT:
                break
        return found

    # ------------------------------------------------------------------ API

    async def search(self, term: object, *, key: object = None) -> list[Station]:
        """Até 25 estações para o termo (2 a 64 caracteres). Termo ruim, bucket vazio ou falha do diretório: lista vazia."""
        checked = check_search_term(term)
        if isinstance(checked, RuleError):
            return []
        cache_key = checked.casefold()
        if cache_key in self._cache:
            return list(self._cache[cache_key])
        if (
            key is not None
        ):  # quem digita no autocomplete é limitado por servidor; o balde global não se esvazia por um admin só
            bucket = self._key_buckets.get(key)
            if bucket is None:
                bucket = self._key_buckets[key] = TokenBucket(
                    SEARCH_PER_KEY_RATE, SEARCH_PER_KEY_BURST, clock=self._clock
                )
            if not bucket.try_acquire():
                return []
        payload = await self._get(
            "/json/stations/search",
            {
                "name": checked,
                "hidebroken": "true",
                "limit": str(SEARCH_LIMIT),
                "order": "clickcount",
                "reverse": "true",
            },
            reserve=SEARCH_RESERVE,
        )
        if payload is None:
            return []
        stations = self._stations(payload)
        self._cache[cache_key] = tuple(stations)
        return stations

    async def by_uuid(self, uuid: object) -> Station | None:
        """A estação pelo ``uuid``, SEM registrar clique (mostrar a reserva atual, health check)."""
        if not valid_uuid(uuid):
            return None
        payload = await self._get("/json/stations/byuuid", {"uuids": str(uuid)})
        found = self._stations(payload)
        return found[0] if found and found[0].uuid == uuid else None

    async def fresh_for_play(self, uuid: object) -> Station | None:
        """A estação para TOCAR agora: ``/json/url/{uuid}`` (registra o clique) e o resultado passa de novo por ``check_station``.

        ``None`` = o diretório respondeu e a estação não existe ou não passa nas regras. ``DirectoryError`` = o diretório não respondeu (rede, espelhos,
        fichas): quem chama NÃO deve culpar a estação."""
        if not valid_uuid(uuid):
            return None
        found = self._stations(await self._get_strict("/json/stations/byuuid", {"uuids": str(uuid)}))
        base = found[0] if found and found[0].uuid == uuid else None
        if base is None:
            return None
        payload = await self._get_strict(f"/json/url/{base.uuid}", {})
        url = payload.get("url") if isinstance(payload, dict) and payload.get("ok") in (True, "true") else None
        checked = check_station(
            {
                "stationuuid": base.uuid,
                "name": base.name,
                "url_resolved": url,
                "codec": base.codec,
                "bitrate": base.bitrate,
                "hls": 0,
                "country": base.country,
            }
        )
        return None if isinstance(checked, RuleError) else checked
