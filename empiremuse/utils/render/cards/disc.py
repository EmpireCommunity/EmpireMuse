"""O disco procedural original (preto, com sulcos e brilho) em volta de um rótulo: ``record_image``. Toda medida e cor vem
de ``layout.py``."""

from __future__ import annotations

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from empiremuse.utils.render.cards.layout import (
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
    Theme,
)

CLEAR = (0, 0, 0, 0)
SHINE_WEDGES = ((292.0, 334.0), (112.0, 154.0))  # graus (sentido do Pillow), dois reflexos opostos
GROOVE_LABEL_GAP = 4  # sulcos param tantos px fora do rótulo


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
