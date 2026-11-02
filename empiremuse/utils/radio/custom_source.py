"""A fonte da rádio personalizada (item 8, R5, contrato §3 e §4): o pool de faixas de uma estação e o sorteio sem repetir faixa nem artista por perto.

- **Pool:** gênero e artistas vêm do Last.fm (``hub.lastfm``); playlists vêm do ``playlist_tracks`` que as portas fornecem (a mesma leitura da R1). Tudo é unido,
  deduplicado e cortado em 300 faixas com até 30 por artista (``rules.radio_shuffle.build_pool``).
- **Sorteio:** ``next_pick`` com o histórico só na memória. A faixa que NÃO resolve (nenhum stream tocável) entra no cache negativo de 24 h
  (``global.radio_resolved``) e sai do sorteio; cinco falhas seguidas pausam a estação ("sem faixas tocáveis") e a nova tentativa fica por conta de quem chama.
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


class FailedTracks:
    """O cache negativo de faixas sem stream (24 h), em memória e em ``global.radio_resolved`` (vale para todos os servidores e reinícios)."""

    def __init__(self, database: Any, *, clock: Callable[[], float] = time.time) -> None:
        self._database = database
        self._clock = clock
        self._memory: dict[str, float] = {}

    async def failed(self, track: Track) -> bool:
        key = failure_id(track)
        at = self._memory.get(key)
        if at is None:
            try:
                doc = await self._database.find_doc(key, db_name=RESOLVED_DB, collection=GLOBAL_DB)
            except Exception as error:  # noqa: BLE001 - o cache é conveniência: só o tipo vai ao log (R-12)
                logger.warning("Cache de faixas sem stream não lido (%s)", type(error).__name__)
                return False
            at = doc.get("at") if isinstance(doc, dict) and doc.get("fail") else None
            if not isinstance(at, int | float) or isinstance(at, bool):
                return False
            self._memory[key] = float(at)
        if self._clock() - self._memory[key] > FAIL_TTL_S:
            self._memory.pop(key, None)
            return False
        return True

    async def mark(self, track: Track) -> None:
        key, now = failure_id(track), self._clock()
        self._memory[key] = now
        try:
            stored = await self._database.find_doc(key, db_name=RESOLVED_DB, collection=GLOBAL_DB)
            rev = stored.get("rev") if isinstance(stored, dict) else None
            await self._database.save_doc_cas(
                key,
                {"fail": True, "at": now, "rev": (rev if isinstance(rev, int) else 0) + 1},
                expected_rev=rev if isinstance(rev, int) else None,
                db_name=RESOLVED_DB,
                collection=GLOBAL_DB,
            )
        except Exception as error:  # noqa: BLE001
            logger.warning("Cache de faixas sem stream não gravado (%s)", type(error).__name__)


@dataclass
class CustomSession:
    """O sorteio de UMA rádio: o pool, o histórico (só em memória) e a sequência de falhas."""

    pool: list[Track]
    failed: FailedTracks
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
