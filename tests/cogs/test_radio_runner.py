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
        self.prepared = 0
        self.origin: float | None = None  # a época gravada da programação virtual (vem do banco no reinício)
        self.prepare_error: Exception | None = None
        self.skips = 0
        self.panels = 0
        self.panel_error: Exception | None = None

    def now(self):
        return self.t

    def wall(self):
        return self.t  # o relógio de parede anda junto com o falso

    def clock_origin(self):
        return self.origin

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

    async def prepare(self):
        self.prepared += 1
        if self.prepare_error is not None:
            error, self.prepare_error = self.prepare_error, None
            raise error

    async def skip_track(self):
        self.skips += 1

    async def ensure_panel(self):
        self.panels += 1
        if self.panel_error is not None:
            raise self.panel_error

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


class TestPlaylistFailure:
    @pytest.mark.asyncio
    async def test_a_playlist_that_does_not_load_goes_through_the_backoff_and_then_plays(self, caplog):
        ports = FakePorts()
        ports.prepare_error = RuntimeError("playlist sem faixas")
        runner, _, _ = make(ports)

        with caplog.at_level(logging.WARNING):
            await runner.start()

        assert runner.phase is Phase.PLAYING and ports.sleeps == [5] and ports.connects == 2
        assert "sem tocar (RuntimeError)" in caplog.text and "playlist sem faixas" not in caplog.text

    @pytest.mark.asyncio
    async def test_the_playlist_is_prepared_before_the_virtual_position_is_computed(self):
        runner, ports, _ = make()

        await runner.start()

        assert ports.prepared == 1


class TestVerdictFailure:
    """Um soluço do banco ao conferir o premium NUNCA desliga a rádio: não retoma às cegas e tenta de novo."""

    @pytest.mark.asyncio
    async def test_a_failing_check_on_reconnect_waits_for_the_next_backoff_instead_of_turning_off(self, caplog):
        ports = FakePorts()
        calls = deque([RuntimeError("mongo fora"), RuntimeError("mongo fora"), Allowed.YES])

        async def allowed():
            item = calls.popleft() if calls else Allowed.YES
            if isinstance(item, Exception):
                raise item
            return item

        runner, _, _ = make(ports)
        await runner.start()
        ports.allowed = allowed

        with caplog.at_level(logging.WARNING):
            await runner.on_disconnect()

        assert runner.phase is Phase.PLAYING and ports.offs == []
        assert ports.sleeps[:3] == [5, 10, 20] and ports.connects == 2  # só conectou depois de conseguir conferir
        assert "RuntimeError" in caplog.text and "mongo" not in caplog.text

    @pytest.mark.asyncio
    async def test_a_failing_check_on_a_presence_resume_keeps_it_paused_for_the_next_tick(self):
        runner, ports, _ = make()
        await runner.start()
        await runner.on_presence(0)
        ports.t += 31
        await runner.tick()
        await runner.on_presence(1)
        ports.t += 6

        async def broken():
            raise RuntimeError("x")

        good = ports.allowed
        ports.allowed = broken
        await runner.tick()
        assert runner.phase is Phase.PAUSED_PRESENCE and len(ports.played) == 1
        ports.allowed = good
        await runner.tick()

        assert runner.phase is Phase.PLAYING and len(ports.played) == 2

    @pytest.mark.asyncio
    async def test_a_failing_check_on_the_first_start_goes_to_the_backoff(self):
        ports = FakePorts()
        state = {"n": 0}

        async def allowed():
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("x")
            return Allowed.YES

        ports.allowed = allowed
        runner, _, _ = make(ports)

        await runner.start()

        assert runner.phase is Phase.PLAYING and ports.sleeps[0] == 5 and ports.offs == []


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


class TestStoppedMeansStopped:
    """Revisão: um runner desligado (``deactivate``) nunca é religado por uma volta que já estava agendada."""

    @pytest.mark.asyncio
    async def test_stopping_during_the_backoff_sleep_never_reconnects(self):
        ports = FakePorts()
        ports.connect_results = deque([False, False, True])
        runner, _, _ = make(ports)
        original = ports.sleep

        async def sleep_then_stop(seconds):
            await original(seconds)
            await runner.stop()  # /radio desligar chega no meio do recuo

        ports.sleep = sleep_then_stop
        await runner.start()

        assert ports.connects == 1 and ports.played == [] and runner.phase is Phase.OFF

    @pytest.mark.asyncio
    async def test_a_queued_start_that_runs_after_the_stop_does_nothing(self):
        runner, ports, _ = make()
        await runner.stop()

        assert await runner.try_start() is True
        await runner.start()

        assert ports.connects == 0 and ports.played == [] and runner.phase is Phase.OFF

    @pytest.mark.asyncio
    async def test_stopping_while_connecting_leaves_without_playing(self):
        ports = FakePorts()
        runner, _, _ = make(ports)
        original = ports.connect

        async def connect_then_stop():
            ok = await original()
            await runner.stop()
            return ok

        ports.connect = connect_then_stop
        await runner.start()

        assert ports.played == [] and runner.phase is Phase.OFF and ports.stops >= 1

    @pytest.mark.asyncio
    async def test_a_disconnect_event_after_the_stop_is_ignored(self):
        runner, ports, _ = make()
        await runner.start()
        await runner.stop()
        connects = ports.connects

        await runner.on_disconnect()

        assert ports.connects == connects and runner.phase is Phase.OFF

    @pytest.mark.asyncio
    async def test_a_shutdown_by_lost_right_also_stops_for_good(self):
        ports = FakePorts()
        runner, _, _ = make(ports)
        await runner.start()
        ports.default_verdict = Allowed.NO_PREMIUM
        ports.t += 31
        await runner.tick()

        await runner.on_disconnect()
        await runner.tick()

        assert runner.stopped and ports.offs == [Allowed.NO_PREMIUM] and ports.connects == 1


class TestSemaphoreIsNotHeldWhileBackingOff:
    @pytest.mark.asyncio
    async def test_a_radio_waiting_for_its_backoff_holds_no_boot_queue_slot(self):
        limits = ProcessLimits()
        in_backoff = asyncio.Event()
        release = asyncio.Event()
        stuck = FakePorts(listeners=0)
        stuck.connect_results = deque([False, True])
        original = stuck.sleep

        async def sleep_held(seconds):
            if seconds != limits.spacing_s:  # o recuo (o espaço entre retomadas é outro sleep)
                in_backoff.set()
                await release.wait()
            await original(seconds)

        stuck.sleep = sleep_held
        stuck_runner = RadioRunner("1:2", stuck, limits, publish=lambda s: None)

        async def boot_stuck():
            if not await limits.resumed(stuck_runner.ports, stuck_runner.try_start):
                await stuck_runner.retry()

        task = asyncio.get_running_loop().create_task(boot_stuck())
        await asyncio.wait_for(in_backoff.wait(), 1)
        assert limits.resume._value == 2  # as duas vagas livres com uma rádio inteira em recuo

        others = [RadioRunner(f"{n}:2", FakePorts(listeners=0), limits, publish=lambda s: None) for n in (2, 3, 4)]
        await asyncio.wait_for(
            asyncio.gather(*(limits.resumed(r.ports, r.try_start) for r in others)), 1
        )  # as outras sobem sem esperar a que está em recuo

        release.set()
        await asyncio.wait_for(task, 1)
        assert all(r.phase is Phase.PAUSED_PRESENCE for r in others) and stuck_runner.phase is Phase.PAUSED_PRESENCE


class TestPeriodicRecheck:
    @pytest.mark.asyncio
    async def test_a_revoked_premium_stops_a_radio_that_never_pauses(self):
        runner, ports, _ = make()  # com ouvinte o tempo todo: nunca pausa, nunca reconecta
        await runner.start()
        ports.default_verdict = Allowed.NO_PREMIUM
        ports.t += 10
        await runner.tick()
        assert runner.phase is Phase.PLAYING  # a conferência é de 30 em 30 s, não a cada passo

        ports.t += 21
        await runner.tick()

        assert runner.phase is Phase.OFF and ports.offs == [Allowed.NO_PREMIUM] and ports.stops >= 1

    @pytest.mark.asyncio
    async def test_a_radio_disabled_from_outside_is_stopped_by_the_next_recheck(self):
        runner, ports, _ = make()
        await runner.start()
        ports.default_verdict = Allowed.DISABLED
        ports.t += 31

        await runner.tick()

        assert runner.phase is Phase.OFF and ports.offs == [Allowed.DISABLED]

    @pytest.mark.asyncio
    async def test_a_recheck_that_cannot_ask_keeps_the_radio_playing(self):
        runner, ports, _ = make()
        await runner.start()

        async def broken():
            raise RuntimeError("banco fora")

        ports.allowed = broken
        ports.t += 31
        await runner.tick()

        assert runner.phase is Phase.PLAYING and ports.offs == []


class TestRacesDuringAPlay:
    @pytest.mark.asyncio
    async def test_the_owner_pausing_while_the_playlist_loads_stops_the_play(self):
        ports = FakePorts()
        original = ports.prepare

        async def prepare_then_pause():
            await original()
            ports.paused = True

        ports.prepare = prepare_then_pause
        runner, _, _ = make(ports)

        await runner.start()

        assert runner.phase is Phase.PAUSED_OWNER and ports.played == []

    @pytest.mark.asyncio
    async def test_the_last_listener_leaving_while_the_playlist_loads_keeps_the_absence_clock(self):
        ports = FakePorts()
        original = ports.prepare

        async def prepare_then_leave():
            await original()
            ports.listeners_n = 0

        ports.prepare = prepare_then_leave
        runner, _, _ = make(ports)
        await runner.start()
        assert runner.phase is Phase.PLAYING and runner.empty_since is not None

        ports.t += 31
        await runner.tick()

        assert runner.phase is Phase.PAUSED_PRESENCE  # não toca para uma call vazia até o próximo evento de voz

    @pytest.mark.asyncio
    async def test_two_ticks_at_the_same_time_resume_only_once(self):
        ports = FakePorts()
        ports.paused = True
        runner, _, _ = make(ports)
        await runner.start()
        ports.paused = False

        await asyncio.gather(runner.tick(), runner.tick())

        assert len(ports.played) == 1 and runner.phase is Phase.PLAYING


class TestVerdictDisabledShape:
    def test_every_verdict_but_yes_means_the_radio_is_not_allowed(self):
        assert {a.value for a in Allowed} == {"yes", "no_premium", "no_channel", "no_permission", "disabled"}


class TestPanelButtons:
    """R-6 (lado do runner): pausa do administrador, "próxima" com intervalo e o painel que acompanha a conexão."""

    @pytest.mark.asyncio
    async def test_admin_pause_stops_the_stream_and_nothing_resumes_it_on_its_own(self):
        runner, ports, states = make()
        await runner.start()

        assert await runner.set_admin_pause(True) is True
        await runner.on_presence(0)
        await runner.on_presence(1)
        ports.t += 600
        await runner.tick()

        assert runner.phase is Phase.PAUSED_ADMIN and ports.stops == 1 and len(ports.played) == 1
        assert states[-1] == RadioState(paused_by_admin=True)

    @pytest.mark.asyncio
    async def test_resuming_plays_again_through_the_global_queue(self):
        runner, ports, _ = make()
        await runner.start()
        await runner.set_admin_pause(True)

        assert await runner.set_admin_pause(False) is True

        assert runner.phase is Phase.PLAYING and len(ports.played) == 2 and ports.sleeps[-1] == 1.0

    @pytest.mark.asyncio
    async def test_resuming_with_an_empty_call_waits_for_a_listener(self):
        ports = FakePorts(listeners=0)
        runner, _, _ = make(ports)
        await runner.start()
        await runner.set_admin_pause(True)

        await runner.set_admin_pause(False)

        assert runner.phase is Phase.PAUSED_PRESENCE and ports.played == []

    @pytest.mark.asyncio
    async def test_pausing_twice_or_resuming_when_not_paused_changes_nothing(self):
        runner, ports, _ = make()
        await runner.start()

        assert await runner.set_admin_pause(False) is False
        assert await runner.set_admin_pause(True) is True
        assert await runner.set_admin_pause(True) is False

    @pytest.mark.asyncio
    async def test_the_admin_pause_survives_a_reconnect(self):
        runner, ports, _ = make()
        await runner.start()
        await runner.set_admin_pause(True)

        await runner.on_disconnect()

        assert runner.phase is Phase.PAUSED_ADMIN and len(ports.played) == 1

    @pytest.mark.asyncio
    async def test_the_owner_pause_wins_over_a_resume_by_the_admin(self):
        ports = FakePorts()
        runner, _, _ = make(ports)
        await runner.start()
        await runner.set_admin_pause(True)
        ports.paused = True

        await runner.set_admin_pause(False)

        assert runner.phase is Phase.PAUSED_OWNER and len(ports.played) == 1

    @pytest.mark.asyncio
    async def test_a_resume_rechecks_the_right_to_play(self):
        ports = FakePorts()
        runner, _, _ = make(ports)
        await runner.start()
        await runner.set_admin_pause(True)
        ports.verdicts = deque([Allowed.NO_PREMIUM])

        await runner.set_admin_pause(False)

        assert runner.phase is Phase.OFF and ports.offs == [Allowed.NO_PREMIUM]

    @pytest.mark.asyncio
    async def test_next_skips_only_while_playing_and_at_most_once_every_five_seconds(self):
        runner, ports, _ = make()
        await runner.start()

        assert await runner.skip() is True
        assert await runner.skip() is False  # dentro dos 5 s
        ports.t += 5
        assert await runner.skip() is True
        await runner.set_admin_pause(True)
        ports.t += 60
        assert await runner.skip() is False  # pausada: nada para pular

        assert ports.skips == 2

    @pytest.mark.asyncio
    async def test_the_panel_is_ensured_on_every_connection_and_its_failure_never_stops_the_radio(self, caplog):
        ports = FakePorts()
        ports.panel_error = RuntimeError("sem permissão com token secreto")
        runner, _, _ = make(ports)

        with caplog.at_level("WARNING"):
            await runner.start()
        await runner.on_disconnect()

        assert ports.panels == 2 and runner.phase is Phase.PLAYING
        assert "RuntimeError" in caplog.text and "secreto" not in caplog.text


class TestVirtualClockSurvivesRestart:
    """Keystone (contrato §1, Q3 do dono): a programação virtual continua depois do reinício, não volta à faixa 0."""

    @pytest.mark.asyncio
    async def test_a_restart_resumes_at_the_position_the_stored_origin_gives(self):
        ports = FakePorts()  # t = 1000
        ports.origin = 900.0  # a rádio ligou 100 s antes do reinício
        runner, _, _ = make(ports)

        await runner.start()

        # 100 s de programação: 60 + 30 = 90 s das duas primeiras, 10 s dentro da terceira
        assert ports.played == [(2, 10_000)]

    @pytest.mark.asyncio
    async def test_a_restart_long_after_wraps_around_the_playlist(self):
        ports = FakePorts()
        ports.origin = 1000.0 - (180 * 5 + 65)  # cinco voltas inteiras de 3 min e mais 65 s

        runner, _, _ = make(ports)
        await runner.start()

        assert ports.played == [(1, 5_000)]

    @pytest.mark.asyncio
    async def test_a_radio_without_a_stored_origin_starts_the_clock_now(self):
        ports = FakePorts()
        runner, _, _ = make(ports)

        await runner.start()

        assert ports.played == [(0, 0)] and runner.virtual_origin == 1000.0

    @pytest.mark.asyncio
    async def test_a_future_origin_from_a_wrong_clock_never_gives_a_negative_position(self):
        ports = FakePorts()
        ports.origin = 5000.0

        runner, _, _ = make(ports)
        await runner.start()

        assert ports.played == [(0, 0)]

    @pytest.mark.asyncio
    async def test_the_origin_is_kept_across_pauses_in_the_same_run(self):
        ports = FakePorts()
        ports.origin = 940.0
        runner, _, _ = make(ports)
        await runner.start()
        await runner.on_presence(0)
        ports.t += 31
        await runner.tick()
        await runner.on_presence(1)
        ports.t += 6
        await runner.tick()

        assert ports.played[-1] == (2, 7_000)  # 97 s desde a época: 60 + 30 das duas primeiras e 7 s na terceira
