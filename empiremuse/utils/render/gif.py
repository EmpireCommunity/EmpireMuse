"""A codificação do PNG dos desenhos (o visor e o Retrato da rádio 24 h). Puro: pixels entram, bytes saem."""

from __future__ import annotations

import io

from PIL import Image

from empiremuse.utils.render.budget import PNG_COMPRESS_LEVEL


def encode_png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG", compress_level=PNG_COMPRESS_LEVEL)
    return buffer.getvalue()
