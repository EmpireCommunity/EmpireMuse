"""O ciclo de vida de uma rádio (item 8, R1) com relógio falso: presença (R-5), recuo da reconexão (R-5), premium reconferido (R-1), pausa
geral do dono e os limites do processo (R-3). Nada de Discord nem Lavalink: as portas são dublês."""

import asyncio
import logging
from collections import deque

import pytest

from empiremuse.utils.radio.runner import Allowed, Phase, ProcessLimits, RadioRunner
from empiremuse.utils.radio.service import RadioState

pytestmark = pytest.mark.critical


class FakePorts:
    def __init__(self, *, listeners=1, durations=(60_000, 30_000, 90_000)):
        self.t = 1000.0
        self.jitter_value = 0.5  # sem jitter
        self.sleeps: list[float] = []
        self.verdicts: deque[Allowed] = deque()
        self.default_verdict = Allowed.YES
        self.connect_results: deque[bool] = deque()
        self.connects = 0
        self.connect_times: list[float] = []
        self.listeners_n = listeners
        self.paused = False
        self.played: list[tuple[int, int]] = []
        self.stops = 0
        self.offs: list[Allowed] = []
        self.durations = list(durations)
        self.in_flight = 0
        self.max_in_flight = 0

    def now(self):
        return self.t

    def jitter(self):
        return self.jitter_value

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds
        await asyncio.sleep(0)

    async def allowed(self):
        return self.verdicts.popleft() if self.verdicts else self.default_verdict

    async def connect(self):
        self.connects += 1
        self.connect_times.append(self.t)
        await self._hold()
        return self.connect_results.popleft() if self.connect_results else True

    async def play_virtual(self, index, position_ms):
        await self._hold()
        self.played.append((index, position_ms))

    async def _hold(self):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        for _ in range(3):
            await asyncio.sleep(0)
        self.in_flight -= 1

    async def stop_stream(self):
        self.stops += 1

    def listeners(self):
        return self.listeners_n

    def paused_all(self):
        return self.paused

    async def turn_off(self, reason):
        self.offs.append(reason)

    def durations_ms(self):
        return self.durations


def make(ports=None, limits=None):
    ports = ports or FakePorts()
    states: list = []
    runner = RadioRunner("1:2", ports, limits or ProcessLimits(), publish=states.append)
    return runner, ports, states


class TestStart:
    @pytest.mark.asyncio
    async def test_with_a_listener_it_connects_and_plays_from_the_start(self):
        runner, ports, states = make()

        await runner.start()

        assert runner.phase is Phase.PLAYING and ports.played == [(0, 0)]
        assert states[-1] == RadioState(playing=True)

    @pytest.mark.asyncio
    async def test_with_nobody_in_the_call_it_connects_and_waits_without_playing(self):
        runner, ports, _ = make(FakePorts(listeners=0))

        await runner.start()

        assert runner.phase is Phase.PAUSED_PRESENCE and ports.played == [] and ports.connects == 1

    @pytest.mark.asyncio
    async def test_stopping_forgets_the_state_and_closes_the_stream(self):
        runner, ports, states = make()
        await runner.start()

        await runner.stop()

        assert runner.phase is Phase.OFF and states[-1] is None and ports.stops == 1


class TestPresence:
    @pytest.mark.asyncio
    async def test_it_pauses_only_after_thirty_seconds_without_a_listener(self):
        runner, ports, _ = make()
        await runner.start()
        await runner.on_presence(0)

        ports.t += 29.9
        await runner.tick()
        assert runner.phase is Phase.PLAYING
        ports.t += 0.2
        await runner.tick()

        assert runner.phase is Phase.PAUSED_PRESENCE and ports.stops == 1

    @pytest.mark.asyncio
    async def test_somebody_coming_back_within_thirty_seconds_cancels_the_pause(self):
        runner, ports, _ = make()
        await runner.start()
        await runner.on_presence(0)
        ports.t += 20
        await runner.on_presence(1)
        ports.t += 20
        await runner.tick()

        assert runner.phase is Phase.PLAYING and ports.stops == 0

    @pytest.mark.asyncio
    async def test_it_resumes_five_seconds_after_a_listener_arrives_at_the_virtual_position(self):
        runner, ports, _ = make()
        await runner.start()  # t0 = 1000
        await runner.on_presence(0)
        ports.t += 31
        await runner.tick()  # pausa
        ports.t += 69  # 100 s desde o início da programação
        await runner.on_presence(1)
        ports.t += 4.9
        await runner.tick()
        assert runner.phase is Phase.PAUSED_PRESENCE
        ports.t += 0.2
        await runner.tick()

        # 105,1 s de programação: 60 + 30 = 90 s das duas primeiras, 15,1 s dentro da terceira
        assert runner.phase is Phase.PLAYING
        assert ports.played[-1] == (2, 15_100)

    @pytest.mark.asyncio
    async def test_a_burst_of_comings_and_goings_costs_at_most_one_resume(self):
        runner, ports, _ = make()
        await runner.start()
        await runner.on_presence(0)
        ports.t += 31
        await runner.tick()
        for _ in range(20):  # entra e sai em rajada, sempre dentro dos 5 s
            await runner.on_presence(1)
            ports.t += 1
            await runner.tick()
            await runner.on_presence(0)

        assert runner.phase is Phase.PAUSED_PRESENCE and len(ports.played) == 1


class TestReconnect:
    @pytest.mark.asyncio
    async def test_failed_connects_back_off_5_10_20_and_recheck_the_premium_each_time(self):
        ports = FakePorts()
        ports.connect_results = deque([False, False, False, True])
        ports.verdicts = deque([Allowed.YES] * 4)
        runner, _, _ = make(ports)

        await runner.start()

        assert ports.sleeps == [5, 10, 20] and ports.connects == 4 and runner.phase is Phase.PLAYING
        assert not ports.verdicts  # as 4 conferências foram consumidas: a inicial e uma por volta

    @pytest.mark.asyncio
    async def test_a_disconnect_returns_through_the_backoff_never_immediately(self):
        runner, ports, _ = make()
        await runner.start()

        await runner.on_disconnect()

        assert ports.sleeps[0] >= 4.0 and ports.stops == 1 and runner.phase is Phase.PLAYING

    @pytest.mark.asyncio
    async def test_the_jitter_stays_within_twenty_percent(self):
        for unit, expected in ((0.0, 4.0), (0.5, 5.0), (1.0, 6.0)):
            ports = FakePorts()
            ports.jitter_value = unit
            runner, _, _ = make(ports)
            await runner.start()

            await runner.on_disconnect()

            assert ports.sleeps[0] == pytest.approx(expected)

    @pytest.mark.asyncio
    async def test_sixty_moves_in_sixty_seconds_make_at_most_six_returns(self):
        """R-5: um script que move o bot a cada segundo. Cada queda espera o recuo do passo seguinte; ninguém volta antes."""
        ports = FakePorts()
        ports.jitter_value = 0.0  # o pior caso: recuo mínimo
        runner, _, _ = make(ports)
        await runner.start()
        started = ports.t
        initial_connects = ports.connects

        while ports.t - started < 60:
            await runner.on_disconnect()

        returns = [t for t in ports.connect_times[initial_connects:] if t - started <= 60]
        assert len(returns) <= 6

    @pytest.mark.asyncio
    async def test_a_stable_connection_restarts_the_backoff_from_the_beginning(self):
        runner, ports, _ = make()
        await runner.start()
        await runner.on_disconnect()
        await runner.on_disconnect()
        ports.t += 500  # ficou estável
        ports.sleeps.clear()

        await runner.on_disconnect()

        assert ports.sleeps[0] == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_events_during_a_reconnect_join_the_one_in_progress(self):
        runner, ports, _ = make()
        await runner.start()
        runner.phase = Phase.RECONNECTING

        await runner.on_disconnect()

        assert ports.connects == 1 and ports.stops == 0


class TestRevocationAndLostPermissions:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", [Allowed.NO_PREMIUM, Allowed.NO_CHANNEL, Allowed.NO_PERMISSION])
    async def test_losing_the_right_in_the_middle_of_a_reconnect_turns_the_radio_off(self, reason, caplog):
        ports = FakePorts()
        runner, _, states = make(ports)
        await runner.start()
        ports.verdicts = deque([reason])

        with caplog.at_level(logging.WARNING):
            await runner.on_disconnect()

        assert runner.phase is Phase.OFF and ports.offs == [reason] and states[-1] is None
        assert ports.connects == 1  # nunca tentou voltar
        assert [r.getMessage() for r in caplog.records] == [f"Rádio desligada ({reason.value})"]

    @pytest.mark.asyncio
    async def test_a_revoked_premium_stops_a_resume_by_presence(self):
        runner, ports, _ = make()
        await runner.start()
        await runner.on_presence(0)
        ports.t += 31
        await runner.tick()
        await runner.on_presence(1)
        ports.t += 6
        ports.verdicts = deque([Allowed.NO_PREMIUM])

        await runner.tick()

        assert runner.phase is Phase.OFF and ports.offs == [Allowed.NO_PREMIUM] and len(ports.played) == 1

    @pytest.mark.asyncio
    async def test_starting_without_the_right_never_connects(self):
        ports = FakePorts()
        ports.default_verdict = Allowed.NO_PREMIUM
        runner, _, _ = make(ports)

        await runner.start()

        assert ports.connects == 0 and ports.offs == [Allowed.NO_PREMIUM]


class TestOwnerPause:
    @pytest.mark.asyncio
    async def test_starting_while_paused_connects_and_stays_silent(self):
        ports = FakePorts()
        ports.paused = True
        runner, _, states = make(ports)

        await runner.start()

        assert runner.phase is Phase.PAUSED_OWNER and ports.played == [] and ports.connects == 1
        assert states[-1] == RadioState(paused_by_owner=True)

    @pytest.mark.asyncio
    async def test_a_listener_arriving_never_resumes_a_radio_paused_by_the_owner(self):
        ports = FakePorts(listeners=0)
        ports.paused = True
        runner, _, _ = make(ports)
        await runner.start()

        await runner.on_presence(1)
        ports.t += 600
        await runner.tick()

        assert runner.phase is Phase.PAUSED_OWNER and ports.played == []

    @pytest.mark.asyncio
    async def test_pausing_while_playing_stops_the_stream_and_keeps_the_radio_in_the_call(self):
        runner, ports, _ = make()
        await runner.start()
        ports.paused = True

        await runner.tick()

        assert runner.phase is Phase.PAUSED_OWNER and ports.stops == 1 and ports.connects == 1

    @pytest.mark.asyncio
    async def test_resuming_plays_when_somebody_is_there_and_waits_when_not(self):
        runner, ports, _ = make()
        ports.paused = True
        await runner.start()
        ports.paused = False
        await runner.tick()
        assert runner.phase is Phase.PLAYING

        quiet, quiet_ports, _ = make(FakePorts(listeners=0))
        quiet_ports.paused = True
        await quiet.start()
        quiet_ports.paused = False
        await quiet.tick()
        assert quiet.phase is Phase.PAUSED_PRESENCE

    @pytest.mark.asyncio
    async def test_resuming_rechecks_the_premium(self):
        runner, ports, _ = make()
        ports.paused = True
        await runner.start()
        ports.paused = False
        ports.verdicts = deque([Allowed.NO_PREMIUM])

        await runner.tick()

        assert runner.phase is Phase.OFF and ports.played == []

    @pytest.mark.asyncio
    async def test_a_disconnect_during_the_owner_pause_comes_back_still_paused(self):
        runner, ports, _ = make()
        ports.paused = True
        await runner.start()

        await runner.on_disconnect()

        assert runner.phase is Phase.PAUSED_OWNER and ports.played == []


class TestProcessLimits:
    """R-3: com 30 rádios mexendo ao mesmo tempo, o processo nunca faz mais que 2 conexões, 2 retomadas e 3 retomadas por presença."""

    @pytest.mark.asyncio
    async def test_thirty_radios_connecting_at_once_open_at_most_two_connections(self):
        limits = ProcessLimits()
        shared = FakePorts()
        runners = [RadioRunner(f"{n}:2", shared, limits, publish=lambda s: None) for n in range(30)]
        shared.listeners_n = 0  # conecta e espera: o que está sendo medido é só a conexão

        await asyncio.gather(*(r.start() for r in runners))

        assert shared.connects == 30 and shared.max_in_flight <= 2

    @pytest.mark.asyncio
    async def test_resuming_thirty_radios_after_the_owner_pause_runs_two_at_a_time_with_a_second_between(self):
        limits = ProcessLimits()
        shared = FakePorts()
        shared.paused = True
        runners = [RadioRunner(f"{n}:2", shared, limits, publish=lambda s: None) for n in range(30)]
        await asyncio.gather(*(r.start() for r in runners))
        shared.max_in_flight = 0
        shared.sleeps.clear()
        shared.paused = False

        await asyncio.gather(*(r.tick() for r in runners))

        assert all(r.phase is Phase.PLAYING for r in runners)
        assert shared.max_in_flight <= 2
        assert shared.sleeps.count(1.0) == 30  # um segundo depois de cada retomada

    @pytest.mark.asyncio
    async def test_thirty_presence_resumes_run_three_at_a_time(self):
        limits = ProcessLimits()
        shared = FakePorts()
        runners = [RadioRunner(f"{n}:2", shared, limits, publish=lambda s: None) for n in range(30)]
        await asyncio.gather(*(r.start() for r in runners))
        for r in runners:
            r.phase = Phase.PAUSED_PRESENCE
            r.listening_since = shared.t - 10
        shared.max_in_flight = 0

        await asyncio.gather(*(r.tick() for r in runners))

        assert shared.max_in_flight <= 3 and all(r.phase is Phase.PLAYING for r in runners)
