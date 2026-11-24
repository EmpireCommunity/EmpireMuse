"""O disco procedural da Expandida e da Slim: um disco preto original (sulcos e brilho), o rótulo e os ladrilhos que giram.

- **Rótulo:** a capa (RGB cru 240×240 do ``CoverService``) recortada em círculo, com anel de 2 px ``#0B0A1F``. Sem capa
  (ou bytes de outro tamanho), na call privada, no modo R e ocioso: o logo recebido em ``CardArt.logo``.
- **Vitrine:** o fundo atrás do disco (``vitrine`` e o brilho violeta) é desenhado uma vez e serve ao cartão e aos
  ladrilhos, então o ladrilho 0 é idêntico ao que o cartão estático (quadro 0 do GIF, o PNG) tem na caixa do disco.
- **Giro:** o disco inteiro (sulcos, brilho e rótulo) gira ``360 / quadros`` graus por quadro, no sentido horário; o
  quadro 0 tem a capa de pé.
Toda medida e cor vem de ``layout.py``.
"""

from __future__ import annotations

from typing import NamedTuple

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from empiremuse.rules.card import CardSpec, CardState
from empiremuse.utils.render.cards.layout import (
    COVER_BYTES,
    COVER_SIDE,
    DISC_BAND,
    DISC_BAND_EVERY,
    DISC_BODY,
    DISC_GROOVE,
    DISC_GROOVE_STEP,
    DISC_HOLE,
    DISC_RIM,
    DISC_SHINE,
    DISC_SHINE_ALPHA,
    DISC_SHINE_BLUR,
    DISC_SUPERSAMPLE,
    GLOW_ALPHA,
    GLOW_COLOR,
    LABEL_RING,
    LABEL_RING_WIDTH,
    MUSIC_STATES,
    RETRY_DISC_OPACITY,
    CardArt,
    CardLayout,
    Theme,
    theme_for,
)
from empiremuse.utils.render.primitives import circle_logo

CLEAR = (0, 0, 0, 0)
MASK_SUPERSAMPLE = 4
SHINE_WEDGES = ((292.0, 334.0), (112.0, 154.0))  # graus (sentido do Pillow), dois reflexos opostos
GLOW_SPREAD = 1.3  # raio do brilho em relação ao raio do disco
GROOVE_LABEL_GAP = 4  # sulcos param tantos px fora do rótulo


class Stage(NamedTuple):
    """O fundo da vitrine (RGB, do tamanho da vitrine) e o disco parado com o rótulo (RGBA, do tamanho da caixa)."""

    showcase: Image.Image
    record: Image.Image


def cover_image(cover: bytes | None) -> Image.Image | None:
    """A capa crua do ``CoverService`` como imagem; qualquer tamanho diferente de 240×240 RGB = sem capa."""
    if cover is None or len(cover) != COVER_BYTES:
        return None
    return Image.frombytes("RGB", (COVER_SIDE, COVER_SIDE), cover)


def uses_cover(spec: CardSpec) -> bool:
    """O rótulo pode ter a capa? Só com faixa visível (nunca call privada, R nem ocioso) e URL de capa no ``CardSpec``."""
    return not spec.hidden and spec.state in MUSIC_STATES and spec.track is not None and spec.cover is not None


def _circle_mask(side: int) -> Image.Image:
    big = side * MASK_SUPERSAMPLE
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
    return mask.resize((side, side), Image.Resampling.LANCZOS)


def label_image(spec: CardSpec, art: CardArt, side: int, theme: Theme) -> Image.Image:
    """O rótulo em círculo (RGBA ``side``×``side``): a capa quando permitida e válida, senão o logo, com o anel."""
    cover = cover_image(art.cover) if uses_cover(spec) else None
    if cover is None:
        label = circle_logo(art.logo, side, theme.line)
    else:
        label = Image.new("RGBA", (side, side), CLEAR)
        label.paste(cover.resize((side, side), Image.Resampling.LANCZOS), (0, 0), _circle_mask(side))
    ImageDraw.Draw(label).ellipse((0, 0, side - 1, side - 1), outline=LABEL_RING + (255,), width=LABEL_RING_WIDTH)
    return label


def _grooves(draw: ImageDraw.ImageDraw, big: int, label_side: int) -> None:
    center = big / 2
    radius = center - DISC_RIM * DISC_SUPERSAMPLE
    inner = (label_side / 2 + GROOVE_LABEL_GAP) * DISC_SUPERSAMPLE
    ring = 0
    while radius > inner:
        color = DISC_BAND if ring % DISC_BAND_EVERY == DISC_BAND_EVERY - 1 else DISC_GROOVE
        box = (center - radius, center - radius, center + radius, center + radius)
        draw.ellipse(box, outline=color + (255,), width=1)
        radius -= DISC_GROOVE_STEP * DISC_SUPERSAMPLE
        ring += 1


def _shine(disc: Image.Image) -> None:
    big = disc.width
    shine = Image.new("L", (big, big), 0)
    paint = ImageDraw.Draw(shine)
    for start, end in SHINE_WEDGES:
        paint.pieslice((0, 0, big - 1, big - 1), start, end, fill=DISC_SHINE_ALPHA)
    shine = ImageChops.multiply(shine.filter(ImageFilter.GaussianBlur(DISC_SHINE_BLUR)), disc.getchannel("A"))
    disc.paste(DISC_SHINE + (255,), (0, 0, big, big), shine)


def record_image(label: Image.Image, side: int, theme: Theme, *, opacity: float = 1.0) -> Image.Image:
    """O disco parado (RGBA ``side``×``side``): corpo, sulcos e brilho desenhados em 2× e reduzidos, o rótulo e o furo."""
    big = side * DISC_SUPERSAMPLE
    disc = Image.new("RGBA", (big, big), CLEAR)
    paint = ImageDraw.Draw(disc)
    paint.ellipse((0, 0, big - 1, big - 1), fill=DISC_BODY + (255,))
    _grooves(paint, big, label.width)
    _shine(disc)
    disc = disc.resize((side, side), Image.Resampling.LANCZOS)
    offset = (side - label.width) // 2
    disc.alpha_composite(label, (offset, offset))
    center = side / 2
    hole = DISC_HOLE / 2
    ImageDraw.Draw(disc).ellipse((center - hole, center - hole, center + hole, center + hole), fill=theme.vitrine)
    if opacity < 1.0:
        disc.putalpha(disc.getchannel("A").point(lambda value: int(value * opacity)))
    return disc


def _glow_mask(side: int) -> Image.Image:
    """Um círculo que some do centro (``GLOW_ALPHA``) até a borda (0): o ``radial_gradient`` do Pillow é a distância
    ao centro, que vale ``edge`` no meio da borda; tudo além disso fica em 0, sem degrau no quadrado."""
    gradient = Image.radial_gradient("L")
    edge = max(1, int(gradient.getpixel((0, gradient.height // 2))))  # type: ignore[arg-type]
    peak = 255 * GLOW_ALPHA
    faded = gradient.point(lambda value: int(max(0, edge - value) / edge * peak))
    return faded.resize((side, side), Image.Resampling.BILINEAR)


def showcase_image(layout: CardLayout, theme: Theme) -> Image.Image:
    """A vitrine (RGB do tamanho dela): fundo ``vitrine`` com cantos arredondados sobre ``card`` e o brilho violeta."""
    x0, y0, x1, y1 = layout.vitrine
    width, height = x1 - x0, y1 - y0
    panel = Image.new("RGB", (width, height), theme.vitrine)
    dx0, dy0, dx1, dy1 = layout.disc
    radius = int((dx1 - dx0) / 2 * GLOW_SPREAD)
    cx, cy = (dx0 + dx1) // 2 - x0, (dy0 + dy1) // 2 - y0
    glow = _glow_mask(2 * radius)
    panel.paste(GLOW_COLOR, (cx - radius, cy - radius, cx + radius, cy + radius), glow)
    corners = Image.new("L", (width, height), 0)
    ImageDraw.Draw(corners).rounded_rectangle((0, 0, width - 1, height - 1), radius=layout.vitrine_radius, fill=255)
    out = Image.new("RGB", (width, height), theme.card)
    out.paste(panel, (0, 0), corners)
    return out


def stage(spec: CardSpec, art: CardArt, layout: CardLayout) -> Stage:
    """A vitrine e o disco parado de um ``CardSpec`` já projetado (R reconectando: o disco a 60%)."""
    theme = theme_for(spec.theme)
    side = layout.disc[2] - layout.disc[0]
    opacity = RETRY_DISC_OPACITY if spec.state is CardState.RADIO_RETRY else 1.0
    record = record_image(label_image(spec, art, layout.label_side, theme), side, theme, opacity=opacity)
    return Stage(showcase_image(layout, theme), record)


def tiles(scene: Stage, layout: CardLayout, frames: int) -> list[Image.Image]:
    """Um ladrilho RGB do tamanho da caixa do disco por quadro; o quadro ``i`` gira ``i * 360 / frames`` graus."""
    if frames < 1:
        raise ValueError("ao menos um quadro")
    vx, vy = layout.vitrine[:2]
    dx0, dy0, dx1, dy1 = layout.disc
    backdrop = scene.showcase.crop((dx0 - vx, dy0 - vy, dx1 - vx, dy1 - vy))
    out: list[Image.Image] = []
    for index in range(frames):
        turned = scene.record
        if index:
            turned = scene.record.rotate(-360.0 * index / frames, resample=Image.Resampling.BICUBIC)
        tile = backdrop.copy()
        tile.paste(turned, (0, 0), turned)
        out.append(tile)
    return out


def card_tiles(spec: CardSpec, art: CardArt, layout: CardLayout, frames: int) -> list[Image.Image]:
    """Os ladrilhos de um ``CardSpec`` já projetado: o 0 é o que ``draw`` cola na caixa do disco."""
    if frames < 1:
        raise ValueError("ao menos um quadro")
    return tiles(stage(spec, art, layout), layout, frames)
