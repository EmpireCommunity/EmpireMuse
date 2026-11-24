"""O disco da Jukebox C (``utils/render/disc_only``): arquivo pequeno e determinístico, o cache que não redesenha, o rótulo "sem
capa" que nunca busca capa e o plano B com a vaga ocupada. CoverService e vaga de mentira; sem rede nem relógio."""

import io
from types import SimpleNamespace

import pytest
from PIL import Image

from empiremuse.utils.render import disc_only as disc
from empiremuse.utils.render.slot import RenderBusy

pytestmark = pytest.mark.critical

COVER = Image.new("RGB", (240, 240), (200, 40, 90)).tobytes()


class Covers:
    def __init__(self, pixels=COVER, pending=False):
        self.calls = 0
        self.result = SimpleNamespace(pixels=pixels, pending=pending)

    async def pixels(self, raw, **_kw):
        self.calls += 1
        return self.result


class Slot:
    def __init__(self, *, pressure=0, busy=False):
        self._pressure, self._busy, self.runs = pressure, busy, 0

    def pressure(self):
        return self._pressure

    async def run(self, fn, *args, label="", **_kw):
        if self._busy:
            raise RenderBusy
        self.runs += 1
        return fn(*args)


def service(covers=None, slot=None):
    return disc.DiscService(covers or Covers(), slot=slot or Slot())


def test_the_gif_is_small_loops_forever_and_has_all_frames():
    images = disc.draw_disc(COVER)
    assert len(images.gif) <= disc.GIF_MAX_BYTES
    gif = Image.open(io.BytesIO(images.gif))
    assert gif.n_frames == disc.FRAMES and gif.size == (disc.DISC_SIDE, disc.DISC_SIDE)
    assert gif.info["loop"] == 0 and gif.info["duration"] == disc.FRAME_MS
    assert Image.open(io.BytesIO(images.png)).size == (disc.DISC_SIDE, disc.DISC_SIDE)


def test_the_same_cover_gives_the_same_bytes_and_the_name_never_carries_the_track():
    assert disc.draw_disc(COVER) == disc.draw_disc(COVER)
    name = disc.disc_name(disc.label_digest(COVER))
    assert name.startswith("disc-") and len(name) == len("disc-") + 8
    assert disc.disc_name(disc.label_digest(None)) != name


@pytest.mark.asyncio
async def test_a_second_disc_for_the_same_cover_comes_from_the_cache_without_drawing():
    slot = Slot()
    svc = service(slot=slot)
    first = await svc.disc("ref")
    second = await svc.disc("ref")
    assert first.images is not None and second.images == first.images and first.name == second.name
    assert slot.runs == 1


@pytest.mark.asyncio
async def test_a_plain_disc_never_fetches_the_cover_even_when_there_is_one():
    covers = Covers()
    result = await service(covers).disc("ref", plain=True)
    assert covers.calls == 0 and result.plain is True
    assert result.name == disc.disc_name(disc.label_digest(None))


@pytest.mark.asyncio
async def test_a_missing_or_pending_cover_gives_the_no_cover_disc_and_asks_to_try_again():
    result = await service(Covers(pixels=None, pending=True)).disc("ref")
    assert result.plain is True and result.pending is True and result.images is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("slot", [Slot(busy=True), Slot(pressure=disc.DEGRADE_AT)])
async def test_a_busy_slot_gives_no_files_so_the_caller_uses_the_cover_url(slot):
    result = await service(slot=slot).disc("ref")
    assert result.busy is True and result.images is None


@pytest.mark.asyncio
async def test_a_gif_over_the_cap_falls_back_to_the_still_png(monkeypatch):
    big = disc.DiscImages(b"x" * (disc.GIF_MAX_BYTES + 1), b"png")
    monkeypatch.setattr(disc, "draw_disc", lambda _pixels: big)
    result = await service().disc("ref")
    assert result.images == disc.DiscImages(b"png", b"png")


def test_the_cache_is_limited_by_bytes():
    cache = disc.DiscCache(max_bytes=100)
    assert cache.put("a", disc.DiscImages(b"x" * 60, b"y" * 20)) is True
    assert cache.put("b", disc.DiscImages(b"x" * 60, b"y" * 20)) is True
    assert len(cache) == 1  # o mais antigo saiu
    assert cache.put("c", disc.DiscImages(b"x" * 200, b"")) is False  # maior que o teto: não é guardado
