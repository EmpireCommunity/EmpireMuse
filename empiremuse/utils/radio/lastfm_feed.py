"""As listas de faixas do Last.fm para a rádio personalizada (item 8, R5, contrato §1 e §4): ``tag.getTopTracks`` (gêneros internacionais) e
``artist.getTopTracks`` (gêneros brasileiros e estações de artistas).

- **Taxa:** um balde PRÓPRIO do processo, de 1 requisição por segundo com rajada de 3, separado do scrobble (os termos do Last.fm proíbem chamadas contínuas
  várias vezes por segundo).
- **Cache** em ``global.radio_lastfm`` (``_id`` = ``"<método>:<argumento normalizado>"``): vale 24 h; se o Last.fm falhar, a lista velha serve até 7 d.
  Só o que se mostra e se toca é guardado (artista e título).
- **Corpo** cortado em 512 KiB; texto de fora passa por ``clean_title`` antes de entrar (e pela função única de texto externo antes de aparecer).
- A chave da API vai na consulta, então NADA daqui loga a mensagem de uma exceção do ``aiohttp`` (a URL a traz): só o tipo (R-12).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp

from empiremuse.rules.radio_shuffle import Track
from empiremuse.rules.radio_station import normalize_name
from empiremuse.rules.text import clean_title
from empiremuse.utils.radio.directory import TokenBucket
from empiremuse.utils.radio.premium import GLOBAL_DB

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool

logger = logging.getLogger(__name__)

LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
CACHE_DB = "radio_lastfm"  # nome da coleção
RATE_PER_S = 1.0
BURST = 3.0
MAX_BODY = 512 * 1024
REQUEST_TIMEOUT_S = 10
FRESH_S = 24 * 3600
STALE_S = 7 * 24 * 3600
TAG_LIMIT = 100
TAG_PAGES = 2
ARTIST_LIMIT = 30
TEXT_LIMIT = 100
BUCKET_WAIT_S = 30.0
BUCKET_POLL_S = 0.25
ERROR_NOT_FOUND = 6  # "The artist you supplied could not be found"

Fetch = Callable[[dict[str, str]], Awaitable[Mapping[str, Any]]]


class LastFmMissing(Exception):
    """Sem ``LASTFM_KEY``: as estações de gênero e de artistas não tocam (a de playlist continua)."""


class LastFmUnavailable(Exception):
    """O Last.fm não respondeu (rede, limite, corpo grande ou inválido) e não há lista velha para usar."""


class ArtistNotFound(Exception):
    pass


@dataclass(frozen=True)
class FeedResult:
    tracks: list[Track]
    unknown: tuple[str, ...] = ()  # artistas que o Last.fm não conhece (⚠️ nas duas telas)


def _parse(items: object) -> list[Track]:
    """Os pares (artista, título) válidos de uma lista do Last.fm; o que não tem forma de faixa é ignorado."""
    found: list[Track] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, Mapping):
            continue
        artist = item.get("artist")
        name = clean_title(artist.get("name") if isinstance(artist, Mapping) else artist, 1, TEXT_LIMIT)
        title = clean_title(item.get("name"), 1, TEXT_LIMIT)
        if name and title:
            found.append(Track(name, title))
    return found


class LastFmFeed:
    def __init__(
        self,
        pool: BotPool,
        *,
        fetch: Fetch | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.pool = pool
        self._fetch = fetch or self._http_json
        self._clock, self._sleep, self._monotonic = clock, sleep, monotonic
        self._bucket = TokenBucket(RATE_PER_S, BURST, clock=monotonic)

    # ------------------------------------------------------------------ cliente

    @property
    def configured(self) -> bool:
        client = getattr(self.pool, "last_fm", None)
        return bool(client is not None and getattr(client, "api_key", ""))

    def _api_key(self) -> str:
        client = getattr(self.pool, "last_fm", None)
        key = getattr(client, "api_key", "") if client is not None else ""
        if not key:
            raise LastFmMissing
        return str(key)

    async def _take(self) -> None:
        """Espera uma ficha do balde (1/s, rajada 3); passar de ``BUCKET_WAIT_S`` é o Last.fm "fora" para quem chamou."""
        deadline = self._monotonic() + BUCKET_WAIT_S
        while not self._bucket.try_acquire():
            if self._monotonic() >= deadline:
                raise LastFmUnavailable
            await self._sleep(BUCKET_POLL_S)

    async def _http_json(self, params: dict[str, str]) -> Mapping[str, Any]:
        query = {**params, "api_key": self._api_key(), "format": "json"}
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(LASTFM_URL, params=query, allow_redirects=False) as response:
                    if response.status >= 500 or response.status == 429 or 300 <= response.status < 400:
                        raise LastFmUnavailable
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(16384):
                        body.extend(chunk)
                        if len(body) > MAX_BODY:
                            raise LastFmUnavailable
            data = json.loads(bytes(body))
        except LastFmUnavailable:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError) as error:
            logger.warning(
                "Last.fm não respondeu (%s)", type(error).__name__
            )  # a mensagem traz a URL com a chave (R-12)
            raise LastFmUnavailable from None
        if not isinstance(data, Mapping):
            raise LastFmUnavailable
        return data

    async def _call(self, params: dict[str, str]) -> Mapping[str, Any]:
        await self._take()
        data = await self._fetch(params)
        error = data.get("error")
        if error == ERROR_NOT_FOUND:
            raise ArtistNotFound
        if error is not None:
            raise LastFmUnavailable
        return data

    # ------------------------------------------------------------------ cache

    async def _cached(self, key: str) -> dict[str, Any] | None:
        try:
            return await self.pool.database.find_doc(key, db_name=CACHE_DB, collection=GLOBAL_DB)
        except Exception as error:  # noqa: BLE001 - o cache é conveniência: só o tipo vai ao log (R-12)
            logger.warning("Cache do Last.fm não lido (%s)", type(error).__name__)
            return None

    async def _store(self, key: str, doc: Mapping[str, Any], previous: Mapping[str, Any] | None) -> None:
        rev = previous.get("rev") if previous else None
        try:
            await self.pool.database.save_doc_cas(
                key,
                {**doc, "rev": (rev if isinstance(rev, int) else 0) + 1},
                expected_rev=rev if isinstance(rev, int) else None,
                db_name=CACHE_DB,
                collection=GLOBAL_DB,
            )
        except Exception as error:  # noqa: BLE001
            logger.warning("Cache do Last.fm não gravado (%s)", type(error).__name__)

    def _entry(self, doc: Mapping[str, Any] | None, max_age: float) -> tuple[list[Track], bool] | None:
        """``(faixas, artista_desconhecido)`` de uma entrada do cache com no máximo ``max_age`` s, ou ``None``."""
        if not doc:
            return None
        at = doc.get("fetched_at")
        if isinstance(at, bool) or not isinstance(at, int | float) or self._clock() - at > max_age:
            return None
        tracks = [Track(a, t) for a, t in (p for p in doc.get("tracks", []) if isinstance(p, list) and len(p) == 2)]
        return tracks, bool(doc.get("unknown"))

    async def _list(
        self, key: str, load: Callable[[], Awaitable[tuple[list[Track], bool]]]
    ) -> tuple[list[Track], bool]:
        stored = await self._cached(key)
        if (fresh := self._entry(stored, FRESH_S)) is not None:
            return fresh
        try:
            tracks, unknown = await load()
        except LastFmUnavailable, LastFmMissing:
            if (stale := self._entry(stored, STALE_S)) is not None:
                return stale  # o Last.fm falhou: a lista velha (até 7 d) segue tocando
            raise
        await self._store(
            key,
            {"tracks": [[t.artist, t.title] for t in tracks], "fetched_at": self._clock(), "unknown": unknown},
            stored,
        )
        return tracks, unknown

    # ------------------------------------------------------------------ listas

    async def tag_tracks(self, tag: str) -> list[Track]:
        async def load() -> tuple[list[Track], bool]:
            found: list[Track] = []
            for page in range(1, TAG_PAGES + 1):
                data = await self._call(
                    {"method": "tag.gettoptracks", "tag": tag, "limit": str(TAG_LIMIT), "page": str(page)}
                )
                top = data.get("tracks")
                found.extend(_parse(top.get("track") if isinstance(top, Mapping) else None))
            return found, False

        return (await self._list(f"tag:{normalize_name(tag)}", load))[0]

    async def artist_tracks(self, artist: str) -> tuple[list[Track], bool]:
        """``(faixas, desconhecido)``: artista que o Last.fm não conhece é lista vazia e ``desconhecido`` (nunca erro)."""

        async def load() -> tuple[list[Track], bool]:
            try:
                data = await self._call(
                    {"method": "artist.gettoptracks", "artist": artist, "limit": str(ARTIST_LIMIT), "autocorrect": "1"}
                )
            except ArtistNotFound:
                return [], True
            top = data.get("toptracks")
            return _parse(top.get("track") if isinstance(top, Mapping) else None), False

        return await self._list(f"artist:{normalize_name(artist)}", load)

    async def recipe_tracks(self, recipe: Mapping[str, Any]) -> FeedResult:
        """As faixas de uma receita do catálogo (``{"tags": [...]}``) ou de uma estação de artistas (``{"artists": [...]}``).

        Uma tag ou um artista que o Last.fm não entregou agora não derruba a estação inteira: vale o resto. Só se NADA veio é que
        ``LastFmUnavailable`` (ou ``LastFmMissing``) sobe.
        """
        if not self.configured:
            raise LastFmMissing
        tracks: list[Track] = []
        unknown: list[str] = []
        failed = 0
        for tag in recipe.get("tags", ()):
            try:
                tracks.extend(await self.tag_tracks(tag))
            except LastFmUnavailable:
                failed += 1
        for artist in recipe.get("artists", ()):
            try:
                found, missing = await self.artist_tracks(artist)
            except LastFmUnavailable:
                failed += 1
                continue
            tracks.extend(found)
            if missing:
                unknown.append(artist)
        if not tracks and failed:
            raise LastFmUnavailable
        return FeedResult(tracks, tuple(unknown))
