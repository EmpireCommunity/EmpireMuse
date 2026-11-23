"""O anel "Tocou há pouco" da rádio ao vivo (D4): só títulos ICY já limpos, no máximo 5, sem repetição seguida, com a hora do relógio
injetado; limpo na troca de estação (inclusive reserva e volta) e mantido na reconexão; um anel por rádio; o retrato o lê como cópia."""

from types import SimpleNamespace

import pytest

from empiremuse.rules.radio import LIVE_CYCLE_ATTEMPTS
from empiremuse.utils.radio.manager import RadioManager
from empiremuse.utils.radio.runner import Phase
from tests.audio.test_radio_relay import MP3, Station, fetch, icy_stream, relay, sends
from tests.audio.test_radio_snapshot import TestSnapshot
from tests.cogs.test_radio_ao_vivo import MAIN, RESERVE, make_live

pytestmark = pytest.mark.critical


async def playing():
    runner, ports, _ = make_live()
    await runner.start()
    assert runner.phase is Phase.PLAYING
    return runner, ports


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("titles", "expected"),
    [
        (["A", "B", "C"], ["A", "B", "C"]),
        (["A", "A", "B", "A"], ["A", "B", "A"]),
        (["A", None, "", "B"], ["A", "B"]),
        ([str(n) for n in range(8)], ["3", "4", "5", "6", "7"]),
    ],
)
async def test_the_ring_keeps_the_last_five_distinct_titles_in_order(titles, expected):
    runner, ports = await playing()
    for title in titles:
        ports.t += 15
        runner.set_title(title)

    assert [t for _, t in runner.recent_titles] == expected
    assert all(isinstance(ts, float) for ts, _ in runner.recent_titles)


@pytest.mark.asyncio
async def test_the_time_comes_from_the_injected_wall_clock_and_the_copy_is_immutable():
    runner, ports = await playing()
    ports.t = 5000.0
    runner.set_title("A")

    copy = runner.recent_titles
    ports.t = 5015.0
    runner.set_title("B")

    assert copy == ((5000.0, "A"),) and isinstance(copy, tuple)
    assert runner.recent_titles == ((5000.0, "A"), (5015.0, "B"))


@pytest.mark.asyncio
async def test_a_reconnection_to_the_same_station_keeps_the_ring():
    runner, ports = await playing()
    runner.set_title("A")

    await ports.stop_stream()
    await runner._play()  # a mesma estação de volta: o título some, o anel fica

    assert runner.title is None and [t for _, t in runner.recent_titles] == ["A"] and ports.active == MAIN


@pytest.mark.asyncio
async def test_changing_the_station_clears_the_ring():
    runner, _ = await playing()
    runner.set_title("A")

    await runner.station_changed()

    assert runner.recent_titles == ()


@pytest.mark.asyncio
async def test_going_to_the_reserve_and_back_clears_the_ring():
    runner, ports = await playing()
    runner.set_title("Da principal")
    runner.failures = LIVE_CYCLE_ATTEMPTS
    await runner._close_cycle_if_due()  # o ciclo esgotou: a reserva entra
    assert ports.active == RESERVE and runner.recent_titles == ()

    runner.set_title("Da reserva")
    ports.unavailable.discard(MAIN)
    assert await runner.restore_primary() is True
    assert ports.active == MAIN and runner.recent_titles == ()


@pytest.mark.asyncio
async def test_two_radios_on_the_same_station_have_independent_rings():
    first, _ = await playing()
    second, _ = await playing()

    first.set_title("Só na primeira")

    assert second.recent_titles == () and len(first.recent_titles) == 1


HOSTILE = "@everyone ouçam <@&123> cargo entra em discord.gg/x [clique](https://evil.example) abc\u202edef"


@pytest.mark.asyncio
async def test_a_hostile_icy_title_reaches_the_ring_only_through_the_relay_and_note_title():
    runner, _ = await playing()
    manager = SimpleNamespace(_runners={"r1": runner})  # só o que o ``note_title`` lê
    body = icy_stream(256, MP3 + b"A" * (256 * 2 - 4), HOSTILE)
    head = b"ICY 200 OK\r\nicy-metaint:256\r\nContent-Type: audio/mpeg\r\n\r\n"
    async with Station(sends(head, body)) as station, relay() as r:
        token = r.register(
            station.url, codec="MP3", bitrate=128, on_title=lambda t: RadioManager.note_title(manager, "r1", t)
        )
        await fetch(r, token)

    ((_, text),) = runner.recent_titles
    for bad in ("@everyone", "<@&", "discord.gg", "](", "://", "\u202e"):
        assert bad not in text


@pytest.mark.asyncio
async def test_the_live_snapshot_carries_a_copy_of_the_ring():
    from types import SimpleNamespace

    from empiremuse.utils.radio.snapshot import live_snapshot

    hub, radio, ports, runner, _ = TestSnapshot.world()
    runner = SimpleNamespace(**vars(runner), recent_titles=((9_000.0, "A"), (9_015.0, "B")))

    snap = await live_snapshot(hub, radio, ports, runner, locale="pt-BR")
    without = await live_snapshot(hub, radio, ports, None, locale="pt-BR")

    assert snap.recent_titles == ((9_000.0, "A"), (9_015.0, "B")) and without.recent_titles == ()
