"""Os tetos nomeados do render (a vaga de desenho e o PNG; medidas no README do pacote).

Os números que uma regra pura usa ficam em ``empiremuse/rules/card.py`` (a camada ``rules`` não importa ``utils``) e são
reexportados aqui, para quem confere o orçamento ler um arquivo só.
"""

from __future__ import annotations

from empiremuse.rules.card import DEGRADE_AT, QUEUE_FACTS_MAX, RENDER_VERSION

RENDER_SLOTS = 1  # uma vaga de desenho no processo, para os players dos 5 bots e o visor da rádio 24 h
SLOT_WAIT_S = 10.0  # espera pela vaga acima disto = ``RenderBusy``
SLOW_RENDER_S = 1.0  # um desenho acima disto vai ao log (só a duração e o rótulo)

PNG_COMPRESS_LEVEL = 6  # nunca ``optimize=True`` (cerca de 5× mais CPU por alguns KiB)

__all__ = [
    "DEGRADE_AT",
    "PNG_COMPRESS_LEVEL",
    "QUEUE_FACTS_MAX",
    "RENDER_SLOTS",
    "RENDER_VERSION",
    "SLOT_WAIT_S",
    "SLOW_RENDER_S",
]
