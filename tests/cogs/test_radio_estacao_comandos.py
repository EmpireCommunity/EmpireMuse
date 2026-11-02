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
