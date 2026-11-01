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
- **Local (R4):** ``region(nó)`` junta até 4 pedidos (raio de 100 km, estado, sigla do estado e nome da cidade) pelo MESMO balde global, esperando fichas até 6 s
  e devolvendo o que já tem com ``partial``; o texto de estado das estações passa de novo pelo resolvedor (``rules.radio_geo``), nunca pela substring do diretório.
- Qualquer falha (rede, TLS, JSON, tamanho) devolve vazio/``None``: o diretório é conveniência, nunca uma dependência que derrube a rádio.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import socket
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, NamedTuple

import aiohttp
import yarl
from cachetools import TTLCache

from empiremuse.rules.errors import RuleError
from empiremuse.rules.playlists import is_public_address
from empiremuse.rules.radio_geo import (
    MAX_HITS,
    RADIUS_M,
    StateIndex,
    clamp_clicks,
    distance_km,
    resolve_state,
    search_term,
    valid_coordinates,
)
from empiremuse.rules.radio_stations import Station, check_search_term, check_station, valid_mirror, valid_uuid
from empiremuse.utils.radio.locations import LocNode
from empiremuse.utils.radio.logo import LogoResult, LogoService
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
REGION_WAIT_S = (
    6.0  # quanto uma escolha de local espera por fichas do balde global antes de devolver o que já tem (``partial``)
)
REGION_POLL_S = 0.25
REGION_MAX_ITEMS = 400  # itens de uma resposta que se examinam (o corpo já é limitado a 256 KiB)
FAVICON_MEMORY = 4096  # estações cujo logo o diretório lembra (as de buscas recentes e as já vistas)
FAVICON_TTL_S = 3600
GEO_SLACK = 1.05  # a distância devolvida pelo diretório é conferida contra o raio com esta folga

Fetcher = Callable[[str, str, dict[str, str]], Awaitable[Any]]
MirrorSource = Callable[[], Awaitable[list[str]]]


class Hit(NamedTuple):
    """Uma estação de uma escolha de local: ``clickcount`` limitado a 0..10⁹, se tem coordenada válida e por onde casou (``geo``, ``name``, ``state``, ``country``)."""

    station: Station
    clickcount: int
    has_geo: bool
    matched: str


class RegionResult(NamedTuple):
    """As estações do local escolhido (até 150); ``partial`` = algum pedido não saiu (fichas esgotadas, balde do servidor ou espelhos fora)."""

    hits: tuple[Hit, ...]
    partial: bool


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
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        state_index: StateIndex | None = None,
        logos: LogoService | None = None,
    ) -> None:
        self._sleep = sleep
        self.state_index = state_index  # o resolvedor de estado do local (``LocationTree.state_index``); sem ele o filtro por estado não roda
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
        self._favicons: TTLCache = TTLCache(
            maxsize=FAVICON_MEMORY, ttl=FAVICON_TTL_S, timer=clock
        )  # uuid -> URL do logo, já limpa
        self.logos = logos or LogoService(self)

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

    async def _acquire_until(self, deadline: float, reserve: float) -> bool:
        """Espera uma ficha do balde global até ``deadline`` (no relógio do diretório); ``False`` se o prazo esgotou."""
        while not self._bucket.try_acquire(reserve):
            remaining = deadline - self._clock()
            if remaining <= 0:
                return False
            await self._sleep(min(REGION_POLL_S, remaining))
        return True

    async def _get_strict(
        self, path: str, query: dict[str, str], *, reserve: float = 0.0, wait_until: float | None = None
    ) -> Any:
        """Pede a um dos espelhos (até 3, em ordem embaralhada). ``DirectoryError`` se o bucket global está vazio (ou o prazo de ``wait_until`` esgotou)
        ou nenhum espelho respondeu."""
        acquired = (
            self._bucket.try_acquire(reserve) if wait_until is None else await self._acquire_until(wait_until, reserve)
        )
        if not acquired:
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

    def _remember(self, station: Station) -> None:
        """Guarda o logo (já validado pelo ``check_station``) da estação: só um uuid que passou por aqui pode ter o logo buscado depois."""
        if station.favicon:
            self._favicons[station.uuid] = station.favicon

    def _stations(self, payload: Any) -> list[Station]:
        """Os itens aceitos de uma lista; o que não é lista ou não passa em ``check_station`` é descartado em silêncio."""
        if not isinstance(payload, list):
            return []
        found: list[Station] = []
        for item in payload[: SEARCH_LIMIT * 2]:
            station = check_station(item)
            if not isinstance(station, RuleError):
                self._remember(station)
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

    # ------------------------------------------------------------------ local (R4)

    def _key_allows(self, key: object) -> bool:
        """O balde por servidor da R3: uma escolha de local custa UMA ficha dele, qualquer que seja o número de pedidos."""
        if key is None:
            return True
        bucket = self._key_buckets.get(key)
        if bucket is None:
            bucket = self._key_buckets[key] = TokenBucket(SEARCH_PER_KEY_RATE, SEARCH_PER_KEY_BURST, clock=self._clock)
        return bucket.try_acquire()

    @staticmethod
    def _region_queries(node: LocNode) -> list[tuple[str, dict[str, str]]]:
        """Os pedidos (no máximo 4) de uma escolha: ``(origem, query)``. País final: um pedido por ``countrycode``; senão geo, estado (por substring
        e pela sigla exata) e nome da cidade."""
        base = {"hidebroken": "true", "order": "clickcount", "reverse": "true", "limit": str(MAX_HITS)}
        if node.cc is None:
            return []
        if node.kind == "country":
            return [("country", {**base, "countrycode": node.cc})]
        queries: list[tuple[str, dict[str, str]]] = []
        if node.lat is not None and node.lon is not None:
            queries.append(
                (
                    "geo",
                    {
                        **base,
                        "geo_lat": f"{node.lat:.5f}",
                        "geo_long": f"{node.lon:.5f}",
                        "geo_distance": str(RADIUS_M),
                    },
                )
            )
        term = search_term(node.terms[0]) if node.terms else None
        if term is not None:
            queries.append(("state", {**base, "countrycode": node.cc, "state": term}))
        exact = search_term(node.exact)
        if exact is not None:
            queries.append(("state", {**base, "countrycode": node.cc, "state": exact, "stateExact": "true"}))
        city = search_term(node.city)
        if city is not None and node.lat is not None:
            queries.append(("name", {**base, "countrycode": node.cc, "name": city}))
        return queries[:4]

    def _region_items(self, payload: Any, source: str, node: LocNode) -> list[Hit]:
        """Os itens aceitos de uma resposta: estação válida (``check_station``), do país do nó e, por origem, dentro do raio ou do estado."""
        if not isinstance(payload, list):
            return []
        found: list[Hit] = []
        for item in payload[:REGION_MAX_ITEMS]:
            if not isinstance(item, Mapping):
                continue
            station = check_station(item)
            if isinstance(station, RuleError):
                continue
            if source != "geo" and node.cc is not None and str(item.get("countrycode", node.cc)).upper() != node.cc:
                continue  # o raio de 100 km pode cruzar a fronteira; os pedidos por país não
            lat, lon = item.get("geo_lat"), item.get("geo_long")
            has_geo = valid_coordinates(lat, lon)
            if source == "geo":
                if not has_geo or node.lat is None or node.lon is None:
                    continue
                if distance_km(node.lat, node.lon, float(lat), float(lon)) > RADIUS_M / 1000 * GEO_SLACK:
                    continue
            if source == "state" and not self._in_state(item, node):
                continue
            self._remember(station)
            found.append(Hit(station, clamp_clicks(item.get("clickcount")), has_geo, source))
        return found

    def _in_state(self, item: Mapping[str, Any], node: LocNode) -> bool:
        """O texto de estado da estação é de uma das subdivisões do nó? ("para" também casa "Paraná": quem decide é o resolvedor, não o diretório.)"""
        if self.state_index is None or node.cc is None:
            return False
        return resolve_state(self.state_index, node.cc, item.get("state")) in node.admin1

    @staticmethod
    def _merge_region(results: Sequence[tuple[str, list[Hit]]]) -> tuple[Hit, ...]:
        """União sem repetir ``uuid``. Primeiro quem casou por geo, nome ou país (por cliques); depois quem só casou por estado e não tem coordenada
        (por cliques). Quem só casou por estado MAS tem coordenada fora do raio é de outra parte do estado e fica de fora."""
        by_uuid: dict[str, Hit] = {}
        for _, hits in results:
            for hit in hits:
                previous = by_uuid.get(hit.station.uuid)
                if previous is None or (previous.matched == "state" and hit.matched != "state"):
                    by_uuid[hit.station.uuid] = hit
        near = sorted((h for h in by_uuid.values() if h.matched != "state"), key=lambda h: -h.clickcount)
        rest = sorted(
            (h for h in by_uuid.values() if h.matched == "state" and not h.has_geo), key=lambda h: -h.clickcount
        )
        return tuple((near + rest)[:MAX_HITS])

    async def region(self, node: LocNode | None, *, key: object = None) -> RegionResult:
        """As estações do local escolhido (cidade e entorno de 100 km, ou o país final). Quem chama já conferiu premium e permissão.

        No máximo 4 pedidos, todos pelo balde global (com a reserva de 1 ficha para o play) e esperando fichas até ``REGION_WAIT_S``; esgotado o prazo
        devolve o que já tem com ``partial``. Repetir a mesma escolha vem do cache (0 pedidos); resultado ``partial`` não é guardado.
        """
        if node is None or not self._region_queries(node):
            return RegionResult((), False)
        cache_key = ("region", node.id)
        if cache_key in self._cache:
            return RegionResult(self._cache[cache_key], False)
        if not self._key_allows(key):
            return RegionResult((), True)
        deadline = self._clock() + REGION_WAIT_S
        results: list[tuple[str, list[Hit]]] = []
        partial = False
        for source, query in self._region_queries(node):
            try:
                payload = await self._get_strict(
                    "/json/stations/search", query, reserve=SEARCH_RESERVE, wait_until=deadline
                )
            except DirectoryError:
                partial = True
                continue
            results.append((source, self._region_items(payload, source, node)))
        hits = self._merge_region(results)
        if not partial:
            self._cache[cache_key] = hits
        return RegionResult(hits, partial)

    async def by_uuid(self, uuid: object) -> Station | None:
        """A estação pelo ``uuid``, SEM registrar clique (mostrar a reserva atual, health check)."""
        if not valid_uuid(uuid):
            return None
        payload = await self._get("/json/stations/byuuid", {"uuids": str(uuid)})
        found = self._stations(payload)
        return found[0] if found and found[0].uuid == uuid else None

    # ------------------------------------------------------------------ logo (painel)

    def favicon_of(self, uuid: str) -> str | None:
        """A URL do logo que o diretório já guardou para este uuid (nunca sai daqui para a página; só o ``LogoService`` a usa)."""
        return self._favicons.get(uuid)

    async def learn(self, uuid: str) -> None:
        """Consulta a estação pelo uuid (sem registrar clique, respeitando a reserva do play e do health) para saber o logo dela. ``DirectoryError`` se
        o diretório não respondeu ou não há ficha."""
        found = self._stations(
            await self._get_strict("/json/stations/byuuid", {"uuids": str(uuid)}, reserve=SEARCH_RESERVE)
        )
        if not found:
            self._favicons.pop(uuid, None)

    async def logo_png(self, uuid: object, *, configured: bool = False) -> LogoResult:
        """O logo da estação (PNG de até 128 px, re-codificado) ou ``LogoResult(None)`` = ícone padrão. Nunca levanta; ver ``utils/radio/logo.py``."""
        return await self.logos.png(uuid, configured=configured)

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
