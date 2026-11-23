"""Paleta única por faixa, quantização dos ladrilhos do disco e a codificação do GIF e do PNG. Puro: pixels entram, bytes saem.

O método é o medido na bancada do contrato (respostas do Alicerce, rodadas 1 e 2): as cores fixas do tema mais as do disco
(três quadros de amostra), ladrilhos quantizados uma vez (vão ao cache) e, por edição, só o cartão é quantizado na mesma
paleta. GIF com ``disposal=1``, ``optimize=False``, laço infinito e o quadro 0 completo; PNG com ``compress_level=6``.
"""

from __future__ import annotations

import io
from collections.abc import Sequence

from PIL import Image

from empiremuse.utils.render.budget import GIF_FRAME_MS, PALETTE_BYTES, PNG_COMPRESS_LEVEL
from empiremuse.utils.render.frames import DiscFrames

RGB = tuple[int, int, int]
MAX_COLORS = 256


def build_palette(tiles: Sequence[Image.Image], fixed: Sequence[RGB]) -> bytes:
    """A paleta (768 bytes): as cores fixas do tema primeiro e o resto tirado do disco (quadro 0 e dois do meio)."""
    if not tiles:
        raise ValueError("sem ladrilhos")
    fixed = list(dict.fromkeys(fixed))[:MAX_COLORS]
    free = MAX_COLORS - len(fixed)
    flat = [value for color in fixed for value in color]
    if free > 0:
        picks = [tiles[0], tiles[len(tiles) // 3], tiles[2 * len(tiles) // 3]]
        collage = Image.new("RGB", (picks[0].width * len(picks), picks[0].height))
        for i, tile in enumerate(picks):
            collage.paste(tile.convert("RGB"), (i * picks[0].width, 0))
        sample = collage.quantize(free, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        flat += (sample.getpalette() or [])[: 3 * free]
    flat += [0] * (PALETTE_BYTES - len(flat))
    return bytes(flat[:PALETTE_BYTES])


def palette_image(palette: bytes) -> Image.Image:
    image = Image.new("P", (1, 1))
    image.putpalette(palette)
    return image


def quantize_tiles(
    tiles: Sequence[Image.Image],
    palette: bytes,
    box: tuple[int, int, int, int],
    *,
    dither_mask: Image.Image | None = None,
) -> DiscFrames:
    """Os ladrilhos RGB do disco na paleta: sem dithering nos sulcos e, dentro de ``dither_mask`` (a capa), Floyd-Steinberg."""
    width, height = box[2] - box[0], box[3] - box[1]
    pal = palette_image(palette)
    out: list[bytes] = []
    for tile in tiles:
        if tile.size != (width, height):
            raise ValueError("ladrilho fora do tamanho da caixa")
        rgb = tile.convert("RGB")
        flat = rgb.quantize(palette=pal, dither=Image.Dither.NONE)
        if dither_mask is not None:
            flat.paste(rgb.quantize(palette=pal, dither=Image.Dither.FLOYDSTEINBERG), (0, 0), dither_mask)
        out.append(flat.tobytes())
    return DiscFrames(palette=palette, tiles=tuple(out), box=box)


def _tile(frames: DiscFrames, data: bytes) -> Image.Image:
    x0, y0, x1, y1 = frames.box
    tile = Image.frombytes("P", (x1 - x0, y1 - y0), data)
    tile.putpalette(frames.palette)
    return tile


def encode_gif(card: Image.Image, frames: DiscFrames) -> bytes:
    """O GIF: o cartão quantizado na paleta da faixa com cada ladrilho colado na caixa do disco."""
    if not frames.tiles:
        raise ValueError("sem quadros")
    card_p = card.convert("RGB").quantize(palette=palette_image(frames.palette), dither=Image.Dither.NONE)
    origin = frames.box[:2]
    images = []
    for data in frames.tiles:
        frame = card_p.copy()
        frame.paste(_tile(frames, data), origin)
        images.append(frame)
    buffer = io.BytesIO()
    images[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=images[1:],
        duration=GIF_FRAME_MS,
        loop=0,
        disposal=1,
        optimize=False,
    )
    return buffer.getvalue()


def encode_png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG", compress_level=PNG_COMPRESS_LEVEL)
    return buffer.getvalue()
