"""Geometria, tokens e regras pequenas dos cartões das skins de imagem da Jukebox, num lugar só.

Os layouts ainda não foram aprovados pelo dono (rodada 2): toda coordenada, tamanho, fonte e cor que os renderizadores
(``expanded.py``, ``slim.py``, ``disc.py``) usam mora AQUI. Os renderizadores não têm número de desenho próprio.

- Expandida v2 e Slim são desenhadas em 1×: as coordenadas abaixo já são pixels do arquivo.
- ``CardArt``: os pixels que o desenho recebe de fora (a capa crua do ``CoverService`` e o PNG do logo).
- ``Translate``: chave do catálogo → texto. Nenhuma palavra visível é escrita no código; ``LABEL_KEYS`` lista toda chave
  pedida, para o dono do ``bot.json`` conferir.
- ``gif_fits``: o teto do GIF, conferido ANTES de alocar qualquer ladrilho.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from typing import NamedTuple

from PIL import ImageDraw

from empiremuse.rules.card import CardSpec, CardState, can_animate
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

Translate = Callable[[str], str]


class CardArt(NamedTuple):
    """Os pixels de fora do desenho. ``cover``: RGB cru 240×240 (172800 bytes) do ``CoverService``, ou ``None``.
    ``logo``: PNG (o logo do bot em M, S, ocioso e call privada; o da estação em R), ou ``None``."""

    cover: bytes | None
    logo: bytes | None


COVER_SIDE = 240
COVER_BYTES = COVER_SIDE * COVER_SIDE * 3  # 172800: qualquer outro tamanho = sem capa

CARD_MAX = (480, 840)  # o maior cartão para o qual o orçamento do GIF foi medido (Expandida v2, 1×)


def gif_fits(card_size: tuple[int, int], box: tuple[int, int, int, int] | None, scale: int) -> bool:
    """O GIF cabe no orçamento? ``card_size`` e ``box`` em 1× e ``scale`` a do arquivo: o cartão dentro de ``CARD_MAX`` e
    a área do disco aceita por ``rules.card.can_animate``. Sem disco (``None``), nunca. Puro: nenhum ladrilho é alocado."""
    if box is None:
        return False
    factor = max(1, int(scale))
    width, height = card_size
    if not (0 < width * factor <= CARD_MAX[0] and 0 < height * factor <= CARD_MAX[1]):
        return False
    return can_animate(box, factor)


# ---------------------------------------------------------------------------------------------------- estados e fatos

RADIO_STATES = frozenset({CardState.RADIO_LIVE, CardState.RADIO_RETRY})
MUSIC_STATES = frozenset({CardState.PLAYING, CardState.PAUSED, CardState.STREAM})

# Os nomes de ``CardSpec.flags`` e de ``queue_kind`` vêm de quem monta o ``CardSpec`` (uma fonte só).
KNOWN_FLAGS = frozenset(
    {FLAG_AUTOPLAY, FLAG_NIGHTCORE, FLAG_ALWAYS_ON, FLAG_RESTRICTED, FLAG_REPEAT_TRACK, FLAG_REPEAT_QUEUE}
)

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
    "jbx.repeat.queue",
    "jbx.title_in_text",
    # já existem no catálogo (o visor da rádio): a qualidade da estação em R
    "visor.quality.good",
    "visor.quality.ok",
    "visor.quality.weak",
    "visor.quality.failing",
)

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def fill(template: str, **values: object) -> str:
    """Troca os ``{nome}`` do texto do catálogo numa passada só (um valor com chaves nunca é lido de novo como modelo)."""
    return _PLACEHOLDER.sub(lambda m: str(values[m[1]]) if m[1] in values else m[0], template)


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


# Degradê ``arco`` (posição de 0 a 1, cor), igual nos dois temas.
ARC_STOPS: tuple[tuple[float, RGB], ...] = (
    (0.0, _hex("#2563EB")),
    (0.42, _hex("#7C3AED")),
    (0.70, _hex("#C026D3")),
    (1.0, _hex("#E11D48")),
)
LIGHT_ALPHA = 0.70  # luzes da coroa: ``texto_2`` a 70%
LIGHT_HALO = 0.25
BUBBLE_ALPHA = 0.60  # bolhas do tubo de luz: ``texto`` a 60%
LIVE_TRACK_ALPHA = 0.50  # trilho do estado ao vivo: o degradê a 50%
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


BRAND_TEXT = "EMPIREMUSE"  # a marca, igual em todo idioma
BRAND_FONT = FontSpec(UNBOUNDED, 700, 13)
BRAND_TRACKING = 2
CHIP_FONT = FontSpec(FIGTREE, 600, 13)
INFO_FONT = FontSpec(FIGTREE, 500, 13)
HEADING_FONT = FontSpec(FIGTREE, 700, 13)
NUMBER_FONT = FontSpec(UNBOUNDED, 600, 13)  # números da fila e da linha do tempo
VOLUME_FONT = FontSpec(UNBOUNDED, 600, 14)
VISOR_TEXT_CHECK = FontSpec(
    NOTO, 700, 20
)  # o visor desenha em Inter: o título só é conferido (direita para a esquerda)
TITLE_WEIGHT = 700
SUB_WEIGHT = 500
ROW_WEIGHT = 500

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


EXPANDED = CardLayout(
    size=(480, 840),
    x0=36,
    x1=444,
    crown=Crown(outer=(1.5, 8.0, 478.5, 136.0), outer_width=3, inner_inset=10, lights=13, light_side=4, step_deg=2.0),
    header=(76, 104),
    cabinet=True,
    vitrine=(36, 116, 444, 380),
    vitrine_radius=24,
    disc=(118, 126, 362, 370),  # 244 px, centrado na vitrine
    label_side=112,
    title_top=400,
    title_size=26,
    title_lines=2,
    title_line_h=32,
    sub_size=16,
    sub_gap=6,
    sub_line_h=20,
    timeline=(510, 530),
    chips=(548, 576),
    info=(588, 604),
    divider_top=620,
    upnext=(632, 648),
    rows_top=654,
    row_h=28,
    rows_max=4,
    row_size=15,
    divider_bottom=776,
    base=(788, 820),
)

SLIM = CardLayout(
    size=(480, 640),
    x0=24,
    x1=456,
    crown=Crown(outer=(1.5, 4.0, 478.5, 44.0), outer_width=3, inner_inset=0, lights=0, light_side=0, step_deg=2.0),
    header=(24, 52),
    cabinet=False,
    vitrine=(24, 64, 456, 284),
    vitrine_radius=20,
    disc=(142, 76, 338, 272),  # 196 px, centrado na vitrine
    label_side=90,
    title_top=300,
    title_size=24,
    title_lines=1,
    title_line_h=30,
    sub_size=15,
    sub_gap=4,
    sub_line_h=18,
    timeline=None,
    chips=(368, 396),
    info=None,
    divider_top=412,
    upnext=(424, 440),
    rows_top=444,
    row_h=26,
    rows_max=4,
    row_size=14,
    divider_bottom=562,
    base=(576, 608),
)

# Gabinete da Expandida v2 (laterais).
CABINET_COLUMNS: tuple[Span, ...] = ((0, 20), (460, 480))
CABINET_SPAN: Span = (72, 820)
TUBE_WIDTH = 8
TUBE_SPAN: Span = (88, 580)
TUBE_BUBBLES = (0.22, 0.5, 0.78)  # posição das 3 bolhas ao longo do tubo
BUBBLE_SIDE = 6
GRILL_SPAN: Span = (600, 812)
GRILL_DOT = 3
GRILL_STEP = 8
GRILL_COLUMNS = (6, 14)  # x do começo de cada coluna de pontos, dentro da lateral

# Linha do tempo da Expandida v2 (§4.4).
TIMELINE_TRACK: Span = (84, 396)  # x do trilho
TIMELINE_TRACK_H = 6
TIMELINE_HEAD = 12
TIMELINE_RING = 2
TIMELINE_MARK_H = 6
TIMELINE_MARK_W = 2
TIMELINE_ICON = 13
TIMELINE_LIVE_DOT = 8
TIMELINE_LIVE_GAP = 12  # do fim do rótulo AO VIVO ao começo do trilho
RULER_MINUTE = 60
RULER_FIVE = 300
RULER_FIVE_ABOVE = 600  # acima de 10 min, marca a cada 5 min
RULER_NONE_ABOVE = 3600  # acima de 1 h, nenhuma marca

# Chips, cabeçalho, linhas da fila e base.
CHIP_PAD = 12
CHIP_GAP = 8
CHIP_ICON = 14
CHIP_ICON_GAP = 6
CHIP_TEXT_MAX = 140
CHIP_MORE_W = 44  # reservado para o chip ``+n``
HEADER_STATE_TEXT_MAX = 90
HEADER_BOT_TEXT_MAX = 80
INFO_ICON = 14
INFO_GAP = 14
ROW_NUMBER_W = 32
ROW_DURATION_W = 44
ROW_REQUESTER_W = 64
ROW_COLUMN_GAP = 8
TRANSPORT_ICON = 20
TRANSPORT_STEP = 36
VOLUME_SLIDER_W = 160
VOLUME_SLIDER_H = 4
VOLUME_KNOB = 12
VOLUME_VALUE_W = 36
VOLUME_ICON = 20
VOLUME_ICON_GAP = 8
INFO_TEXT_MAX = 160
QUALITY_DOT = 8
TIME_TEXT_W = 56  # decorrido e total da linha do tempo
LIVE_LABEL_MAX = 120

# ---------------------------------------------------------------------------------------------------------- ícones
# Os ícones que o ``primitives.mono_icon`` não tem, numa prancheta de 128 px (reduzida ao tamanho do uso). Traço único,
# uma cor; ``CLEAR`` abre o vazado.

CLEAR: RGBA = (0, 0, 0, 0)


def _star(points: int, outer: float, inner: float) -> list[tuple[float, float]]:
    out = []
    for i in range(points * 2):
        radius = outer if i % 2 == 0 else inner
        angle = -math.pi / 2 + i * math.pi / points
        out.append((64 + radius * math.cos(angle), 64 + radius * math.sin(angle)))
    return out


Shape = Callable[[ImageDraw.ImageDraw, RGBA], None]


def _play(d: ImageDraw.ImageDraw, c: RGBA) -> None:
    d.polygon([(36, 22), (36, 106), (106, 64)], fill=c)


def _pause(d: ImageDraw.ImageDraw, c: RGBA) -> None:
    d.rectangle((32, 24, 56, 104), fill=c)
    d.rectangle((72, 24, 96, 104), fill=c)


def _stop(d: ImageDraw.ImageDraw, c: RGBA) -> None:
    d.rectangle((28, 28, 100, 100), fill=c)


def _playpause(d: ImageDraw.ImageDraw, c: RGBA) -> None:
    d.polygon([(10, 28), (10, 100), (60, 64)], fill=c)
    d.rectangle((72, 28, 88, 100), fill=c)
    d.rectangle((100, 28, 116, 100), fill=c)


def _repeat(d: ImageDraw.ImageDraw, c: RGBA) -> None:
    d.rounded_rectangle((16, 36, 112, 96), radius=26, outline=c, width=11)
    d.rectangle((56, 28, 78, 50), fill=CLEAR)
    d.polygon([(50, 22), (50, 62), (78, 42)], fill=c)


def _sparkle(d: ImageDraw.ImageDraw, c: RGBA) -> None:
    d.polygon(_star(4, 56, 14), fill=c)


def _moon(d: ImageDraw.ImageDraw, c: RGBA) -> None:
    d.ellipse((14, 14, 114, 114), fill=c)
    d.ellipse((46, 0, 128, 82), fill=CLEAR)


def _lock(d: ImageDraw.ImageDraw, c: RGBA) -> None:
    d.arc((38, 14, 90, 74), 180, 360, fill=c, width=11)
    d.rectangle((38, 42, 49, 62), fill=c)
    d.rectangle((79, 42, 90, 62), fill=c)
    d.rounded_rectangle((24, 58, 104, 114), radius=10, fill=c)


def _person(d: ImageDraw.ImageDraw, c: RGBA) -> None:
    d.ellipse((42, 10, 86, 54), fill=c)
    d.pieslice((18, 62, 110, 154), 180, 360, fill=c)


def _folder(d: ImageDraw.ImageDraw, c: RGBA) -> None:
    d.rounded_rectangle((12, 26, 60, 46), radius=6, fill=c)
    d.rounded_rectangle((12, 38, 116, 104), radius=8, fill=c)


def _radio(d: ImageDraw.ImageDraw, c: RGBA) -> None:
    d.line((34, 46, 90, 16), fill=c, width=8)
    d.rounded_rectangle((12, 44, 116, 110), radius=12, fill=c)
    d.ellipse((68, 58, 100, 90), fill=CLEAR)
    d.rectangle((26, 62, 56, 70), fill=CLEAR)
    d.rectangle((26, 80, 56, 88), fill=CLEAR)


def _pin(d: ImageDraw.ImageDraw, c: RGBA) -> None:
    d.ellipse((30, 8, 98, 76), fill=c)
    d.polygon([(34, 56), (94, 56), (64, 120)], fill=c)
    d.ellipse((50, 28, 78, 56), fill=CLEAR)


def _ballot(d: ImageDraw.ImageDraw, c: RGBA) -> None:
    d.rectangle((44, 10, 84, 62), fill=c)
    d.rectangle((14, 52, 114, 114), fill=c)
    d.rectangle((30, 58, 98, 66), fill=CLEAR)


ICON_SHAPES: dict[str, Shape] = {
    "play": _play,
    "pause": _pause,
    "stop": _stop,
    "playpause": _playpause,
    "repeat": _repeat,
    "sparkle": _sparkle,
    "moon": _moon,
    "lock": _lock,
    "person": _person,
    "folder": _folder,
    "radio": _radio,
    "pin": _pin,
    "ballot": _ballot,
}
