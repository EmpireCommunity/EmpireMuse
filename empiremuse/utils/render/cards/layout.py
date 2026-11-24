"""Geometria, tokens e regras pequenas dos cartões das skins de imagem da Jukebox, num lugar só.

Os layouts ainda não foram aprovados pelo dono (rodada 2): toda coordenada, tamanho, fonte e cor que os renderizadores
(``expanded.py``, ``slim.py``, ``disc.py``) usam mora AQUI. Os renderizadores não têm número de desenho próprio.

- Expandida v2 e Slim são desenhadas em 1×: as coordenadas abaixo já são pixels do arquivo.
- ``CardArt``: os pixels que o desenho recebe de fora (a capa crua do ``CoverService`` e o PNG do logo).
- ``Translate``: chave do catálogo → texto. Nenhuma palavra visível é escrita no código; ``LABEL_KEYS`` lista toda chave
  pedida, para o dono do ``bot.json`` conferir.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from typing import NamedTuple

from PIL import ImageDraw

from empiremuse.rules.card import CardSpec, CardState
from empiremuse.utils.music.card_facts import (
    FLAG_ALWAYS_ON,
    FLAG_AUTOPLAY,
    FLAG_NIGHTCORE,
    FLAG_REPEAT_QUEUE,
    FLAG_REPEAT_TRACK,
    FLAG_RESTRICTED,
    QUEUE_KIND_EMPTY,
    QUEUE_KIND_QUEUE,
    QUEUE_KIND_RECOMMENDED,
)
from empiremuse.utils.render.fonts import FIGTREE, NOTO, UNBOUNDED

RGB = tuple[int, int, int]
RGBA = tuple[int, int, int, int]
Box = tuple[int, int, int, int]
Span = tuple[int, int]  # y de cima e y de baixo de uma faixa do cartão

Translate = Callable[..., str]  # ``player.tr``: chave e os valores dos ``{marcadores}``


class CardArt(NamedTuple):
    """Os pixels de fora do desenho. ``cover``: RGB cru 240×240 (172800 bytes) do ``CoverService``, ou ``None``.
    ``logo``: PNG (o logo do bot em M, S, ocioso e call privada; o da estação em R), ou ``None``."""

    cover: bytes | None
    logo: bytes | None


COVER_SIDE = 240
COVER_BYTES = COVER_SIDE * COVER_SIDE * 3  # 172800: qualquer outro tamanho = sem capa


# ---------------------------------------------------------------------------------------------------- estados e fatos

RADIO_STATES = frozenset({CardState.RADIO_LIVE, CardState.RADIO_RETRY})
MUSIC_STATES = frozenset({CardState.PLAYING, CardState.PAUSED, CardState.STREAM})


MAX_VOLUME = 150  # o volume do player vai até 150: o deslizante é a fração disso


def private(spec: CardSpec) -> CardSpec:
    """Call privada: nada do que toca chega ao desenho (título, artista, capa, call, quem pediu, fila, rádio, estação),
    mesmo que quem montou o ``CardSpec`` não tenha passado pelo ``rules.card.seal``. Fora dela, o ``spec`` como veio."""
    if not spec.hidden:
        return spec
    return spec._replace(track=None, cover=None, voice_name="", queue=(), radio=None, station=None)


def is_station(spec: CardSpec) -> bool:
    """Estado S: música normal tocando pela estação do servidor."""
    return spec.station is not None and spec.state in MUSIC_STATES


# ---------------------------------------------------------------------------------------------------------- textos

LABEL_KEYS: tuple[str, ...] = (
    "jbx.state.playing",
    "jbx.state.paused",
    "jbx.state.radio",
    "jbx.state.reconnecting",
    "jbx.state.station",
    "jbx.state.idle",
    "jbx.artist",  # nova: o rótulo ARTISTA do painel da Rádio da Jukebox (a spec não lhe dá chave)
    "jbx.requested_by",
    "jbx.from_station",
    "jbx.recommended",
    "jbx.up_next",
    "jbx.recommended_list",
    "jbx.and_more",
    "jbx.then_station",
    "jbx.queue.empty",
    "jbx.idle.title",
    "jbx.idle.hint",
    "jbx.hidden_track",
    "jbx.retry",
    "jbx.vote",
    "jbx.on_air",
    "jbx.origin",
    "jbx.chip.autoplay",
    "jbx.chip.nightcore",
    "jbx.chip.always_on",
    "jbx.chip.restricted",
    "jbx.repeat.track",
    "jbx.repeat.track_on",
    "jbx.repeat.queue",
    "jbx.title_in_text",
    # já existem no catálogo (o visor da rádio): a qualidade da estação em R
    "visor.quality.good",
    "visor.quality.ok",
    "visor.quality.weak",
    "visor.quality.failing",
)


def clock(seconds: int) -> str:
    """``3:29`` ou ``1:02:03``."""
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


# ---------------------------------------------------------------------------------------------------------- tokens


def _hex(value: str) -> RGB:
    return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)


class Theme(NamedTuple):
    """Os tokens da §4.2 da spec (Expandida e Slim usam os mesmos)."""

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
LIGHT_THEME_NAME = "white"


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
RETRY_DISC_OPACITY = 0.60  # R reconectando: o disco a 60%

# ---------------------------------------------------------------------------------------------------------- fontes


class FontSpec(NamedTuple):
    name: str
    weight: int
    size: int


VISOR_TEXT_CHECK = FontSpec(
    NOTO, 700, 20
)  # o visor desenha em Inter: o título só é conferido (direita para a esquerda)

# ---------------------------------------------------------------------------------------------------------- geometria


class Crown(NamedTuple):
    """A coroa: metade de cima de uma elipse que atravessa o cartão (``outer`` = caixa da elipse inteira)."""

    outer: tuple[float, float, float, float]
    outer_width: int
    inner_inset: int  # arco interno tantos px para dentro; 0 = sem arco interno
    lights: int  # luzes entre os dois arcos (0 = nenhuma)
    light_side: int
    step_deg: float  # passo do degradê ao longo do arco


class CardLayout(NamedTuple):
    size: tuple[int, int]
    x0: int  # conteúdo de x0 a x1
    x1: int
    crown: Crown
    header: Span  # chips do topo (estado e bot) e a marca
    cabinet: bool  # laterais de jukebox (só a Expandida v2)
    vitrine: Box
    vitrine_radius: int
    disc: Box  # caixa do disco: é a ``disc_box`` (1× = pixels do arquivo)
    label_side: int  # o rótulo (capa ou logo) em círculo
    title_top: int
    title_size: int
    title_lines: int
    title_line_h: int
    sub_size: int
    sub_gap: int  # abaixo da última linha do título
    sub_line_h: int
    timeline: Span | None  # só a Expandida v2
    chips: Span
    info: Span | None  # linha de informação (só a Expandida v2)
    divider_top: int
    upnext: Span
    rows_top: int
    row_h: int
    rows_max: int  # linhas no total, contando "e mais {n}" e "Depois: estação"
    row_size: int
    divider_bottom: int
    base: Span  # legenda de transporte e volume


# ---------------------------------------------------------------------------------------------------------- ícones
# Os ícones que o ``primitives.mono_icon`` não tem, numa prancheta de 128 px (reduzida ao tamanho do uso). Traço único,
# uma cor; ``CLEAR`` abre o vazado.

CLEAR: RGBA = (0, 0, 0, 0)


Shape = Callable[[ImageDraw.ImageDraw, RGBA], None]


def _play(d: ImageDraw.ImageDraw, c: RGBA) -> None:
    d.polygon([(36, 22), (36, 106), (106, 64)], fill=c)


def _pause(d: ImageDraw.ImageDraw, c: RGBA) -> None:
    d.rectangle((32, 24, 56, 104), fill=c)
    d.rectangle((72, 24, 96, 104), fill=c)


def _lock(d: ImageDraw.ImageDraw, c: RGBA) -> None:
    d.arc((38, 14, 90, 74), 180, 360, fill=c, width=11)
    d.rectangle((38, 42, 49, 62), fill=c)
    d.rectangle((79, 42, 90, 62), fill=c)
    d.rounded_rectangle((24, 58, 104, 114), radius=10, fill=c)


def _radio(d: ImageDraw.ImageDraw, c: RGBA) -> None:
    d.line((34, 46, 90, 16), fill=c, width=8)
    d.rounded_rectangle((12, 44, 116, 110), radius=12, fill=c)
    d.ellipse((68, 58, 100, 90), fill=CLEAR)
    d.rectangle((26, 62, 56, 70), fill=CLEAR)
    d.rectangle((26, 80, 56, 88), fill=CLEAR)
