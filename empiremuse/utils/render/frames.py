"""Quadros do disco já quantizados (``DiscFrames``) e o cache deles, limitado por BYTES.

A chave nunca é a faixa nem a URL: é o ``sha256`` dos pixels da capa reduzida mais tudo o que muda os pixels (skin, escala,
tema, número de quadros e ``RENDER_VERSION``). Duas faixas do mesmo álbum dividem a entrada; nenhum dado de servidor ou de
membro entra na chave nem no valor.
"""

from __future__ import annotations

import logging
from typing import NamedTuple

from cachetools import LRUCache

from empiremuse.rules.card import CardSpec
from empiremuse.utils.render.budget import FRAMES_CACHE_BYTES

logger = logging.getLogger(__name__)


class DiscFrames(NamedTuple):
    """A paleta única da faixa (768 bytes) e um ladrilho ``P`` cru por quadro, todos do tamanho de ``box``."""

    palette: bytes
    tiles: tuple[bytes, ...]
    box: tuple[int, int, int, int]  # em pixels do arquivo


class FramesKey(NamedTuple):
    cover_digest: str | None  # ``None`` = sem capa (o logo)
    skin: str
    scale: int
    theme: str
    frames: int
    render_version: int


def frames_key(spec: CardSpec, cover_digest: str | None, frames: int) -> FramesKey:
    """A chave dos quadros de um ``CardSpec`` já projetado pela skin: só o que muda os pixels do disco."""
    return FramesKey(cover_digest, spec.skin, spec.scale, spec.theme, frames, spec.render_version)


def frames_size(value: DiscFrames) -> int:
    """O peso de uma entrada: a paleta mais a soma dos BYTES dos ladrilhos (nunca o número de ladrilhos)."""
    return len(value.palette) + sum(len(tile) for tile in value.tiles)


class FramesCache:
    """``LRUCache`` por bytes; a expulsão é a única limpeza. Valor maior que o teto não é guardado (o render segue)."""

    def __init__(self, max_bytes: int = FRAMES_CACHE_BYTES) -> None:
        self._cache: LRUCache[FramesKey, DiscFrames] = LRUCache(maxsize=max_bytes, getsizeof=frames_size)

    def get(self, key: FramesKey) -> DiscFrames | None:
        return self._cache.get(key)

    def put(self, key: FramesKey, value: DiscFrames) -> bool:
        try:
            self._cache[key] = value
        except ValueError:  # maior que o teto inteiro: o cachetools recusa
            logger.warning("Quadros do disco acima do teto do cache (%d bytes)", frames_size(value))
            return False
        return True

    @property
    def size(self) -> int:
        return int(self._cache.currsize)

    @property
    def max_bytes(self) -> int:
        return int(self._cache.maxsize)

    def __len__(self) -> int:
        return len(self._cache)
