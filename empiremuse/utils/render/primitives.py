"""Primitivas de desenho divididas pelo visor da rádio e pelo Retrato (extraídas de ``utils/radio/visor.py``).

- ``circle_logo`` só abre PNG (``formats=["PNG"]``): o logo vem sempre do ``LogoService``, nunca de bytes de fora.
- ``fit`` corta por busca binária: o número de medidas cresce com o log do tamanho, nunca uma por caractere.
- ``draw_clipped`` desenha o texto numa camada do tamanho da caixa: nada sai da caixa, nem marca combinante.
"""

from __future__ import annotations

import io
import math
from collections.abc import Callable

from PIL import Image, ImageDraw, ImageFont

RGB = tuple[int, int, int]
ELLIPSIS = "…"
ICON_CANVAS = 128


def mono_icon(kind: str, side: int, color: RGB) -> Image.Image:
    """Ícones monocromáticos (alto-falante, mudo, estrela, sinal, lista, fone, relógio, setas, transporte, + e −), ``side`` px."""
    im = Image.new("RGBA", (ICON_CANVAS, ICON_CANVAS), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if kind in ("speaker", "speaker_mute", "speaker_low"):
        d.rectangle((22, 50, 44, 78), fill=color)
        d.polygon([(44, 50), (74, 26), (74, 102), (44, 78)], fill=color)
        if kind == "speaker":
            d.arc((60, 40, 100, 88), start=-45, end=45, fill=color, width=9)
            d.arc((70, 24, 124, 104), start=-45, end=45, fill=color, width=9)
        elif kind == "speaker_low":
            d.arc((60, 40, 100, 88), start=-45, end=45, fill=color, width=9)
        else:
            d.line((84, 50, 112, 78), fill=color, width=9)
            d.line((112, 50, 84, 78), fill=color, width=9)
    elif kind == "star":
        points = []
        for i in range(10):
            radius = 52 if i % 2 == 0 else 22
            angle = -math.pi / 2 + i * math.pi / 5
            points.append((64 + radius * math.cos(angle), 64 + radius * math.sin(angle)))
        d.polygon(points, fill=color)
    elif kind == "signal":
        for i, h in enumerate((30, 50, 70, 90)):
            d.rounded_rectangle((20 + i * 24, 108 - h, 36 + i * 24, 108), radius=4, fill=color)
    elif kind == "list":
        for i in range(3):
            y = 30 + i * 34
            d.ellipse((12, y - 7, 26, y + 7), fill=color)
            d.rounded_rectangle((38, y - 5, 116, y + 5), radius=5, fill=color)
    elif kind == "headphone":
        d.arc((20, 24, 108, 112), start=180, end=360, fill=color, width=10)
        d.rounded_rectangle((18, 66, 42, 104), radius=8, fill=color)
        d.rounded_rectangle((86, 66, 110, 104), radius=8, fill=color)
    elif kind == "clock":
        d.ellipse((16, 16, 112, 112), outline=color, width=10)
        d.line((64, 36, 64, 66, 86, 80), fill=color, width=10)
    elif kind == "chevron_left":
        d.line((78, 28, 46, 64, 78, 100), fill=color, width=12)
    elif kind == "chevron_right":
        d.line((50, 28, 82, 64, 50, 100), fill=color, width=12)
    elif kind == "prev":
        d.rectangle((26, 30, 40, 98), fill=color)
        d.polygon([(100, 30), (100, 98), (44, 64)], fill=color)
    elif kind == "next":
        d.rectangle((88, 30, 102, 98), fill=color)
        d.polygon([(28, 30), (28, 98), (84, 64)], fill=color)
    elif kind == "plus":
        d.rectangle((56, 24, 72, 104), fill=color)
        d.rectangle((24, 56, 104, 72), fill=color)
    elif kind == "minus":
        d.rectangle((24, 56, 104, 72), fill=color)
    return im.resize((side, side), Image.Resampling.LANCZOS)


def circle_logo(logo: bytes | None, side: int, fallback: RGB) -> Image.Image:
    """O logo (PNG do ``LogoService``) recortado em círculo; sem logo, ou bytes que não são PNG, um disco na cor de reserva."""
    out = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    mask = Image.new("L", (side, side), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, side - 1, side - 1), fill=255)
    picture: Image.Image | None = None
    if logo:
        try:
            with Image.open(io.BytesIO(logo), formats=["PNG"]) as opened:
                opened.load()
                picture = opened.convert("RGBA").resize((side, side), Image.Resampling.LANCZOS)
        except Exception:  # noqa: BLE001 - logo inválido vira o disco de reserva, nunca derruba o desenho
            picture = None
    if picture is None:
        picture = Image.new("RGBA", (side, side), fallback + (255,))
    out.paste(picture, (0, 0), mask)
    return out


def fit(text: str, measure: Callable[[str], float], max_width: float) -> str:
    """``text`` inteiro se cabe; senão o maior começo que cabe com ``…`` (busca binária sobre o tamanho do começo)."""
    if measure(text) <= max_width:
        return text
    low, high = 0, len(text) - 1  # o maior começo que cabe está em [low, high]; ``text[:0]`` vale como piso
    while low < high:
        middle = (low + high + 1) // 2
        if measure(text[:middle] + ELLIPSIS) <= max_width:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + ELLIPSIS


def draw_clipped(
    base: Image.Image,
    box: tuple[int, int, int, int],
    xy: tuple[float, float],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: RGB | tuple[int, int, int, int],
    *,
    anchor: str = "lm",
) -> None:
    """Desenha ``text`` em ``xy`` (coordenadas de ``base``) sem passar de ``box``: a tinta fora da caixa é descartada."""
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        return
    layer = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((xy[0] - x0, xy[1] - y0), text, font=font, fill=fill, anchor=anchor)
    base.alpha_composite(layer, (x0, y0))
