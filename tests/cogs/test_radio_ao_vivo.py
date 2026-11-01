"""A rádio ao vivo (item 8, R3): recuo de 2 a 32 s, ciclo esgotado e troca para a reserva, volta para a principal, título só tocando (runner com
portas falsas e relógio falso); a saúde das estações e o health check (R-10); o comando e a ação com estação."""

import asyncio
from types import SimpleNamespace

import pytest

from empiremuse.rules.radio import KIND_LIVE, LIVE_BACKOFF_STEPS_S, BotCandidate, check_radio, live_backoff_s, radio_id
from empiremuse.rules.radio_stations import Station
from empiremuse.utils.radio import RadioHub, actions
from empiremuse.utils.radio.actions import Actor, Refused
from empiremuse.utils.radio.health import (
    CHECK_EVERY_S,
    CYCLES_TO_MARK,
    PER_CYCLE,
    UNAVAILABLE_TTL_S,
    WINDOW_S,
    HealthChecker,
    StationHealth,
)
from empiremuse.utils.radio.live import LiveRadioRunner, StationFailure
from empiremuse.utils.radio.runner import Phase, ProcessLimits
from empiremuse.utils.radio.service import RadioState
from tests.cogs.test_radio_runner import FakePorts
from tests.persistence.test_radio_store import FakeDb

pytestmark = pytest.mark.critical

MAIN, RESERVE = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
GUILD, CHANNEL = 500, 900


class LivePorts(FakePorts):
    """O dublê da rádio ao vivo: ``play_live`` falha enquanto a estação ativa estiver na lista de quebradas."""

    def __init__(self, *, reserve=RESERVE, **kwargs):
        super().__init__(**kwargs)
        self.primary, self.reserve, self.active = MAIN, reserve, MAIN
        self.broken: set[str] = set()
        self.lavalink_down = False  # falha que NÃO é da estação
        self.live_plays: list[str] = []
        self.views: list = []
        self.exhausted: list[str] = []
        self.unavailable: set[str] = set()

    def on_reserve(self):
        return self.active != self.primary

    async def play_live(self):
        await self._hold()
        if self.lavalink_down:
            raise RuntimeError("lavalink fora")
        if self.active in self.broken:
            raise StationFailure
        self.live_plays.append(self.active)

    def update_panel(self, view):
        self.views.append(view)

    async def cycle_exhausted(self):
        self.exhausted.append(self.active)
        self.unavailable.add(self.active)

    async def switch_source(self):
        other = self.reserve if self.active == self.primary else self.primary
        if other is None or other in self.unavailable:
            return False
        self.active = other
        return True

    async def use_primary(self):
        if self.primary in self.unavailable:
            return False
        self.active = self.primary
        return True


def make_live(ports=None):
    ports = ports or LivePorts()
    states: list = []
    runner = LiveRadioRunner("1:2", ports, ProcessLimits(), publish=states.append)
    return runner, ports, states


async def run_until(runner, ports, done, *, rounds=3000):
    """Roda ``runner.start()`` até a condição valer e então o para (sem dormir de verdade: o relógio é falso)."""
    task = asyncio.ensure_future(runner.start())
    for _ in range(rounds):
        await asyncio.sleep(0)
        if done():
            break
    runner._stopped = True
    await task


class TestLiveRunner:
    @pytest.mark.asyncio
    async def test_it_plays_the_live_source_and_never_the_virtual_clock(self):
        runner, ports, states = make_live()

        await runner.start()

        assert (
            runner.phase is Phase.PLAYING and ports.live_plays == [MAIN] and ports.played == [] and ports.prepared == 0
        )
        assert states[-1] == RadioState(playing=True)

    @pytest.mark.asyncio
    async def test_the_title_is_shown_only_while_playing_and_cleared_on_the_next_start(self):
        runner, ports, states = make_live()
        await runner.start()

        runner.set_title("Artista - Faixa")
        assert states[-1].title == "Artista - Faixa" and states[-1].playing

        await runner.ports.stop_stream()
        runner._set(Phase.PAUSED_PRESENCE)
        assert states[-1].title is None
        await runner._play()
        assert runner.title is None and states[-1].title is None

    @pytest.mark.asyncio
    async def test_a_repeated_title_does_not_republish(self):
        runner, _, states = make_live()
        await runner.start()
        runner.set_title("A")
        count = len(states)

        runner.set_title("A")

        assert len(states) == count

    @pytest.mark.asyncio
    async def test_a_failing_station_retries_after_2_4_8_16_32_seconds_and_never_at_once(self):
        ports = LivePorts(reserve=None)
        ports.broken = {MAIN}
        runner, _, _ = make_live(ports)

        await run_until(runner, ports, lambda: len(ports.sleeps) >= 5)

        assert ports.sleeps[:5] == [live_backoff_s(n) for n in range(5)] == [2.0, 4.0, 8.0, 16.0, 32.0]
        assert LIVE_BACKOFF_STEPS_S == (2, 4, 8, 16, 32)

    def test_the_backoff_has_a_jitter_of_20_percent(self):
        assert live_backoff_s(0, 0.0) == 1.6 and live_backoff_s(0, 1.0) == 2.4 and live_backoff_s(9, 0.5) == 32.0

    @pytest.mark.asyncio
    async def test_after_a_cycle_of_five_failures_it_records_the_station_and_switches_to_the_reserve(self):
        ports = LivePorts()
        ports.broken = {MAIN}
        runner, _, states = make_live(ports)

        await runner.start()

        assert ports.exhausted == [MAIN] and ports.live_plays == [RESERVE]
        assert runner.phase is Phase.PLAYING and states[-1].on_reserve is True

    @pytest.mark.asyncio
    async def test_with_both_stations_out_the_radio_stays_in_the_call_silent_and_retries_once_a_minute(self):
        ports = LivePorts()
        ports.broken = {MAIN, RESERVE}
        runner, _, states = make_live(ports)

        await run_until(runner, ports, lambda: runner.outage and len(ports.sleeps) >= 14)

        assert runner.outage and states[-1].offline and ports.played == [] and ports.live_plays == []
        assert set(ports.sleeps[-3:]) == {60.0}  # uma tentativa por minuto, sem martelar a estação
        assert ports.exhausted[:2] == [MAIN, RESERVE]

    @pytest.mark.asyncio
    async def test_without_a_reserve_configured_it_goes_straight_to_the_silent_wait(self):
        ports = LivePorts(reserve=None)
        ports.broken = {MAIN}
        runner, _, _ = make_live(ports)

        await run_until(runner, ports, lambda: runner.outage)

        assert runner.outage and ports.exhausted == [MAIN] and ports.live_plays == []

    @pytest.mark.asyncio
    async def test_when_the_stream_drops_while_playing_it_comes_back_by_the_backoff(self):
        runner, ports, _ = make_live()
        await runner.start()

        await runner.on_stream_lost()

        assert (
            runner.phase is Phase.PLAYING
            and ports.live_plays == [MAIN, MAIN]
            and ports.sleeps[-1] == 2.0
            and ports.stops >= 1
        )

    @pytest.mark.asyncio
    async def test_a_stream_event_while_paused_or_stopped_is_ignored(self):
        runner, ports, _ = make_live(LivePorts(listeners=0))
        await runner.start()
        assert runner.phase is Phase.PAUSED_PRESENCE

        await runner.on_stream_lost()

        assert ports.live_plays == [] and ports.sleeps == []

    @pytest.mark.asyncio
    async def test_a_station_that_plays_and_drops_at_once_still_closes_the_cycle_and_is_switched(self):
        runner, ports, _ = make_live()
        await runner.start()

        for _ in range(5):  # toca, cai em segundos, volta pelo recuo... cinco vezes
            ports.t += 3
            await runner.on_stream_lost()

        assert ports.exhausted == [MAIN] and ports.active == RESERVE and runner.phase is Phase.PLAYING

    @pytest.mark.asyncio
    async def test_drops_after_a_stable_stretch_never_close_the_cycle(self):
        runner, ports, _ = make_live()
        await runner.start()

        for _ in range(8):
            ports.t += 300  # tocou por mais de 2 min antes de cair
            await runner.on_stream_lost()

        assert ports.exhausted == [] and ports.active == MAIN and runner.failures == 0

    @pytest.mark.asyncio
    async def test_a_stable_playing_stretch_resets_the_failure_count_on_the_next_tick(self):
        runner, ports, _ = make_live()
        await runner.start()
        ports.t += 3
        await runner.on_stream_lost()
        assert runner.failures == 1

        ports.t += 300
        await runner.tick()

        assert runner.failures == 0

    @pytest.mark.asyncio
    async def test_failures_that_are_not_the_stations_never_count_or_mark_it(self):
        ports = LivePorts()
        ports.lavalink_down = True
        runner, _, _ = make_live(ports)

        await run_until(runner, ports, lambda: len(ports.sleeps) >= 12)

        assert ports.exhausted == [] and ports.active == MAIN and runner.failures == 0 and not runner.outage

    @pytest.mark.asyncio
    async def test_a_voice_connection_that_keeps_failing_never_counts_against_the_station(self):
        ports = LivePorts()
        ports.connect_results.extend([False] * 12)
        runner, _, _ = make_live(ports)

        await run_until(runner, ports, lambda: ports.connects >= 12)

        assert ports.exhausted == [] and ports.live_plays == [] and ports.active == MAIN

    @pytest.mark.asyncio
    async def test_when_the_main_station_is_rehabilitated_a_radio_on_the_reserve_goes_back(self):
        ports = LivePorts()
        ports.broken = {MAIN}
        runner, _, states = make_live(ports)
        await runner.start()
        assert ports.active == RESERVE

        ports.broken = set()
        ports.unavailable.discard(MAIN)  # o health check a reabilitou
        assert await runner.restore_primary() is True

        assert ports.active == MAIN and ports.live_plays[-1] == MAIN and states[-1].on_reserve is False

    @pytest.mark.asyncio
    async def test_restore_primary_does_nothing_if_it_is_still_unavailable_or_not_on_the_reserve(self):
        ports = LivePorts()
        ports.broken = {MAIN}
        runner, _, _ = make_live(ports)
        await runner.start()  # foi para a reserva e a principal está marcada

        assert await runner.restore_primary() is False and ports.active == RESERVE

        fresh, _, _ = make_live()
        await fresh.start()
        assert await fresh.restore_primary() is False

    @pytest.mark.asyncio
    async def test_with_nobody_listening_the_live_source_is_not_opened(self):
        runner, ports, _ = make_live(LivePorts(listeners=0))

        await runner.start()

        assert ports.live_plays == [] and runner.phase is Phase.PAUSED_PRESENCE

    @pytest.mark.asyncio
    async def test_the_owners_pause_all_keeps_the_live_source_closed(self):
        ports = LivePorts()
        ports.paused = True
        runner, _, _ = make_live(ports)

        await runner.start()

        assert ports.live_plays == [] and runner.phase is Phase.PAUSED_OWNER


# ---------------------------------------------------------------------- saúde (R-10)


class Clock:
    def __init__(self):
        self.now = 10_000.0

    def __call__(self):
        return self.now


def make_health(clock=None):
    db = FakeDb()
    return StationHealth(SimpleNamespace(database=db), clock=clock or Clock()), db


class TestStationHealth:
    @pytest.mark.asyncio
    async def test_three_exhausted_cycles_in_one_hour_make_the_station_unavailable_for_thirty_minutes(self):
        clock = Clock()
        health, _ = make_health(clock)

        results = []
        for _ in range(CYCLES_TO_MARK):
            results.append(await health.record_exhausted(MAIN))
            clock.now += 600

        assert results == [False, False, True] and health.unavailable(MAIN)
        clock.now += 1799 - 600  # ainda dentro dos 30 min (1800 s) contados da marca
        assert health.unavailable(MAIN)
        clock.now += 2
        assert not health.unavailable(MAIN)
        assert (
            UNAVAILABLE_TTL_S == 1800 and WINDOW_S == 3600
        )  # os números do contrato (R-10), não os que o código escolher

    @pytest.mark.asyncio
    async def test_three_failures_inside_the_hour_mark_it_but_the_same_three_spread_over_two_hours_do_not(self):
        close, _ = make_health(clock := Clock())
        for _ in range(3):
            marked = await close.record_exhausted(MAIN)
            clock.now += 1700  # 3400 s no total: dentro de 1 h
        assert marked is True

        spread_clock = Clock()
        spread, _ = make_health(spread_clock)
        for _ in range(3):
            marked = await spread.record_exhausted(MAIN)
            spread_clock.now += 1900  # 3800 s entre a primeira e a terceira: a primeira já saiu da janela
        assert marked is False and not spread.unavailable(MAIN)

    @pytest.mark.asyncio
    async def test_failures_more_than_an_hour_apart_never_mark_it(self):
        clock = Clock()
        health, _ = make_health(clock)

        for _ in range(6):
            assert await health.record_exhausted(MAIN) is False
            clock.now += 3601  # mais de 1 h (3600 s) entre uma falha e a seguinte

        assert not health.unavailable(MAIN)

    @pytest.mark.asyncio
    async def test_marks_from_another_process_are_seen_after_a_refresh(self):
        clock = Clock()
        health, db = make_health(clock)
        other = StationHealth(SimpleNamespace(database=db), clock=clock)
        for _ in range(CYCLES_TO_MARK):
            await other.record_exhausted(MAIN)

        assert not health.unavailable(MAIN)
        await health.refresh()
        assert health.unavailable(MAIN) and health.unavailable_uuids() == [MAIN]

    @pytest.mark.asyncio
    async def test_clear_rehabilitates_and_a_down_database_never_raises(self):
        health, db = make_health()
        for _ in range(CYCLES_TO_MARK):
            await health.record_exhausted(MAIN)
        await health.clear(MAIN)
        assert not health.unavailable(MAIN)

        db.fail = True
        assert await health.record_exhausted(RESERVE) is False
        await health.refresh()
        await health.clear(RESERVE)

    @pytest.mark.parametrize("value", [None, 5, "", ["x"]])
    def test_a_non_uuid_is_never_unavailable(self, value):
        health, _ = make_health()

        assert health.unavailable(value) is False


def uuid_n(n):
    return f"00000000-0000-0000-0000-{n:012d}"


class HubWorld:
    def __init__(self, n_unavailable, referenced):
        self.db = FakeDb()
        self.pool = SimpleNamespace(database=self.db)
        self.clock = Clock()
        self.hub = SimpleNamespace(health=StationHealth(self.pool, clock=self.clock), directory=self)
        self.by_uuid_calls: list[str] = []
        self.url_calls = 0  # /json/url (clique): o health check NUNCA chama
        self.probed: list[str] = []
        self.recovered: list[str] = []
        self.in_flight = self.max_in_flight = 0
        self.good: set[str] = set()
        self.n_unavailable = n_unavailable
        self.referenced = set(referenced)

    async def prepare(self):
        for n in range(self.n_unavailable):
            self.hub.health._until[uuid_n(n)] = self.clock.now + 1000
            await self.db.save_doc_cas(
                uuid_n(n),
                {"_id": uuid_n(n), "until": self.clock.now + 1000, "fails": [], "rev": 1},
                expected_rev=None,
                db_name="stations_health",
                collection="global",
            )

    async def by_uuid(self, uuid):
        self.by_uuid_calls.append(uuid)
        return Station(uuid, "n", "http://stream.example:8000/x", "MP3", 128, "BR")

    async def probe(self, station):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0)
        self.in_flight -= 1
        self.probed.append(station.uuid)
        return station.uuid in self.good

    async def recovered_cb(self, uuid):
        self.recovered.append(uuid)

    def checker(self):
        return HealthChecker(self.hub, self.probe, referenced=lambda: self.referenced, recovered=self.recovered_cb)


class TestHealthChecker:
    @pytest.mark.asyncio
    async def test_fifty_broken_stations_without_a_radio_cost_zero_requests(self):
        world = HubWorld(50, referenced=())
        await world.prepare()

        assert await world.checker().run_once() == 0
        assert world.by_uuid_calls == [] and world.probed == []

    @pytest.mark.asyncio
    async def test_forty_unavailable_stations_are_checked_twenty_per_cycle_two_at_a_time(self):
        world = HubWorld(40, referenced={uuid_n(n) for n in range(40)})
        await world.prepare()

        await world.checker().run_once()

        assert len(world.by_uuid_calls) == PER_CYCLE == 20 and len(set(world.by_uuid_calls)) == 20
        assert world.max_in_flight <= 2

    @pytest.mark.asyncio
    async def test_it_never_calls_the_click_endpoint_and_only_looks_at_unavailable_and_referenced_stations(self):
        world = HubWorld(
            3, referenced={uuid_n(0), uuid_n(99)}
        )  # 99 está referenciada mas saudável; 1 e 2 estão fora mas sem rádio
        await world.prepare()

        await world.checker().run_once()

        assert world.by_uuid_calls == [uuid_n(0)] and world.url_calls == 0

    @pytest.mark.asyncio
    async def test_a_station_that_plays_again_is_rehabilitated_and_the_radios_are_told(self):
        world = HubWorld(2, referenced={uuid_n(0), uuid_n(1)})
        await world.prepare()
        world.good = {uuid_n(1)}

        assert await world.checker().run_once() == 1

        assert world.recovered == [uuid_n(1)]
        assert not world.hub.health.unavailable(uuid_n(1)) and world.hub.health.unavailable(uuid_n(0))

    @pytest.mark.asyncio
    async def test_one_bad_station_never_breaks_the_cycle(self):
        world = HubWorld(2, referenced={uuid_n(0), uuid_n(1)})
        await world.prepare()
        world.good = {uuid_n(1)}
        original = world.by_uuid

        async def flaky(uuid):
            if uuid == uuid_n(0):
                raise OSError("rede")
            return await original(uuid)

        world.by_uuid = flaky  # type: ignore[method-assign]

        assert await world.checker().run_once() == 1

    def test_the_cycle_period_is_ten_minutes(self):
        assert CHECK_EVERY_S == 600


# ---------------------------------------------------------------------- ação e fiação


@pytest.fixture
def world():
    db = FakeDb()
    hub = RadioHub(SimpleNamespace(database=db))
    return SimpleNamespace(db=db, hub=hub)


ADMIN = Actor(user_id=7, manage_guild=True, manage_messages_in_channel=True)
STATION = Station(MAIN, "Boa FM", "http://stream.example:8000/x", "MP3", 128, "BR")


def bots(*ids):
    return [
        BotCandidate(
            f"TOKEN_{n}", n, main=False, radio_only=False, ready=True, in_guild=True, busy=False, has_radio_here=False
        )
        for n in ids
    ]


async def premium(world):
    await world.hub.control.load()
    await world.hub.premium.grant(GUILD, by=1)


async def turn_on(world, **over):
    args = dict(
        guild_id=GUILD,
        channel_id=CHANNEL,
        playlist=None,
        actor=ADMIN,
        candidates=bots(2, 3),
        playlist_exists=False,
        bot_can_join=True,
        station=STATION,
    )
    return await actions.turn_on(world.hub, **{**args, **over})


class TestTurnOnWithAStation:
    @pytest.mark.asyncio
    async def test_it_creates_a_live_radio_that_keeps_only_the_uuid(self, world):
        await premium(world)

        radio = await turn_on(world)

        assert radio.kind == KIND_LIVE and radio.source == {"station": MAIN, "reserve": None} and radio.enabled
        assert radio.id == radio_id(2, GUILD) and "stream.example" not in str(radio.source)

    @pytest.mark.asyncio
    async def test_an_unavailable_station_is_refused_with_nothing_reserved(self, world):
        await premium(world)
        world.hub.health._until[MAIN] = 10**12

        with pytest.raises(Refused) as refusal:
            await turn_on(world)

        assert refusal.value.code == actions.STATION_DOWN and (await world.hub.premium.fresh(GUILD)).active == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("playlist", "station"), [("Rock", STATION), (None, None)])
    async def test_a_playlist_and_a_station_together_or_neither_is_refused(self, world, playlist, station):
        await premium(world)

        with pytest.raises(Refused) as refusal:
            await turn_on(world, playlist=playlist, station=station, playlist_exists=True)

        assert refusal.value.code == actions.CHOOSE_ONE

    @pytest.mark.asyncio
    async def test_a_custom_radio_still_needs_its_playlist(self, world):
        await premium(world)

        with pytest.raises(Refused) as refusal:
            await turn_on(world, playlist="Rock", station=None, playlist_exists=False)

        assert refusal.value.code == actions.NO_PLAYLIST

    def test_a_live_document_validates_with_station_and_optional_reserve(self):
        doc = {
            "_id": radio_id(2, GUILD),
            "channel_id": str(CHANNEL),
            "kind": "live",
            "source": {"station": MAIN, "reserve": RESERVE},
        }

        checked = check_radio(doc)

        assert not hasattr(checked, "code") and checked.source["reserve"] == RESERVE


class TestManagerWiring:
    @pytest.mark.asyncio
    async def test_referenced_stations_are_those_of_live_radios_running_here(self, world):
        await premium(world)
        radio = await turn_on(world)
        world.hub.manager.ports_factory = lambda r: LivePorts()
        await world.hub.manager.activate(radio, wait=True)

        assert world.hub.manager.referenced_stations() == {MAIN}

    @pytest.mark.asyncio
    async def test_stream_lost_and_title_reach_live_runners_and_unknown_radios_are_ignored(self, world):
        await premium(world)
        radio = await turn_on(world)
        ports = LivePorts()
        world.hub.manager.ports_factory = lambda r: ports
        await world.hub.manager.activate(radio, wait=True)
        runner = world.hub.manager._runners[radio.id]
        assert isinstance(runner, LiveRadioRunner)

        world.hub.manager.note_title(radio.id, "Artista - Faixa")
        assert world.hub.service.state(radio.id).title == "Artista - Faixa"
        world.hub.manager.note_stream_lost(radio.id)
        await asyncio.gather(*list(world.hub.manager._tasks))
        world.hub.manager.note_stream_lost("9:9")  # rádio desconhecida: nada acontece

        assert ports.live_plays == [MAIN, MAIN]
        assert runner.title is None  # o título era do fluxo anterior: a nova conexão começa sem ele

    @pytest.mark.asyncio
    async def test_a_rehabilitated_main_station_brings_the_radio_back_from_the_reserve(self, world):
        await premium(world)
        radio = await turn_on(world)
        ports = LivePorts()
        ports.broken = {MAIN}
        world.hub.manager.ports_factory = lambda r: ports
        await world.hub.manager.activate(radio, wait=True)
        assert ports.active == RESERVE
        ports.broken = set()
        ports.unavailable.clear()

        await world.hub.manager.station_recovered(MAIN)
        await asyncio.gather(*list(world.hub.manager._tasks))

        assert ports.active == MAIN


class TestLivePortsAndEvents:
    def ports(self, reserve=RESERVE, clock=None):
        from empiremuse.utils.radio.live_ports import LiveRadioPorts

        health, _ = make_health(clock)
        pool = SimpleNamespace(radio=SimpleNamespace(health=health))
        radio = check_radio(
            {
                "_id": radio_id(2, GUILD),
                "channel_id": str(CHANNEL),
                "kind": "live",
                "source": {"station": MAIN, "reserve": reserve},
            }
        )
        return LiveRadioPorts(pool, radio), health

    @pytest.mark.asyncio
    async def test_the_switch_goes_to_the_other_station_only_if_it_exists_and_is_healthy(self):
        ports, health = self.ports()

        assert not ports.on_reserve()
        assert await ports.switch_source() is True and ports.on_reserve() and ports.active == RESERVE
        health._until[MAIN] = 10**12  # a principal está marcada: a reserva não volta para ela
        assert await ports.switch_source() is False and ports.active == RESERVE
        assert await ports.use_primary() is False
        del health._until[MAIN]
        assert await ports.use_primary() is True and not ports.on_reserve()

    @pytest.mark.asyncio
    async def test_no_reserve_or_a_marked_reserve_means_no_switch(self):
        ports, _ = self.ports(reserve=None)
        assert await ports.switch_source() is False

        marked, health = self.ports()
        health._until[RESERVE] = 10**12
        assert await marked.switch_source() is False

    @pytest.mark.asyncio
    async def test_the_reserve_is_reread_from_the_store_when_switching(self):
        ports, _ = self.ports(reserve=None)
        third = "33333333-3333-3333-3333-333333333333"

        async def get(radio_id):
            return check_radio(
                {
                    "_id": radio_id,
                    "channel_id": str(CHANNEL),
                    "kind": "live",
                    "source": {"station": MAIN, "reserve": third},
                }
            )

        ports.pool.radio.store = SimpleNamespace(get=get)

        assert (
            await ports.switch_source() is True and ports.active == third
        )  # escolhida na página depois que a rádio ligou

        async def broken(radio_id):
            raise ConnectionError("mongo fora")

        ports.pool.radio.store = SimpleNamespace(get=broken)
        ports.active = MAIN
        assert await ports.switch_source() is True and ports.active == third  # banco fora: mantém a que já conhecia

    def test_a_lost_or_failed_live_stream_goes_to_the_manager_but_our_own_stops_do_not(self):
        from empiremuse.utils.music.models import event_steps
        from empiremuse.wavelink import TrackEnd, TrackException, TrackStuck

        lost: list[str] = []
        manager = SimpleNamespace(note_stream_lost=lost.append)
        player = SimpleNamespace(
            radio_live=True,
            radio_id="1:2",
            bot=SimpleNamespace(pool=SimpleNamespace(radio=SimpleNamespace(manager=manager))),
        )

        def event(cls, reason=None):
            made = cls.__new__(cls)
            if reason is not None:  # TrackException/TrackStuck têm __slots__ e nenhum "reason"
                made.reason = reason
            return made

        for reason in ("FINISHED", "LOAD_FAILED"):
            assert event_steps._he_live_radio(player, event(TrackEnd, reason)) is True
        for reason in ("STOPPED", "REPLACED", "CLEANUP"):
            assert event_steps._he_live_radio(player, event(TrackEnd, reason)) is True  # consumido, mas sem aviso
        assert event_steps._he_live_radio(player, event(TrackException)) is True
        assert event_steps._he_live_radio(player, event(TrackStuck)) is True

        assert lost == ["1:2"] * 4

    def test_a_music_player_or_a_custom_radio_never_takes_the_live_path(self):
        from empiremuse.utils.music.models import event_steps
        from empiremuse.wavelink import TrackEnd

        made = TrackEnd.__new__(TrackEnd)
        made.reason = "FINISHED"

        assert event_steps._he_live_radio(SimpleNamespace(), made) is False
        assert event_steps._he_live_radio(SimpleNamespace(radio_id="1:2"), made) is False


class TestLiveLoadsOnlyOnTheLocalNode:
    """Gate de contrato R-8: o endereço do relay e o token nunca saem do nó local (nada de ``ytsearch:``, Deezer, Spotify nem outro nó)."""

    async def world_with_player(self, *, local=True):
        from unittest.mock import AsyncMock

        from empiremuse.utils.radio.live_ports import LiveRadioPorts
        from tests.cogs.test_radio_portas import BOT_ID, build as build_ports

        w = build_ports()
        radio = check_radio(
            {
                "_id": radio_id(BOT_ID, GUILD),
                "channel_id": str(CHANNEL),
                "kind": "live",
                "source": {"station": MAIN, "reserve": None},
            }
        )
        ports = LiveRadioPorts(w.ports.pool, radio)
        self.local = SimpleNamespace(
            identifier="LOCAL",
            get_tracks=AsyncMock(return_value=[SimpleNamespace(title="live", info={"uri": "http://127.0.0.1:9/r/x"})]),
        )
        self.remote = SimpleNamespace(
            identifier="PUBLICO", get_tracks=AsyncMock(return_value=[SimpleNamespace(title="outro")])
        )
        w.bot.music.nodes = {"PUBLICO": self.remote, **({"LOCAL": self.local} if local else {})}
        w.hub.directory = SimpleNamespace(fresh_for_play=AsyncMock(return_value=STATION))
        return w, ports

    @pytest.mark.asyncio
    async def test_the_stream_is_loaded_by_the_exact_local_url_on_the_local_node_only(self):
        import re

        w, ports = await self.world_with_player()
        try:
            assert await ports.connect() is True
            await ports.play_live()

            self.local.get_tracks.assert_awaited_once()
            query = self.local.get_tracks.await_args.args[0]
            assert re.fullmatch(r"http://127\.0\.0\.1:\d+/r/[A-Za-z0-9_-]{22}", query)
            assert "ytsearch" not in query and "source" not in self.local.get_tracks.await_args.kwargs
            assert (
                self.local.get_tracks.await_args.kwargs["trusted_local"] is True
            )  # o filtro de IP do nó libera SÓ esta carga
            self.remote.get_tracks.assert_not_awaited()  # nenhum outro nó
            w.music.get_tracks.assert_not_awaited()  # nem o get_tracks genérico (provedores de busca, Deezer, Spotify)
            assert w.player.process_next.await_count == 1
        finally:
            await w.hub.relay.close()

    @pytest.mark.asyncio
    async def test_with_the_real_node_guard_the_relay_load_and_play_are_not_refused_and_the_same_address_from_a_user_is(
        self,
    ):
        """Contra o filtro de IP REAL do nó (#184): a carga do relay passa e a faixa toca sem decodificar; o mesmo endereço pedido por um usuário é recusado."""
        from collections import OrderedDict
        from unittest.mock import AsyncMock

        from cachetools import TTLCache

        from empiremuse.wavelink.errors import MissingSessionID, TrackNotFound
        from empiremuse.wavelink.node import Node

        w, ports = await self.world_with_player()
        real = object.__new__(Node)
        real.players = {}
        real.session_id = None
        real._client = SimpleNamespace(bot=SimpleNamespace(music=SimpleNamespace(players={})))
        real._trusted_encoded = OrderedDict()
        real._checked_encoded = TTLCache(maxsize=64, ttl=600)
        track = SimpleNamespace(id="LIVE-ENCODED", title="live", info={"uri": "http://127.0.0.1:9/r/x"})
        real._get_tracks = AsyncMock(return_value=[track])
        real.build_track = AsyncMock(side_effect=AssertionError("a faixa do relay não deveria ser decodificada"))
        real.identifier = "LOCAL"
        w.bot.music.nodes = {"LOCAL": real}
        try:
            await ports.connect()
            await ports.play_live()  # a carga do relay NÃO é recusada pela barreira de IP do nó

            with pytest.raises(
                MissingSessionID
            ):  # e o play da faixa passa sem decodificar (chegou ao nó falso, sem sessão)
                await Node.update_player(real, 7, {"encodedTrack": "LIVE-ENCODED"}, True)
            real.build_track.assert_not_awaited()
            with pytest.raises(
                TrackNotFound
            ):  # o mesmo endereço, pedido por um usuário (sem trusted_local), é recusado
                await Node.get_tracks(real, "http://127.0.0.1:9/r/x")
        finally:
            await w.hub.relay.close()

    @pytest.mark.asyncio
    async def test_the_player_of_a_live_radio_is_created_on_the_local_node(self):
        w, ports = await self.world_with_player()
        try:
            await ports.connect()

            assert w.bot.music.get_player.call_args.kwargs["node_id"] == "LOCAL"
        finally:
            await w.hub.relay.close()

    @pytest.mark.asyncio
    async def test_without_a_local_node_nothing_is_opened_and_it_is_not_the_stations_fault(self):
        w, ports = await self.world_with_player(local=False)

        assert await ports.connect() is False  # o player nem nasce: sem nó local a conexão falha e o runner recua
        with pytest.raises(RuntimeError) as failure:
            await ports.play_live()

        assert not isinstance(failure.value, StationFailure) and w.hub.relay._sessions == {}
        self.remote.get_tracks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_stream_that_the_lavalink_requested_and_could_not_open_is_the_stations_fault(self):
        w, ports = await self.world_with_player()

        async def fail_after_touching(query, **kwargs):
            for session in w.hub.relay._sessions.values():
                session.hits += 1  # o Lavalink chegou ao relay
            raise OSError("fluxo sem áudio")

        self.local.get_tracks.side_effect = fail_after_touching
        try:
            await ports.connect()
            with pytest.raises(StationFailure):
                await ports.play_live()
            assert w.hub.relay._sessions == {}  # o token foi revogado
        finally:
            await w.hub.relay.close()

    @pytest.mark.asyncio
    async def test_a_lavalink_that_never_reached_the_relay_is_not_the_stations_fault(self):
        w, ports = await self.world_with_player()
        self.local.get_tracks.side_effect = OSError("lavalink fora")
        try:
            await ports.connect()
            with pytest.raises(OSError):
                await ports.play_live()
        finally:
            await w.hub.relay.close()


class TestLivePanel:
    """Contrato §2 e §3: o painel da call mostra título, "reserva no ar" e "fora do ar", sem passar de 1 edição a cada 15 s."""

    def test_the_runner_hands_the_panel_title_reserve_and_outage(self):
        import asyncio as aio

        async def go():
            ports = LivePorts()
            ports.broken = {MAIN}
            runner, _, _ = make_live(ports)
            await runner.start()  # falha a principal 5 vezes e vai para a reserva
            runner.set_title("Artista - Faixa")
            return ports.views

        views = aio.run(go())

        assert views[-1].title == "Artista - Faixa" and views[-1].on_reserve is True and views[-1].offline is False
        assert any(v.offline for v in views)  # durante as 5 tentativas da principal o painel mostrou "fora do ar"

    @pytest.mark.asyncio
    async def test_the_panel_text_shows_the_title_the_reserve_and_the_outage(self):
        from empiremuse.utils.radio.panel import LiveView, panel_payload

        def tr(key, **kw):
            return key + "|" + "|".join(f"{k}={v}" for k, v in sorted(kw.items()))

        def text(view):
            return panel_payload(tr, playlist_name="Boa FM", live=True, view=view)["embeds"][0].description

        assert "radio.panel.live.now|artist=Artista|song=Faixa" in text(LiveView(title="Artista - Faixa"))
        assert "radio.panel.live.now" not in text(LiveView())  # sem título do fluxo não há "Tocando agora"
        assert "panel_reserve" in text(LiveView(on_reserve=True)) and "panel_reserve" not in text(LiveView())
        assert "panel_offline" in text(LiveView(offline=True)) and "radio.panel.live.now" not in text(
            LiveView(offline=True, title="x")
        )
        assert len(text(LiveView(title="z" * 1000))) < 500  # o título tem teto

    @pytest.mark.asyncio
    async def test_a_title_every_100_ms_edits_the_panel_at_most_once_per_15_seconds_with_the_latest(self):
        from unittest.mock import AsyncMock

        from empiremuse.utils.radio.live_ports import LiveRadioPorts
        from empiremuse.utils.radio.panel import LiveView
        from tests.cogs.test_radio_portas import BOT_ID, build as build_ports

        w = build_ports()
        radio = check_radio(
            {
                "_id": radio_id(BOT_ID, GUILD),
                "channel_id": str(CHANNEL),
                "kind": "live",
                "source": {"station": MAIN, "reserve": None},
            }
        )
        ports = LiveRadioPorts(w.ports.pool, radio)
        edits: list = []
        clock = {"t": 1000.0}
        gate = asyncio.Event()

        async def fake_edit(view):
            edits.append(view)

        async def fake_sleep(seconds):
            await gate.wait()
            clock["t"] += seconds

        ports._edit_panel = fake_edit  # type: ignore[method-assign]
        ports.now = lambda: clock["t"]  # type: ignore[method-assign]
        ports.sleep = fake_sleep  # type: ignore[method-assign]
        w.hub.store.get = AsyncMock(return_value=None)

        for n in range(150):  # 15 s de títulos novos a cada 100 ms
            ports.update_panel(LiveView(title=f"faixa {n}"))
            await asyncio.sleep(0)
            clock["t"] += 0.1
        assert len(edits) == 1 and edits[0].title == "faixa 0"  # a primeira vai na hora; o resto espera a janela

        gate.set()
        for _ in range(10):
            await asyncio.sleep(0)

        assert len(edits) == 2 and edits[1].title == "faixa 149"  # uma edição só, com o título mais recente

    @pytest.mark.asyncio
    async def test_the_edit_uses_no_mentions_and_the_clean_text(self):
        from unittest.mock import AsyncMock, MagicMock

        from empiremuse.utils.radio.live_ports import LiveRadioPorts
        from empiremuse.utils.radio.panel import LiveView
        from tests.cogs.test_radio_portas import BOT_ID, build as build_ports

        w = build_ports()
        radio = check_radio(
            {
                "_id": radio_id(BOT_ID, GUILD),
                "channel_id": str(CHANNEL),
                "kind": "live",
                "source": {"station": MAIN, "reserve": None},
            }
        )
        ports = LiveRadioPorts(w.ports.pool, radio)
        ports._names[MAIN] = "Boa FM"
        message = MagicMock()
        message.edit = AsyncMock()
        w.channel.fetch_message = AsyncMock(return_value=message)
        w.bot.get_color = lambda me: 0
        created = await w.hub.store.create(radio)
        await w.hub.store.set_panel_message(created.id, 123)

        await ports._edit_panel(LiveView(title="Faixa", on_reserve=True))

        kwargs = message.edit.await_args.kwargs
        assert kwargs["allowed_mentions"].everyone is False and kwargs["allowed_mentions"].users is False
        assert "embeds" in kwargs and "components" in kwargs


class TestStationHealthCleanup:
    @pytest.mark.asyncio
    async def test_a_refresh_deletes_expired_marks_so_the_collection_does_not_grow_forever(self):
        clock = Clock()
        health, db = make_health(clock)
        for _ in range(CYCLES_TO_MARK):
            await health.record_exhausted(MAIN)
        assert MAIN in db.tables["stations_health"]

        clock.now += 1801 + 3601  # a marca venceu e as falhas saíram da janela
        await health.refresh()

        assert MAIN not in db.tables["stations_health"] and not health.unavailable(MAIN)

    @pytest.mark.asyncio
    async def test_a_refresh_keeps_a_mark_that_is_still_valid_and_recent_failures(self):
        clock = Clock()
        health, db = make_health(clock)
        for _ in range(CYCLES_TO_MARK):
            await health.record_exhausted(MAIN)
        await health.record_exhausted(RESERVE)  # uma falha só, recente

        clock.now += 60
        await health.refresh()

        assert MAIN in db.tables["stations_health"] and RESERVE in db.tables["stations_health"]


class TestRadioStaysOutOfTheRpc:
    @pytest.mark.asyncio
    async def test_a_radio_player_publishes_nothing_so_the_relay_url_never_reaches_the_call(self):
        from empiremuse.utils.music.models.player_state import PlayerStateMixin

        touched = []
        player = SimpleNamespace(radio_id="1:2", channel_id=10, guild=SimpleNamespace(voice_client=None))
        player.bot = SimpleNamespace(get_channel=lambda _id: touched.append("channel"))

        result = await PlayerStateMixin.process_rpc(player)

        assert result is None and touched == []  # saiu antes de olhar canal, faixa ou membros

    @pytest.mark.asyncio
    async def test_the_live_track_carries_neither_the_local_url_nor_a_title_derived_from_it(self):
        w, ports = await TestLiveLoadsOnlyOnTheLocalNode().world_with_player()
        try:
            await ports.connect()
            await ports.play_live()

            queued = w.player.queue[0]
            assert queued.info["uri"] == "" and queued.info["title"] == STATION.name
            assert "127.0.0.1" not in str(queued.info)
        finally:
            await w.hub.relay.close()
