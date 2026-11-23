"""A marca da faixa ao vivo da Jukebox (T-434 §1.5 [m1]), na própria faixa: vale mesmo depois que ``player.radio_mode`` foi limpo.

O ``track.id`` dessa faixa carrega o endereço local do relay com o token. Por isso ela nunca entra em fila, histórico, fila salva, sessão
gravada, cache de resolução ou outro nó do Lavalink, nem volta pelo ⏮. Módulo sem dependências: os modelos do player o importam sem
puxar o controlador (``utils/radio/jukebox_radio.py``).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

LIVE_MARK = "jukebox_live"


def mark_live(track: Any) -> None:
    track.info.setdefault("extra", {})[LIVE_MARK] = True


def is_live(track: Any) -> bool:
    info = getattr(track, "info", None)
    extra = info.get("extra") if isinstance(info, dict) else None
    return isinstance(extra, dict) and extra.get(LIVE_MARK) is True


def without_live(tracks: Iterable[Any]) -> list[Any]:
    return [t for t in tracks if not is_live(t)]
