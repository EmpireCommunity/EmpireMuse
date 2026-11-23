"""O pipeline do cartão (``utils/render/cards/pipeline.py``): formato e orçamento do GIF, anexo mantido, vaga ocupada,
um desenho por player e a capa que ainda chega. Capa, vaga e relógio de mentira; sem rede nem sleep."""

import asyncio
import pytest

from empiremuse.rules.card import CardFormat, CardState, cover_digest
from empiremuse.utils.music.covers import CoverResult
from empiremuse.utils.render.budget import GIF_MAX_BYTES
from empiremuse.utils.render.cards import expanded, pipeline
from empiremuse.utils.render.cards.pipeline import CardPipeline
from empiremuse.utils.render.slot import RenderBusy
from empiremuse.utils.render.state import RenderStates
from tests.utils.test_render_cards import RecordingBook, RecordingTr, base_spec, cover_bytes, logo_png, radio_facts

pytestmark = [pytest.mark.critical, pytest.mark.asyncio]

SLIM = {"skin": "muse_slim_black", "motion": False}


class Covers:
    def __init__(self, result=None):
        self.result = result if result is not None else CoverResult(cover_bytes())
        self.asked = []

    async def pixels(self, raw):
        self.asked.append(raw)
        return self.result


class Slot:
    """A vaga sem thread: ``gate`` segura a entrada; ``busy`` = sempre ocupada."""

    def __init__(self, *, busy=False):
        self.busy = busy
        self.gate = None
        self.runs = 0

    def pressure(self):
        return 0

    async def run(self, fn, *args, label="render", wait_s=None):
        if self.busy:
            raise RenderBusy
        if self.gate is not None:
            await self.gate.wait()
        self.runs += 1
        return fn(*args)


@pytest.fixture(scope="module")
def font_book():
    return RecordingBook()


def make(font_book, *, covers=None, slot=None):
    return CardPipeline(
        covers or Covers(),
        slot=slot or Slot(),
        book=font_book,
        states=RenderStates(),
        clock=lambda: 100.0,
        bot_logo=logo_png(),
    )


class Player:
    """Um player de mentira: o estado de render é guardado por referência fraca, como o player real."""


async def test_a_moving_disc_is_a_gif_within_budget_and_the_same_spec_keeps_the_attachment(font_book):
    pipe, who, spec = make(font_book), Player(), base_spec()

    first = await pipe.render(who, "expanded", spec, RecordingTr())
    again = await pipe.render(who, "expanded", spec, RecordingTr())

    assert first.key.fmt is CardFormat.GIF and first.filename == "card.gif"
    assert first.data[:6] == b"GIF89a" and len(first.data) <= GIF_MAX_BYTES
    assert pipe.frames.size > 0
    assert (again.key, again.data) == (first.key, None) and pipe.slot.runs == 1


@pytest.mark.parametrize("change", [{"motion": False}, {"scale": 2}])
async def test_no_motion_or_a_card_above_the_measured_size_is_a_png_and_allocates_no_frames(
    font_book, monkeypatch, change
):
    def no_tiles(*_args):
        raise AssertionError("ladrilhos alocados")

    monkeypatch.setattr(expanded, "disc_tiles", no_tiles)

    done = await make(font_book).render(Player(), "expanded", base_spec(**change), RecordingTr())

    assert done.key.fmt is CardFormat.PNG and done.data[:8] == b"\x89PNG\r\n\x1a\n"


async def test_a_gif_over_the_byte_ceiling_goes_out_as_png(font_book, monkeypatch):
    monkeypatch.setattr(pipeline, "GIF_MAX_BYTES", 10)

    done = await make(font_book).render(Player(), "expanded", base_spec(), RecordingTr())

    assert done.key.fmt is CardFormat.PNG and done.data[:4] == b"\x89PNG"


async def test_a_busy_slot_keeps_the_attachment_and_asks_for_a_redraw(font_book):
    slot = Slot()
    pipe, who = make(font_book, slot=slot), Player()
    shown = await pipe.render(who, "slim", base_spec(**SLIM), RecordingTr())
    slot.busy = True

    busy = await pipe.render(who, "slim", base_spec(**SLIM, volume=10), RecordingTr())

    assert busy.busy and busy.data is None and busy.key == shown.key
    assert pipe.states.get(who).upgrade_due is True


async def test_a_busy_slot_without_any_attachment_returns_no_key(font_book):
    busy = await make(font_book, slot=Slot(busy=True)).render(Player(), "expanded", base_spec(), RecordingTr())

    assert busy.key is None and busy.data is None


async def test_only_the_newest_request_of_a_player_is_drawn(font_book):
    slot = Slot()
    slot.gate = asyncio.Event()
    pipe, who = make(font_book, slot=slot), Player()
    older = asyncio.ensure_future(pipe.render(who, "slim", base_spec(**SLIM, volume=1), RecordingTr()))
    newer = asyncio.ensure_future(pipe.render(who, "slim", base_spec(**SLIM, volume=2), RecordingTr()))
    for _ in range(5):  # os dois chegam à vaga
        await asyncio.sleep(0)
    slot.gate.set()

    old, new = await older, await newer

    assert old.stale and old.data is None and slot.runs == 2
    assert new.data is not None and new.key.spec.volume == 2
    assert pipe.states.get(who).last == new.key


async def test_a_cover_still_on_its_way_draws_the_logo_and_asks_for_a_redraw(font_book):
    pipe, who = make(font_book, covers=Covers(CoverResult(None, pending=True))), Player()

    done = await pipe.render(who, "expanded", base_spec(), RecordingTr())

    assert done.key.cover_digest is None and pipe.states.get(who).upgrade_due is True


async def test_a_private_call_never_asks_for_a_cover(font_book):
    covers = Covers()

    await make(font_book, covers=covers).render(Player(), "expanded", base_spec(hidden=True, cover=None), RecordingTr())

    assert covers.asked == []


async def test_the_radio_card_uses_the_station_logo(font_book):
    asked = []

    async def station_logo(uuid):
        asked.append(uuid)
        return logo_png()

    spec = base_spec(
        skin="muse_radio_black",
        state=CardState.RADIO_LIVE,
        track=None,
        cover=None,
        radio=radio_facts(logo_key="u-1"),
        motion=False,
    )

    done = await make(font_book).render(Player(), "visor_jukebox", spec, RecordingTr(), station_logo=station_logo)

    assert done.key.fmt is CardFormat.PNG and asked == ["u-1"]


# ---------------------------------------------------------------------- rodada de correções do S5


class DrawnThenWait(Slot):
    """A vaga que desenha e só devolve quando ``after`` abre (o desenho terminou na thread, o laço ainda não viu)."""

    def __init__(self):
        super().__init__()
        self.after = None
        self.level = 0

    def pressure(self):
        return self.level

    async def run(self, fn, *args, label="render", wait_s=None):
        self.runs += 1
        done = fn(*args)
        if self.after is not None:
            await self.after.wait()
        return done


@pytest.mark.regression
async def test_a_drawing_already_superseded_is_dropped_after_the_slot(font_book):
    slot = DrawnThenWait()
    pipe, who = make(font_book, slot=slot), Player()
    paused = base_spec(**SLIM)
    shown = await pipe.render(who, "slim", paused, RecordingTr())
    slot.after = asyncio.Event()
    older = asyncio.ensure_future(pipe.render(who, "slim", base_spec(**SLIM, volume=10), RecordingTr()))
    for _ in range(5):  # o mais velho já desenhou e espera a volta da thread
        await asyncio.sleep(0)

    newer = await pipe.render(who, "slim", paused, RecordingTr())  # o mesmo de antes: mantém o anexo na hora
    slot.after.set()
    old = await older

    assert newer.key == shown.key and newer.data is None
    assert old.stale and old.data is None and pipe.states.get(who).last == shown.key


@pytest.mark.regression
@pytest.mark.parametrize("became_private", [False, True])
async def test_a_busy_slot_keeps_the_attachment_only_when_privacy_did_not_change(font_book, became_private):
    slot = Slot()
    pipe, who = make(font_book, slot=slot), Player()
    shown = await pipe.render(who, "slim", base_spec(**SLIM), RecordingTr())
    slot.busy = True
    change = {"hidden": True, "track": None, "cover": None} if became_private else {"volume": 10}

    busy = await pipe.render(who, "slim", base_spec(**SLIM, **change), RecordingTr())

    assert busy.busy and busy.data is None
    assert busy.key == (None if became_private else shown.key)
    assert pipe.states.get(who).upgrade_due is True


@pytest.mark.regression
async def test_a_failing_gif_goes_out_as_png(font_book, monkeypatch):
    def broken_tiles(*_args):
        raise MemoryError

    monkeypatch.setattr(expanded, "disc_tiles", broken_tiles)

    done = await make(font_book).render(Player(), "expanded", base_spec(), RecordingTr())

    assert done.key.fmt is CardFormat.PNG and done.data[:4] == b"\x89PNG"


@pytest.mark.regression
async def test_two_cold_gifs_of_one_player_inside_the_gap_make_the_second_a_png_even_after_forget(font_book):
    covers = Covers()
    pipe, who = make(font_book, covers=covers), Player()
    first = await pipe.render(who, "expanded", base_spec(), RecordingTr())
    pipe.states.forget(who)  # mensagem nova: o anexo some, o relógio do GIF frio fica
    covers.result = CoverResult(bytes(reversed(cover_bytes())))  # outra capa: quadros frios de novo

    second = await pipe.render(who, "expanded", base_spec(cover="https://covers.example/y.jpg"), RecordingTr())

    assert first.key.fmt is CardFormat.GIF and second.key.fmt is CardFormat.PNG
    assert pipe.states.get(who).upgrade_due is True


@pytest.mark.regression
async def test_under_pressure_the_fourth_request_is_png_and_a_gif_on_screen_stays(font_book):
    slot = DrawnThenWait()
    pipe = make(font_book, slot=slot)
    shown_gif, fresh = Player(), Player()
    gif = await pipe.render(shown_gif, "expanded", base_spec(), RecordingTr())
    slot.level = 3  # três esperando a vaga

    kept = await pipe.render(shown_gif, "expanded", base_spec(), RecordingTr())
    png = await pipe.render(fresh, "expanded", base_spec(volume=10), RecordingTr())

    assert gif.key.fmt is CardFormat.GIF and (kept.key, kept.data) == (gif.key, None) and slot.runs == 2
    assert png.key.fmt is CardFormat.PNG and pipe.states.get(fresh).upgrade_due is True


@pytest.mark.regression
async def test_a_station_logo_that_is_late_draws_the_bot_logo_and_asks_for_a_redraw(font_book, monkeypatch):
    never = asyncio.Event()

    async def station_logo(_uuid):
        await never.wait()

    spec = base_spec(
        skin="muse_radio_black",
        state=CardState.RADIO_LIVE,
        track=None,
        cover=None,
        radio=radio_facts(logo_key="u-1"),
        motion=False,
    )
    monkeypatch.setattr(pipeline, "COVER_WAIT_S", 0)  # o prazo já passou: sem relógio de verdade
    pipe, who = make(font_book), Player()

    done = await pipe.render(who, "visor_jukebox", spec, RecordingTr(), station_logo=station_logo)
    never.set()

    assert done.key.fmt is CardFormat.PNG and pipe.states.get(who).upgrade_due is True


@pytest.mark.regression
async def test_a_station_logo_that_arrives_after_the_deadline_is_drawn_on_the_next_cycle(font_book, monkeypatch):
    ready = asyncio.Event()
    station = logo_png()

    async def station_logo(_uuid):
        await ready.wait()
        return station

    spec = base_spec(
        skin="muse_radio_black",
        state=CardState.RADIO_LIVE,
        track=None,
        cover=None,
        radio=radio_facts(logo_key="u-1"),
        motion=False,
    )
    pipe, who = make(font_book), Player()
    monkeypatch.setattr(pipeline, "COVER_WAIT_S", 0)
    late = await pipe.render(who, "visor_jukebox", spec, RecordingTr(), station_logo=station_logo)
    ready.set()
    monkeypatch.setattr(pipeline, "COVER_WAIT_S", 5)

    drawn = await pipe.render(who, "visor_jukebox", spec, RecordingTr(), station_logo=station_logo)

    assert late.key.cover_digest is None and drawn.data is not None
    assert drawn.key.cover_digest == cover_digest(station) and pipe.states.get(who).upgrade_due is False
