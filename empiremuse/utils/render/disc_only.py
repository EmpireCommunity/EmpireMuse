"""O disco da Jukebox C (Components V2): só o disco girando, 128 px, sem cartão em volta (Alicerce §3, spec §5).

- **Arquivo:** ``DISC_SIDE`` px, opaco (``#1A1240`` com o brilho violeta atrás), ``FRAMES`` quadros de ``FRAME_MS`` ms, laço
  infinito; o quadro 0 tem a capa em pé e é o PNG parado (pausa, "animação desligada", carregando).
- **Rótulo:** a capa (240×240 RGB cru do ``CoverService``) em círculo; sem capa, ou em call privada, o degradê Nebulosa com uma
  nota branca. Nunca o avatar do bot (parece capa).
- **Cache:** a chave é o ``sha256`` dos pixels do rótulo mais a versão do desenho, nunca a faixa nem o servidor; o valor é o GIF e
  o PNG já codificados, num LRU por BYTES. Repetição custa zero de CPU, e vale entre servidores (o disco não tem nome nem posição).
- **Vaga:** o desenho roda na vaga única do processo (``RenderSlot``). Vaga ocupada ou pressão alta = ``Disc.busy``: quem chama usa
  a capa por URL (plano B) e o próximo ciclo tenta de novo. ``Disc.pending`` = a capa ainda pode chegar.
"""

from __future__ import annotations

import hashlib
import io
import logging
from typing import Any, NamedTuple, Optional

from cachetools import LRUCache
from PIL import Image, ImageDraw

from empiremuse.rules.card import DEGRADE_AT, RENDER_VERSION
from empiremuse.utils.render.cards.disc import record_image
from empiremuse.utils.render.cards.layout import (
    COVER_BYTES,
    COVER_SIDE,
    GLOW_COLOR,
    LABEL_RING,
    LABEL_RING_WIDTH,
    theme_for,
)
from empiremuse.utils.render.slot import RenderBusy, RenderSlot, shared_slot

logger = logging.getLogger(__name__)

DISC_VERSION = 1  # sobe quando o desenho do disco muda (invalida o cache num deploy)
DISC_SIDE = 128
DISC_MARGIN = 5
LABEL_SIDE = 58
FRAMES = 24
FRAME_MS = 80  # o GIF mede em centésimos: 75 ms vira 70; 24 × 80 ms = 1,92 s por volta
GIF_MAX_BYTES = 256 * 1024
CACHE_BYTES = 16 * 1024 * 1024
BACKGROUND = (0x1A, 0x12, 0x40)
GLOW_ALPHA = 0.35
NEBULA = ((0x25, 0x63, 0xEB), (0x7C, 0x3A, 0xED), (0xC0, 0x26, 0xD3))
NOTE_COLOR = (255, 255, 255)
PALETTE_SAMPLES = 4  # quadros reunidos para a paleta única do GIF
SUPERSAMPLE = 4
NO_COVER = "nocover"

RGB = tuple[int, int, int]


class DiscImages(NamedTuple):
    gif: bytes
    png: bytes


class Disc(NamedTuple):
    """``images`` = os arquivos, ou ``None`` com ``busy`` (vaga ocupada: plano B). ``name`` = ``disc-<hash8>`` (sem extensão)."""

    name: str
    images: Optional[DiscImages]
    busy: bool = False
    pending: bool = False
    plain: bool = False  # o rótulo é o "sem capa"


def label_digest(cover: Optional[bytes]) -> str:
    """A identidade do que vai no rótulo; ``None`` (sem capa) tem uma só, para um GIF por processo."""
    return hashlib.sha256(cover).hexdigest() if cover is not None else NO_COVER


def disc_name(digest: str) -> str:
    """O nome do anexo: só o hash do rótulo e as versões, nunca a faixa, a call ou o servidor."""
    raw = f"{digest}:{DISC_VERSION}:{RENDER_VERSION}".encode()
    return f"disc-{hashlib.sha256(raw).hexdigest()[:8]}"


# ---------------------------------------------------------------------- desenho (síncrono, na thread da vaga)


def _lerp(a: RGB, b: RGB, t: float) -> RGB:
    return (
        int(round(a[0] + (b[0] - a[0]) * t)),
        int(round(a[1] + (b[1] - a[1]) * t)),
        int(round(a[2] + (b[2] - a[2]) * t)),
    )


def _nebula_gradient(side: int) -> Image.Image:
    """O degradê diagonal Nebulosa (azul, violeta, magenta)."""
    image = Image.new("RGB", (side, side))
    pixels = image.load()
    span = 2.0 * (side - 1)
    for y in range(side):
        for x in range(side):
            t = (x + y) / span
            pixels[x, y] = _lerp(NEBULA[0], NEBULA[1], t * 2) if t < 0.5 else _lerp(NEBULA[1], NEBULA[2], (t - 0.5) * 2)
    return image


def _note_mask(side: int) -> Image.Image:
    """Uma colcheia branca (cabeça, haste e bandeira) numa máscara ``L`` do tamanho do rótulo."""
    big = side * SUPERSAMPLE
    mask = Image.new("L", (big, big), 0)
    paint = ImageDraw.Draw(mask)
    unit = big / 100
    paint.ellipse((31 * unit, 60 * unit, 51 * unit, 76 * unit), fill=255)  # cabeça
    paint.rectangle((48 * unit, 22 * unit, 52 * unit, 68 * unit), fill=255)  # haste
    paint.polygon(
        [(52 * unit, 22 * unit), (70 * unit, 34 * unit), (70 * unit, 46 * unit), (52 * unit, 34 * unit)], fill=255
    )  # bandeira
    return mask.resize((side, side), Image.Resampling.LANCZOS)


def _circle_mask(side: int) -> Image.Image:
    big = side * SUPERSAMPLE
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
    return mask.resize((side, side), Image.Resampling.LANCZOS)


def label_image(cover: Optional[bytes], side: int = LABEL_SIDE) -> Image.Image:
    """O rótulo em círculo (RGBA) com o anel: a capa válida, senão o degradê Nebulosa com a nota."""
    art: Image.Image
    if cover is not None and len(cover) == COVER_BYTES:
        art = Image.frombytes("RGB", (COVER_SIDE, COVER_SIDE), cover).resize((side, side), Image.Resampling.LANCZOS)
    else:
        art = _nebula_gradient(side)
        art.paste(NOTE_COLOR, (0, 0), _note_mask(side))
    label = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    label.paste(art, (0, 0), _circle_mask(side))
    ImageDraw.Draw(label).ellipse((0, 0, side - 1, side - 1), outline=LABEL_RING + (255,), width=LABEL_RING_WIDTH)
    return label


def _backdrop() -> Image.Image:
    """O fundo opaco com o brilho violeta radial atrás do disco."""
    image = Image.new("RGB", (DISC_SIDE, DISC_SIDE), BACKGROUND)
    gradient = Image.radial_gradient("L")
    edge = max(1, int(gradient.getpixel((0, gradient.height // 2))))  # type: ignore[arg-type]
    peak = 255 * GLOW_ALPHA
    faded = gradient.point(lambda value: int(max(0, edge - value) / edge * peak))
    image.paste(GLOW_COLOR, (0, 0), faded.resize((DISC_SIDE, DISC_SIDE), Image.Resampling.BILINEAR))
    return image


def tiles(cover: Optional[bytes], frames: int = FRAMES) -> list[Image.Image]:
    """Um quadro RGB por giro de ``360 / frames`` graus; o 0 tem o rótulo em pé."""
    if frames < 1:
        raise ValueError("ao menos um quadro")
    theme = theme_for("black")
    disc_side = DISC_SIDE - 2 * DISC_MARGIN
    record = record_image(label_image(cover), disc_side, theme)
    backdrop = _backdrop()
    out: list[Image.Image] = []
    for index in range(frames):
        turned = record if not index else record.rotate(-360.0 * index / frames, resample=Image.Resampling.BICUBIC)
        frame = backdrop.copy()
        frame.paste(turned, (DISC_MARGIN, DISC_MARGIN), turned)
        out.append(frame)
    return out


def encode(frames: list[Image.Image]) -> DiscImages:
    """O GIF (paleta única de amostras de vários quadros, sem pontilhado) e o PNG do quadro 0."""
    step = max(1, len(frames) // PALETTE_SAMPLES)
    sample = frames[::step][:PALETTE_SAMPLES]
    sheet = Image.new("RGB", (DISC_SIDE * len(sample), DISC_SIDE))
    for n, frame in enumerate(sample):
        sheet.paste(frame, (n * DISC_SIDE, 0))
    palette = sheet.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    indexed = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]
    gif = io.BytesIO()
    indexed[0].save(
        gif, "GIF", save_all=True, append_images=indexed[1:], duration=FRAME_MS, loop=0, disposal=1, optimize=False
    )
    png = io.BytesIO()
    frames[0].save(png, "PNG", compress_level=6)
    return DiscImages(gif.getvalue(), png.getvalue())


def draw_disc(cover: Optional[bytes]) -> DiscImages:
    """Tudo de um disco: os quadros e os dois arquivos."""
    return encode(tiles(cover))


# ---------------------------------------------------------------------- cache e serviço


def _images_size(value: DiscImages) -> int:
    return len(value.gif) + len(value.png)


class DiscCache:
    """``LRUCache`` por bytes. Só no laço de eventos (nunca de dentro da thread do desenho)."""

    def __init__(self, max_bytes: int = CACHE_BYTES) -> None:
        self._cache: LRUCache[str, DiscImages] = LRUCache(maxsize=max_bytes, getsizeof=_images_size)

    def get(self, name: str) -> Optional[DiscImages]:
        return self._cache.get(name)

    def put(self, name: str, images: DiscImages) -> bool:
        try:
            self._cache[name] = images
        except ValueError:
            return False
        return True

    def __len__(self) -> int:
        return len(self._cache)


class DiscService:
    """O disco de um rótulo (``plain`` = sempre o "sem capa": call privada, rádio, ocioso)."""

    def __init__(self, covers: Any, *, cache: Optional[DiscCache] = None, slot: Optional[RenderSlot] = None) -> None:
        self.covers = covers
        self.cache = cache if cache is not None else DiscCache()
        self._slot = slot

    @property
    def slot(self) -> RenderSlot:
        return self._slot if self._slot is not None else shared_slot()

    async def disc(self, cover: object = None, *, plain: bool = False) -> Disc:
        """O disco para ``cover`` (a referência validada do ``CardSpec``, ou ``None``). Nunca levanta ``RenderBusy``."""
        pixels: Optional[bytes] = None
        pending = False
        if cover is not None and not plain:
            result = await self.covers.pixels(cover)
            pixels, pending = result.pixels, bool(result.pending)
        name = disc_name(label_digest(pixels))
        no_cover = pixels is None
        cached = self.cache.get(name)
        if cached is not None:
            return Disc(name, cached, pending=pending, plain=no_cover)
        if self.slot.pressure() >= DEGRADE_AT:
            return Disc(name, None, busy=True, pending=pending, plain=no_cover)
        try:
            images: DiscImages = await self.slot.run(draw_disc, pixels, label="disc")
        except RenderBusy:
            return Disc(name, None, busy=True, pending=pending, plain=no_cover)
        except Exception as error:  # noqa: BLE001 - sem disco, o plano B; só o tipo vai ao log
            logger.warning("Disco não saiu (%s)", type(error).__name__)
            return Disc(name, None, busy=True, pending=pending, plain=no_cover)
        if len(images.gif) > GIF_MAX_BYTES:
            logger.warning("GIF do disco acima do teto (%d bytes): fica o PNG parado", len(images.gif))
            images = DiscImages(images.png, images.png)
        self.cache.put(name, images)
        return Disc(name, images, pending=pending, plain=no_cover)


_SERVICE: Optional[DiscService] = None


def disc_service(cover_hosts: object) -> DiscService:
    """A instância do processo, criada no primeiro uso com o ``COVER_HOSTS`` já validado pelo ``config_loader``."""
    global _SERVICE
    if _SERVICE is None:
        from empiremuse.utils.render.cards.pipeline import card_pipeline

        _SERVICE = DiscService(
            card_pipeline(cover_hosts).covers
        )  # a mesma CoverService (sandbox, vaga de rede e cache)
    return _SERVICE
