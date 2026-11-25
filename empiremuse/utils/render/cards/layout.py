"""Medidas e cores do disco procedural (``disc.record_image`` e ``utils/render/disc_only.py``), num lugar só."""

from __future__ import annotations

from typing import NamedTuple

RGB = tuple[int, int, int]

COVER_SIDE = 240
COVER_BYTES = COVER_SIDE * COVER_SIDE * 3  # 172800: qualquer outro tamanho = sem capa


# ---------------------------------------------------------------------------------------------------------- tokens


def _hex(value: str) -> RGB:
    return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)


class Theme(NamedTuple):
    """Os tokens da §4.2 da spec."""

    card: RGB
    vitrine: RGB
    text: RGB
    text_2: RGB
    muted: RGB  # ``apagado``: rótulos, quem pediu, números; no tema claro nunca sobre ``chip``
    chip: RGB
    chip_text: RGB
    line: RGB
    ok: RGB
    warn: RGB
    error: RGB
    live: RGB
    volume: RGB
    volume_bg: RGB


THEMES: dict[str, Theme] = {
    "black": Theme(
        card=_hex("#1A1240"),
        vitrine=_hex("#0B0A1F"),
        text=_hex("#F6F2FF"),
        text_2=_hex("#D9D0FF"),
        muted=_hex("#A99BE6"),
        chip=_hex("#261A5C"),
        chip_text=_hex("#F6F2FF"),
        line=_hex("#43359A"),
        ok=_hex("#6EE7C8"),
        warn=_hex("#FFD479"),
        error=_hex("#FFC9D6"),
        live=_hex("#E11D48"),
        volume=_hex("#8B5CF6"),
        volume_bg=_hex("#261A5C"),
    ),
    "white": Theme(
        card=_hex("#F6F2FF"),
        vitrine=_hex("#E6DEFF"),
        text=_hex("#1A1240"),
        text_2=_hex("#43359A"),
        muted=_hex("#6B5FB0"),
        chip=_hex("#E6DEFF"),
        chip_text=_hex("#1A1240"),
        line=_hex("#D9D0FF"),
        ok=_hex("#0F7A5F"),
        warn=_hex("#8A5A00"),
        error=_hex("#B4123C"),
        live=_hex("#E11D48"),
        volume=_hex("#7C3AED"),
        volume_bg=_hex("#D9D0FF"),
    ),
}
DEFAULT_THEME = "black"


def theme_for(name: str) -> Theme:
    """O tema do ``CardSpec``; nome desconhecido = escuro."""
    return THEMES.get(name, THEMES[DEFAULT_THEME])


GLOW_COLOR = _hex("#7C3AED")  # brilho radial da vitrine
GLOW_ALPHA = 0.35

# O disco (original, procedural): preto nos dois temas.
DISC_BODY = _hex("#121019")
DISC_GROOVE = _hex("#1E1B29")
DISC_BAND = _hex("#2A2638")  # as faixas mais marcadas entre as músicas
DISC_SHINE = (255, 255, 255)
DISC_SHINE_ALPHA = 30  # 0..255
DISC_SHINE_BLUR = 16  # em pixels da prancheta 2×
DISC_SUPERSAMPLE = 2
DISC_GROOVE_STEP = 3  # em 1×
DISC_BAND_EVERY = 7  # a cada tantos sulcos, uma faixa marcada
DISC_RIM = 3  # borda lisa de fora, em 1×
LABEL_RING = _hex("#0B0A1F")
LABEL_RING_WIDTH = 2
DISC_HOLE = 8
