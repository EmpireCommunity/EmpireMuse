"""Base do render das skins: vaga única (pressão, prazo, cancelamento, visor), cache de quadros por bytes, GIF e primitivas.

Sem relógio real nem ``sleep``: a vaga recebe um ``to_thread`` falso preso num ``asyncio.Event`` e um relógio injetado.
"""

import asyncio
import io
import logging

import pytest
from PIL import Image, ImageFont

from empiremuse.i18n import get_i18n
from empiremuse.utils.render.gif import encode_png
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
async def test_three_waiting_put_the_slot_under_pressure():
    gate = Gate()
    slot = RenderSlot(to_thread=gate)
    tasks = [asyncio.create_task(slot.run(lambda i=i: i)) for i in range(4)]
    await settle()

    pressure = slot.pressure()
    gate.open.set()

    assert pressure == 3 and gate.started == 1, "só um desenho por vez; os outros esperam a vaga"
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


# ------------------------------------------------------------------------------------------------------------ PNG


def test_png_decodes_to_the_same_size():
    card = Image.new("RGB", (120, 80), (20, 20, 30))

    with Image.open(io.BytesIO(encode_png(card)), formats=["PNG"]) as png:
        assert png.size == card.size


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
