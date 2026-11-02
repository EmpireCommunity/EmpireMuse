"""A fonte da rádio personalizada (item 8, R5, contrato §3 e §4): o pool de faixas de uma estação e o sorteio sem repetir faixa nem artista por perto.

- **Pool:** gênero e artistas vêm do Last.fm (``hub.lastfm``); playlists vêm do ``playlist_tracks`` que as portas fornecem (a mesma leitura da R1). Tudo é unido,
  deduplicado e cortado em 300 faixas com até 30 por artista (``rules.radio_shuffle.build_pool``).
- **Sorteio:** ``next_pick`` com o histórico só na memória. A faixa que NÃO resolve (nenhum stream tocável) entra no cache negativo de 24 h
  (``global.radio_resolved``) e sai do sorteio; a que resolve é guardada ali por 7 dias; cinco falhas seguidas pausam a estação ("sem faixas tocáveis") e a nova tentativa fica por conta de quem chama.
- Nada aqui toca o Discord nem o Lavalink: a resolução da faixa chega como função (``resolve``).
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from empiremuse.rules.radio_genres import find_genre, recipe, shipped_catalog
from empiremuse.rules.radio_shuffle import Track, build_pool, next_pick, remember, track_key
from empiremuse.rules.radio_station import KIND_ARTISTS, KIND_GENRE, KIND_PLAYLISTS, CustomStation
from empiremuse.utils.radio.premium import GLOBAL_DB

if TYPE_CHECKING:
    from empiremuse.utils.radio import RadioHub

logger = logging.getLogger(__name__)

RESOLVED_DB = "radio_resolved"  # nome da coleção do cache negativo
FAIL_TTL_S = 24 * 3600
RESOLVED_TTL_S = 7 * 24 * 3600  # a faixa resolvida vale 7 dias
ABSENT_TTL_S = (
    300  # "não há nada guardado para esta faixa" é lembrado por 5 min (sem uma consulta por faixa a cada sorteio)
)
STOCK_SLOTS = 2  # adiantados ao mesmo tempo em todas as rádios do processo
FAIL_STREAK_MAX = 5  # falhas seguidas que pausam a estação
RETRY_AFTER_S = 600  # a nova tentativa de uma estação sem faixas tocáveis
PLAYLIST_NAMES_MAX = 5

PlaylistTracks = Callable[[Sequence[str]], Awaitable[list[Track]]]
Resolve = Callable[[Track], Awaitable[Any | None]]


class EmptyStation(Exception):
    """A estação não tem nenhuma faixa (gênero ou artistas sem resultado, playlists vazias ou sumidas)."""


class NoPlayableTracks(Exception):
    """``FAIL_STREAK_MAX`` faixas seguidas sem stream tocável: a estação pausa e tenta de novo em ``RETRY_AFTER_S``."""


@dataclass
class Loaded:
    pool: list[Track]
    unknown: tuple[str, ...] = ()


async def load_pool(
    hub: RadioHub, station: CustomStation, playlist_tracks: PlaylistTracks, rng: random.Random | None = None
) -> Loaded:
    """O pool da estação. ``EmptyStation`` sem faixas; ``LastFmMissing``/``LastFmUnavailable`` sobem para quem chama mostrar (gênero e artistas)."""
    rng = rng or random.Random()
    if station.kind == KIND_PLAYLISTS:
        names = list(station.source.get("playlists", ()))[:PLAYLIST_NAMES_MAX]
        pool = build_pool(await playlist_tracks(names), rng)
        unknown: tuple[str, ...] = ()
    elif station.kind == KIND_GENRE:
        genre = find_genre(shipped_catalog(), station.source.get("genre"))
        if genre is None:
            raise EmptyStation
        fed = await hub.lastfm.recipe_tracks(recipe(genre))
        pool, unknown = build_pool(fed.tracks, rng), ()
    elif station.kind == KIND_ARTISTS:
        fed = await hub.lastfm.recipe_tracks({"artists": list(station.source.get("artists", ()))})
        pool, unknown = build_pool(fed.tracks, rng), fed.unknown
    else:
        raise EmptyStation
    if not pool:
        raise EmptyStation
    return Loaded(pool, unknown)


def failure_id(track: Track) -> str:
    artist, title = track_key(track)
    return hashlib.sha1(f"{artist}\x1f{title}".encode()).hexdigest()  # noqa: S324 - só uma chave de cache, sem papel de segurança


class ResolvedTracks:
    """O cache de faixas resolvidas, em memória e em ``global.radio_resolved`` (vale para todos os servidores e reinícios; ``_id`` = ``failure_id``).

    Um documento por faixa, de um de dois tipos: **resolvida** ``{encoded, uri, source, length_ms, at}`` (7 dias) ou **falha** ``{fail: true, at}`` (24 h).
    A leitura do banco é feita no máximo uma vez por faixa (a AUSÊNCIA também é lembrada por ``ABSENT_TTL_S``): o sorteio olha o pool inteiro e não pode
    custar uma consulta por faixa a cada sorteio.
    """

    def __init__(self, database: Any, *, clock: Callable[[], float] = time.time) -> None:
        self._database = database
        self._clock = clock
        self._memory: dict[str, tuple[dict[str, Any] | None, float]] = {}  # chave: (documento ou None, quando foi lido)

    async def _entry(self, track: Track) -> dict[str, Any] | None:
        key, now = failure_id(track), self._clock()
        known = self._memory.get(key)
        if known is not None and (known[0] is not None or now - known[1] <= ABSENT_TTL_S):
            return known[0]
        try:
            doc = await self._database.find_doc(key, db_name=RESOLVED_DB, collection=GLOBAL_DB)
        except Exception as error:  # noqa: BLE001 - o cache é conveniência: só o tipo vai ao log (R-12)
            logger.warning("Cache de faixas resolvidas não lido (%s)", type(error).__name__)
            return None
        entry = doc if isinstance(doc, dict) and isinstance(doc.get("at"), int | float) else None
        self._memory[key] = (entry, now)
        return entry

    async def failed(self, track: Track) -> bool:
        entry = await self._entry(track)
        return bool(entry and entry.get("fail") and self._clock() - entry["at"] <= FAIL_TTL_S)

    async def get(self, track: Track) -> dict[str, Any] | None:
        """A faixa já resolvida (``encoded``, ``uri``, ``source``, ``length_ms``) com menos de 7 dias, ou ``None``."""
        entry = await self._entry(track)
        if not entry or entry.get("fail") or self._clock() - entry["at"] > RESOLVED_TTL_S:
            return None
        encoded = entry.get("encoded")
        return entry if isinstance(encoded, str) and encoded else None

    async def _write(self, track: Track, body: dict[str, Any]) -> None:
        key, now = failure_id(track), self._clock()
        document = {**body, "at": now}
        self._memory[key] = (document, now)
        try:
            stored = await self._database.find_doc(key, db_name=RESOLVED_DB, collection=GLOBAL_DB)
            rev = stored.get("rev") if isinstance(stored, dict) else None
            await self._database.save_doc_cas(
                key,
                {**document, "rev": (rev if isinstance(rev, int) else 0) + 1},
                expected_rev=rev if isinstance(rev, int) else None,
                db_name=RESOLVED_DB,
                collection=GLOBAL_DB,
            )
        except Exception as error:  # noqa: BLE001
            logger.warning("Cache de faixas resolvidas não gravado (%s)", type(error).__name__)

    async def mark(self, track: Track) -> None:
        """A faixa não resolveu: fora do sorteio por 24 h."""
        await self._write(track, {"fail": True})

    async def store(self, track: Track, partial: Any) -> None:
        """A faixa resolvida pelo nó: o próximo sorteio dela (inclusive depois de um reinício) não busca no Lavalink."""
        encoded = getattr(partial, "id", None)
        if not isinstance(encoded, str) or not encoded:
            return
        info = getattr(partial, "info", {}) or {}
        await self._write(
            track,
            {
                "fail": False,  # o ``$set`` mescla campos: sem isto uma falha antiga continuaria valendo por cima da faixa resolvida
                "encoded": encoded,
                "uri": str(info.get("uri") or "")[:2000],
                "source": str(info.get("sourceName") or "")[:60],
                "length_ms": int(info.get("length") or 0),
            },
        )

    async def forget(self, track: Track) -> None:
        """O ``encoded`` guardado não decodifica mais: a entrada é apagada e a faixa volta a ser buscada."""
        key = failure_id(track)
        self._memory[key] = (None, self._clock())
        try:
            await self._database.delete_data(key, RESOLVED_DB, GLOBAL_DB)
        except Exception as error:  # noqa: BLE001
            logger.warning("Cache de faixas resolvidas não apagado (%s)", type(error).__name__)


@dataclass
class CustomSession:
    """O sorteio de UMA rádio: o pool, o histórico (só em memória) e a sequência de falhas."""

    pool: list[Track]
    failed: ResolvedTracks
    rng: random.Random = field(default_factory=random.Random)
    history: list[Track] = field(default_factory=list)
    streak: int = 0

    async def next_playable(self, resolve: Resolve) -> tuple[Track, Any]:
        """Sorteia e resolve até achar uma faixa tocável. ``NoPlayableTracks`` depois de ``FAIL_STREAK_MAX`` falhas seguidas (a sequência é zerada ao pausar)."""
        while self.streak < FAIL_STREAK_MAX:
            candidates = [t for t in self.pool if not await self.failed.failed(t)]
            pick = next_pick(candidates, self.history, self.rng)
            if pick is None:
                break
            resolved = await resolve(pick)
            if resolved is None:
                self.streak += 1
                await self.failed.mark(pick)
                self.pool = [t for t in self.pool if track_key(t) != track_key(pick)] or self.pool
                continue
            self.streak = 0
            self.history = remember(self.history, pick)
            return pick, resolved
        self.streak = 0
        raise NoPlayableTracks
