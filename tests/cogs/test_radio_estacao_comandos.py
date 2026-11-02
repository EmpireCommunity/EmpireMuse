"""`/radio station ...` (manage_guild em execução, cooldown de 5 s por servidor) e as portas da personalizada (adiantar, pular, retomar) — item 8, R5."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from empiremuse.i18n import get_i18n
from empiremuse.rules.radio_shuffle import Track
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.radio import station_commands
from empiremuse.utils.radio.custom import CustomRadioRunner
from empiremuse.utils.radio.custom_ports import CustomRadioPorts
from empiremuse.utils.radio.custom_source import NoPlayableTracks
from empiremuse.utils.radio.runner import Phase, ProcessLimits
from tests.cogs.test_radio_runner import FakePorts

pytestmark = pytest.mark.critical


@pytest.fixture(autouse=True)
def fresh_cooldown():
    station_commands.STATION_COOLDOWN._cache.clear()
    yield
    station_commands.STATION_COOLDOWN._cache.clear()


def make_inter(*, manage_guild=True, guild_id=500):
    return SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock()),
        guild=SimpleNamespace(id=guild_id, preferred_locale="pt-BR"),
        guild_id=guild_id,
        author=SimpleNamespace(id=7, guild_permissions=SimpleNamespace(manage_guild=manage_guild)),
    )


def make_cog():
    return SimpleNamespace(bot=SimpleNamespace(pool=SimpleNamespace(i18n=get_i18n())))


class TestStationCommandGate:
    @pytest.mark.asyncio
    async def test_without_manage_guild_the_command_is_refused_and_uses_no_cooldown(self):
        cog = make_cog()

        with pytest.raises(GenericError):
            await station_commands.gate(cog, make_inter(manage_guild=False))
        assert (
            await station_commands.gate(cog, make_inter()) is not None
        )  # quem pode ainda passa: a recusa não gastou a vaga

    @pytest.mark.asyncio
    async def test_a_second_command_within_five_seconds_in_the_same_server_is_refused(self):
        cog = make_cog()
        await station_commands.gate(cog, make_inter())

        with pytest.raises(GenericError):
            await station_commands.gate(cog, make_inter())
        assert await station_commands.gate(cog, make_inter(guild_id=501)) is not None  # outro servidor, outro balde

    def test_the_cooldown_is_one_use_per_five_seconds_per_server(self):
        mapping = station_commands.STATION_COOLDOWN
        assert (mapping._cooldown.rate, mapping._cooldown.per) == (1, 5)


class TestCustomPorts:
    @staticmethod
    def make_ports(*, queue=(), current=None):
        ports = object.__new__(CustomRadioPorts)
        player = SimpleNamespace(queue=list(queue), current=current, process_next=AsyncMock())
        ports._player = lambda: player
        ports.session = SimpleNamespace(next_playable=AsyncMock(return_value=(Track("a", "b"), "partial")))
        ports._stocking = asyncio.Lock()
        ports.pool = SimpleNamespace(radio=SimpleNamespace(stock_slots=asyncio.Semaphore(2)))
        ports.play_next = AsyncMock()
        ports._refresh_panel = MagicMock()
        ports.no_tracks = False
        return ports, player

    @pytest.mark.asyncio
    async def test_stocking_adds_one_track_and_never_a_second(self):
        ports, player = self.make_ports()

        await ports._stock()
        await ports._stock()

        assert player.queue == ["partial"]
        ports.session.next_playable.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_keep_playing_draws_again_when_nothing_plays_and_nothing_waits(self):
        ports, _ = self.make_ports(current=None)

        await ports.keep_playing()

        ports.play_next.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_keep_playing_only_stocks_while_something_plays(self):
        ports, player = self.make_ports(current=object())

        await ports.keep_playing()

        ports.play_next.assert_not_awaited()
        assert player.queue == ["partial"]

    @pytest.mark.asyncio
    async def test_skipping_with_a_track_waiting_plays_it_without_drawing(self):
        ports, player = self.make_ports(queue=["waiting"], current=object())

        await ports.skip_track()

        player.process_next.assert_awaited_once()
        ports.play_next.assert_not_awaited()
        ports._refresh_panel.assert_called_once()

    @pytest.mark.asyncio
    async def test_skipping_with_an_empty_queue_draws_now(self):
        ports, player = self.make_ports(current=object())

        await ports.skip_track()

        ports.play_next.assert_awaited_once()
        player.process_next.assert_not_awaited()


class TestCustomRunnerPause:
    @pytest.mark.asyncio
    async def test_a_station_without_playable_tracks_pauses_and_the_state_says_offline(self):
        ports = FakePorts()

        async def play_next():
            raise NoPlayableTracks

        ports.play_next = play_next
        runner = CustomRadioRunner("1:2", ports, ProcessLimits(), publish=lambda state: None)

        with pytest.raises(NoPlayableTracks):
            await runner._start_source()

        assert runner.no_tracks is True
        assert runner._state(Phase.PLAYING).offline is True
        assert runner._backoff() == 600.0  # a nova tentativa vem em 10 min

    @pytest.mark.asyncio
    async def test_a_successful_draw_clears_the_pause(self):
        ports = FakePorts()
        ports.play_next = AsyncMock()
        runner = CustomRadioRunner("1:2", ports, ProcessLimits(), publish=lambda state: None)
        runner.no_tracks = True

        await runner._start_source()

        assert runner.no_tracks is False


class TestTheRadioNeverStops:
    """Regra do dono: "A RÁDIO NÃO PODE PARAR, DEVE SIM PULAR PARA A PRÓXIMA MÚSICA"."""

    @staticmethod
    def make_ports(session, *, prepare=None):
        from empiremuse.utils.radio.custom_source import REBUILD_BACKOFF_S  # noqa: F401

        ports = object.__new__(CustomRadioPorts)
        player = SimpleNamespace(queue=[], current=None, process_next=AsyncMock())
        ports._player = lambda: player
        ports.session = session
        ports.pool = SimpleNamespace(radio=SimpleNamespace(stock_slots=asyncio.Semaphore(2), resolved=None))
        ports._resolve = AsyncMock()
        ports._refresh_panel = MagicMock()
        ports.searching = False
        ports.no_tracks = False
        ports.sleeps = []
        ports.shown = []

        async def sleep(seconds):
            ports.sleeps.append(seconds)
            ports.shown.append(ports.searching)  # "procurando faixas…" está ligado durante o recuo

        ports.sleep = sleep
        ports.prepare = prepare or AsyncMock()
        return ports, player

    @pytest.mark.asyncio
    async def test_five_failures_back_off_20_seconds_rebuild_the_pool_and_play_on(self):
        from empiremuse.utils.radio.custom_source import StreakLimit

        calls = []

        class Session:
            rebuilt = False

            async def next_playable(self, resolve):
                calls.append("draw")
                if len(calls) == 1:
                    raise StreakLimit
                return Track("a", "b"), "partial"

        ports, _ = self.make_ports(Session())

        track, partial = await ports._draw()

        assert (track, partial) == (Track("a", "b"), "partial")
        assert ports.sleeps == [20] and ports.shown == [True] and ports.searching is False
        ports.prepare.assert_awaited_once()  # o pool foi relido
        assert ports.no_tracks is False  # nunca pausada

    @pytest.mark.asyncio
    async def test_an_empty_pool_after_the_rebuild_is_a_broken_source_that_stops_but_stays_on(self):
        from empiremuse.utils.radio.custom_source import EmptyStation, StreakLimit

        class Session:
            async def next_playable(self, resolve):
                raise StreakLimit

        ports, _ = self.make_ports(Session(), prepare=AsyncMock(side_effect=EmptyStation))

        with pytest.raises(NoPlayableTracks):
            await ports._draw()

        assert ports.searching is False

    @pytest.mark.asyncio
    async def test_the_slot_of_the_stock_is_free_while_the_radio_backs_off(self):
        from empiremuse.utils.radio.custom_source import StreakLimit

        holder = []

        class Session:
            rebuilt = False
            first = True

            async def next_playable(self, resolve):
                if self.first:
                    self.first = False
                    raise StreakLimit
                return Track("a", "b"), "partial"

        ports, _ = self.make_ports(Session())
        slots = ports.pool.radio.stock_slots
        original_sleep = ports.sleep

        async def sleep(seconds):
            holder.append(slots._value)  # vagas livres durante o recuo
            await original_sleep(seconds)

        ports.sleep = sleep

        await ports._draw(stocking=True)

        assert holder == [2]

    def test_a_track_start_zeroes_the_streak(self):
        session = SimpleNamespace(streak=4)
        ports, _ = self.make_ports(session)
        ports.current = None

        ports.track_started()

        assert session.streak == 0

    def test_the_panel_says_searching_during_the_backoff_and_the_notice_only_for_a_broken_source(self):
        from empiremuse.utils.radio.panel import CustomView, custom_payload
        from tests.cogs.test_radio_paineis_separados import tr_pt

        base = dict(station="Rock", artist="", title="")
        searching = custom_payload(tr_pt(), view=CustomView(**base, searching=True))
        broken = custom_payload(tr_pt(), view=CustomView(**base, no_tracks=True))

        def text(payload):
            return " ".join(field.value for field in payload["embeds"][0].fields)

        assert "Procurando faixas" in text(searching) and "Procurando faixas" not in text(broken)
        assert "troque a estação ou edite-a" in text(broken)


class TestEditingWakesABrokenStation:
    @pytest.mark.asyncio
    async def test_changing_the_station_ends_the_ten_minute_wait_at_once(self):
        ports = object.__new__(CustomRadioPorts)
        ports._wake = asyncio.Event()
        ports.no_tracks, ports.searching = True, False
        ports._refresh_panel = MagicMock()

        waiting = asyncio.create_task(ports.sleep(600))
        await asyncio.sleep(0)
        await ports.apply_station(SimpleNamespace())

        await asyncio.wait_for(waiting, 1)  # acordou sem esperar os 600 s

    @pytest.mark.asyncio
    async def test_a_healthy_radio_does_not_wake_its_reconnect_backoff(self):
        ports = object.__new__(CustomRadioPorts)
        ports._wake = asyncio.Event()
        ports.no_tracks, ports.searching = False, False
        ports._refresh_panel = MagicMock()

        await ports.apply_station(SimpleNamespace())

        assert not ports._wake.is_set()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(ports.sleep(5), 0.05)


class TestABrokenSourceWaitsTenMinutes:
    """§3: pool vazio ou Last.fm fora para a rádio (continua ligada), com nova tentativa em 10 min e nenhuma consulta antes disso."""

    @staticmethod
    def make_ports(clock):
        from empiremuse.utils.radio.custom_source import EmptyStation

        ports = object.__new__(CustomRadioPorts)
        player = SimpleNamespace(queue=[], current=object(), process_next=AsyncMock())
        ports._player = lambda: player
        ports.session = SimpleNamespace(streak=5)
        ports.no_tracks, ports.searching, ports.retry_at = False, False, 0.0
        ports._refresh_panel = MagicMock()
        ports.now = lambda: clock[0]
        ports.prepare = AsyncMock(side_effect=EmptyStation)
        ports._stock = AsyncMock()
        ports.play_next = AsyncMock()
        ports._wake = asyncio.Event()
        return ports

    @pytest.mark.asyncio
    async def test_no_prepare_and_no_lastfm_call_for_ten_minutes_then_one_retry(self):
        clock = [1000.0]
        ports = self.make_ports(clock)
        ports._source_broken()
        assert ports.no_tracks is True and ports.retry_at == 1600.0

        for step in range(0, 599, 5):  # um passo do runner a cada 5 s
            clock[0] = 1000.0 + step
            await ports.keep_playing()
        ports.prepare.assert_not_awaited()
        ports.play_next.assert_not_awaited()

        clock[0] = 1600.0
        await ports.keep_playing()

        ports.prepare.assert_awaited_once()  # ainda quebrada: uma tentativa e mais 10 min
        assert ports.retry_at == 2200.0 and ports.no_tracks is True
        ports.play_next.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_when_the_source_comes_back_the_radio_plays_again(self):
        clock = [1000.0]
        ports = self.make_ports(clock)
        ports.prepare = AsyncMock()
        ports._source_broken()
        ports._player().current = None
        clock[0] = 1600.0

        await ports.keep_playing()

        ports.prepare.assert_awaited_once()
        ports.play_next.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_editing_or_changing_the_station_tries_at_once(self):
        clock = [1000.0]
        ports = self.make_ports(clock)
        ports._source_broken()

        await ports.apply_station(SimpleNamespace())

        assert ports.retry_at == 0.0 and ports._wake.is_set()  # a espera acorda e o runner reconecta com a estação nova
