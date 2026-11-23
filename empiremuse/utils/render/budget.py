"""Todos os tetos nomeados do render das skins num lugar só (contrato das skins 2.3, §2.6; medidas no README do pacote).

Os números que uma regra pura usa ficam em ``empiremuse/rules/card.py`` (a camada ``rules`` não importa ``utils``) e são
reexportados aqui, para quem confere o orçamento ler um arquivo só.
"""

from __future__ import annotations

from empiremuse.rules.card import COLD_GIF_GAP_S, DEGRADE_AT, DISC_MAX_PX_1X, QUEUE_FACTS_MAX, RENDER_VERSION

MIB = 1024 * 1024

RENDER_SLOTS = 1  # uma vaga de desenho no processo, para os players dos 5 bots e o visor da rádio 24 h
SLOT_WAIT_S = 10.0  # espera pela vaga acima disto = ``RenderBusy``
SLOW_RENDER_S = 1.0  # um desenho acima disto vai ao log (só a duração e o rótulo)

GIF_FRAMES = 30
GIF_FRAME_MS = 60  # 30 × 60 ms = 1,8 s por volta
GIF_MAX_BYTES = 3 * MIB // 2  # 1,5 MiB: acima disto o GIF é descartado e sai o PNG
PNG_COMPRESS_LEVEL = 6  # nunca ``optimize=True`` (cerca de 5× mais CPU por alguns KiB)
PALETTE_BYTES = 768  # 256 cores RGB

FRAMES_CACHE_BYTES = 24 * MIB  # cache de quadros do disco, limitado por bytes (nunca por entradas)

__all__ = [
    "COLD_GIF_GAP_S",
    "DEGRADE_AT",
    "DISC_MAX_PX_1X",
    "FRAMES_CACHE_BYTES",
    "GIF_FRAMES",
    "GIF_FRAME_MS",
    "GIF_MAX_BYTES",
    "MIB",
    "PALETTE_BYTES",
    "PNG_COMPRESS_LEVEL",
    "QUEUE_FACTS_MAX",
    "RENDER_SLOTS",
    "RENDER_VERSION",
    "SLOT_WAIT_S",
    "SLOW_RENDER_S",
]
