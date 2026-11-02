"""O sorteio puro da rádio personalizada (item 8, R5, contrato §3): sem repetir faixa nem artista por perto, e o pool de uma estação.

Aqui só há dados e decisões, sem rede nem relógio: o gerador (``random.Random``) chega de fora para o teste ser determinístico. O histórico
das últimas escolhas fica na memória de quem chama (``utils/radio/custom_source.py``) e não vai para o banco.
"""

from __future__ import annotations

import random
import unicodedata
from collections.abc import Iterable, Sequence
from typing import NamedTuple

from empiremuse.rules.radio_station import POOL_MAX, POOL_PER_ARTIST_MAX

TRACK_WINDOW = 50  # uma faixa não volta em ``min(50, ⌊pool/2⌋)`` jogadas
ARTIST_WINDOW = 4  # um artista não volta em ``min(4, ⌊artistas/2⌋)``
HISTORY_MAX = TRACK_WINDOW


class Track(NamedTuple):
    artist: str
    title: str


def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split()).casefold()


def track_key(track: Track) -> tuple[str, str]:
    """A identidade de uma faixa: artista e título normalizados (a mesma faixa com caixa ou espaços diferentes é uma só)."""
    return _norm(track.artist), _norm(track.title)


def build_pool(tracks: Iterable[Track], rng: random.Random) -> list[Track]:
    """As faixas deduplicadas por artista e título, no máximo ``POOL_MAX``, com até ``POOL_PER_ARTIST_MAX`` por artista (amostra sorteada)."""
    unique: dict[tuple[str, str], Track] = {}
    for track in tracks:
        if track.artist.strip() and track.title.strip():
            unique.setdefault(track_key(track), track)
    by_artist: dict[str, list[Track]] = {}
    for key, track in unique.items():
        by_artist.setdefault(key[0], []).append(track)
    kept: list[Track] = []
    for group in by_artist.values():
        kept.extend(group if len(group) <= POOL_PER_ARTIST_MAX else rng.sample(group, POOL_PER_ARTIST_MAX))
    return kept if len(kept) <= POOL_MAX else rng.sample(kept, POOL_MAX)


def next_pick(pool: Sequence[Track], history: Sequence[Track], rng: random.Random) -> Track | None:
    """A próxima faixa: sorteada entre as que não estão nas janelas de faixa e de artista. Sem candidata, as janelas caem pela metade até abrir.

    ``None`` só com o pool vazio; com ao menos uma faixa sempre devolve uma (as janelas chegam a zero).
    """
    if not pool:
        return None
    artists = {_norm(t.artist) for t in pool}
    track_window = min(TRACK_WINDOW, len(pool) // 2)
    artist_window = min(ARTIST_WINDOW, len(artists) // 2)
    while True:
        recent_tracks = {track_key(t) for t in history[-track_window:]} if track_window else set()
        recent_artists = {_norm(t.artist) for t in history[-artist_window:]} if artist_window else set()
        candidates = [t for t in pool if track_key(t) not in recent_tracks and _norm(t.artist) not in recent_artists]
        if candidates:
            return rng.choice(candidates)
        if track_window == 0 and artist_window == 0:
            return rng.choice(list(pool))
        track_window //= 2
        artist_window //= 2


def remember(history: Sequence[Track], track: Track) -> list[Track]:
    """O histórico com a faixa nova no fim, cortado em ``HISTORY_MAX`` (sempre uma lista nova)."""
    return [*history, track][-HISTORY_MAX:]
