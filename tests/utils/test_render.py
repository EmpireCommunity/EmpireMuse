"""Base do render das skins: vaga única (pressão, prazo, cancelamento, visor), cache de quadros por bytes, GIF e primitivas.

Sem relógio real nem ``sleep``: a vaga recebe um ``to_thread`` falso preso num ``asyncio.Event`` e um relógio injetado.
"""

import asyncio
import io
import logging

import pytest
from PIL import Image, ImageFont

from empiremuse.i18n import get_i18n
from empiremuse.rules.card import RENDER_VERSION, CardFormat, CardSpec, CardState, choose_format
from empiremuse.utils.render.frames import DiscFrames, FramesCache, frames_key, frames_size
from empiremuse.utils.render.gif import build_palette, encode_gif, encode_png, quantize_tiles
from empiremuse.utils.render.primitives import circle_logo, draw_clipped, fit
from empiremuse.utils.render.slot import RenderBusy, RenderSlot, shared_slot
from tests.support.radio_snapshot import snapshot

pytestmark = pytest.mark.critical

FONT = ImageFont.load_default(size=40)
PT = get_i18n().for_locale("pt-BR")


class Gate:
    """``to_thread`` falso: cada chamada espera o portão abrir e então roda ``fn`` no laço."""

    def __init__(self) -> None:
        self.open = asyncio.Event()
        self.started = 0

    async def __call__(self, fn, *args):
        self.started += 1
        await self.open.wait()
        return fn(*args)


async def settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)  # só cede a vez ao laço; não depende de tempo


@pytest.mark.asyncio
async def test_three_waiting_make_the_fourth_request_a_png_with_upgrade():
    gate = Gate()
    slot = RenderSlot(to_thread=gate)
    tasks = [asyncio.create_task(slot.run(lambda i=i: i)) for i in range(4)]
    await settle()

    pressure = slot.pressure()
    choice = choose_format(motion=True, animatable=True, pressure=pressure, cold=False, now=0.0, last_cold_gif_at=None)
    gate.open.set()

    assert pressure == 3 and gate.started == 1, "só um desenho por vez; os outros esperam a vaga"
    assert choice.fmt is CardFormat.PNG and choice.upgrade_due
    assert await asyncio.gather(*tasks) == [0, 1, 2, 3] and slot.pressure() == 0


@pytest.mark.asyncio
async def test_waiting_past_the_deadline_is_render_busy():
    gate = Gate()
    slot = RenderSlot(to_thread=gate, wait_s=0)
    holder = asyncio.create_task(slot.run(lambda: "a"))
    await settle()

    with pytest.raises(RenderBusy):
        await slot.run(lambda: "b")
    gate.open.set()
    assert await holder == "a" and slot.pressure() == 0


@pytest.mark.asyncio
async def test_a_cancelled_caller_keeps_the_slot_until_the_drawing_ends():
    gate = Gate()
    slot = RenderSlot(to_thread=gate)
    first = asyncio.create_task(slot.run(lambda: "a"))
    await settle()
    first.cancel()
    await settle()
    second = asyncio.create_task(slot.run(lambda: "b"))
    await settle()

    assert first.cancelled() and gate.started == 1 and slot.pressure() == 1, (
        "a thread ainda desenha: a vaga segue tomada"
    )
    gate.open.set()
    assert await second == "b"


@pytest.mark.asyncio
async def test_the_radio_visor_goes_through_the_shared_slot(monkeypatch, caplog):
    from empiremuse.utils.radio import panel

    ticks = iter([0.0, 2.5])
    calls = []

    async def to_thread(fn, *args):
        calls.append(args)
        return b"png"

    slot = RenderSlot(to_thread=to_thread, clock=lambda: next(ticks))
    monkeypatch.setattr(panel, "shared_slot", lambda: slot)
    with caplog.at_level(logging.WARNING):
        payload = await panel.visor_payload_async(PT, snapshot(), filter_options=[], color=1)

    assert payload["file"].fp.read() == b"png" and len(calls) == 1
    assert [r.getMessage() for r in caplog.records] == ["Render lento (visor, 2.50 s)"]
    assert shared_slot() is shared_slot(), "uma vaga por processo (por laço)"


@pytest.mark.asyncio
async def test_a_busy_slot_never_holds_the_radio_panel_creation_it_goes_out_without_the_visor(caplog):
    from empiremuse.utils.radio import panel

    gate = Gate()
    slot = RenderSlot(
        to_thread=gate, wait_s=0
    )  # a edição de sempre (sem ``now``) desiste na hora, sem esperar de verdade
    holder = asyncio.create_task(slot.run(lambda: b"x"))
    await settle()

    with caplog.at_level(logging.DEBUG, logger="empiremuse.utils.radio.panel"):
        blank = await panel.visor_payload_async(PT, snapshot(), filter_options=[], slot=slot, now=True)
    with pytest.raises(RenderBusy):
        await panel.visor_payload_async(PT, snapshot(), filter_options=[], slot=slot)
    gate.open.set()
    await holder

    assert "file" not in blank and blank["embeds"][0].image.url is None and len(blank["components"]) == 4
    assert gate.started == 1 and slot.pressure() == 0, "a criação não entrou na fila da vaga"
    assert [r.levelname for r in caplog.records] == ["DEBUG"] and "Estação" not in caplog.text


# ------------------------------------------------------------------------------------------------- cache de quadros


def frames(tile_bytes: int, tiles: int = 3) -> DiscFrames:
    return DiscFrames(palette=b"\x00" * 768, tiles=tuple(b"\x01" * tile_bytes for _ in range(tiles)), box=(0, 0, 1, 1))


@pytest.mark.parametrize(("tile_bytes", "tiles", "expected"), [(100, 2, 968), (0, 0, 768), (1000, 30, 30_768)])
def test_frames_weigh_their_bytes_not_their_count(tile_bytes, tiles, expected):
    assert frames_size(frames(tile_bytes, tiles)) == expected


def spec(**changes) -> CardSpec:
    base = CardSpec(
        "muse_expanded_black", "black", 1, RENDER_VERSION, "pt-BR", CardState.PLAYING, False, None, None, None,
        0, (), "empty", 0, frozenset(), 0, 100, "Muse", "", None, None, True,
    )  # fmt: skip
    return base._replace(**changes)


def test_frames_key_refuses_a_missing_label_digest():
    with pytest.raises(ValueError):
        frames_key(spec(), "", 30)


def test_frames_cache_never_passes_its_byte_ceiling_and_refuses_an_oversized_entry():
    cache = FramesCache(max_bytes=10_000)
    sizes = []
    for i in range(50):
        cache.put(frames_key(spec(), f"{i:064x}", 30), frames(400))
        sizes.append(cache.size)

    assert max(sizes) <= 10_000 and len(cache) < 50
    assert cache.put(frames_key(spec(), "big", 30), frames(20_000)) is False


@pytest.mark.parametrize(
    "change",
    [dict(skin="muse_slim_black"), dict(theme="white"), dict(scale=2), dict(render_version=RENDER_VERSION + 1)],
)
def test_frames_key_changes_with_everything_that_changes_the_pixels(change):
    base = frames_key(spec(), "a" * 64, 30)

    assert frames_key(spec(**change), "a" * 64, 30) != base
    assert frames_key(spec(), "b" * 64, 30) != base, "outra capa ou outro logo = outra entrada"
    assert frames_key(spec(), "a" * 64, 20) != base
    assert frames_key(spec(volume=50, hidden=True), "a" * 64, 30) == base, "só o que muda o disco entra na chave"


# ------------------------------------------------------------------------------------------------------------ GIF/PNG


def test_gif_has_every_frame_loops_forever_and_png_decodes():
    card = Image.new("RGB", (120, 80), (20, 20, 30))
    box = (10, 10, 50, 50)
    tiles = []
    for i in range(30):  # um ponto que gira: cada quadro difere do anterior depois da quantização
        tile = Image.new("RGB", (40, 40), (20, 20, 30))
        tile.paste((255, 255, 255), (i, 0, i + 4, 4))
        tiles.append(tile)
    disc = quantize_tiles(tiles, build_palette(tiles, [(20, 20, 30), (255, 255, 255)]), box)

    with Image.open(io.BytesIO(encode_gif(card, disc)), formats=["GIF"]) as gif:
        assert gif.n_frames == 30 and gif.size == card.size and gif.info["loop"] == 0 and gif.info["duration"] == 60
    with Image.open(io.BytesIO(encode_png(card)), formats=["PNG"]) as png:
        assert png.size == card.size
    with pytest.raises(ValueError):
        quantize_tiles([Image.new("RGB", (39, 40))], disc.palette, box)


# ------------------------------------------------------------------------------------------------------- primitivas


def linear_fit(text, measure, max_width):
    if measure(text) <= max_width:
        return text
    while text and measure(text + "…") > max_width:
        text = text[:-1]
    return text.rstrip() + "…"


@pytest.mark.parametrize("text", ["", "curto", "um título bem comprido de música", "palavra final   "])
def test_fit_matches_the_one_character_cut(text):
    assert fit(text, len, 12) == linear_fit(text, len, 12)


def test_fit_measures_a_huge_text_a_logarithmic_number_of_times():
    calls = []

    def measure(value):
        calls.append(len(value))
        return len(value)

    result = fit("x" * 100_000, measure, 50)

    assert result == "x" * 49 + "…" and len(calls) <= 20


def picture(fmt: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 0, 0)).save(buffer, format=fmt)
    return buffer.getvalue()


@pytest.mark.parametrize("logo", [picture("JPEG"), picture("GIF"), b"\x89PNG\r\n\x1a\nlixo", None])
def test_circle_logo_opens_only_png(logo):
    assert circle_logo(logo, 16, (1, 2, 3)).getpixel((8, 8)) == (1, 2, 3, 255)


def test_circle_logo_draws_a_png_logo():
    assert circle_logo(picture("PNG"), 16, (1, 2, 3)).getpixel((8, 8))[:3] == (200, 0, 0)


def test_clipped_text_never_paints_outside_its_box():
    base = Image.new("RGBA", (300, 160), (0, 0, 0, 0))
    box = (60, 50, 200, 110)
    draw_clipped(base, box, (40, 80), "Z" + "̶̧́" * 40 + " título enorme", FONT, (255, 255, 255))

    inside = base.getchannel("A").getbbox()
    assert (
        inside is not None
        and box[0] <= inside[0]
        and box[1] <= inside[1]
        and inside[2] <= box[2]
        and inside[3] <= box[3]
    )
