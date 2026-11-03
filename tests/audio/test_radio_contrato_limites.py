"""Os números do contrato R5 fixados com LITERAIS (não importados): mudar um deles quebra aqui, e a mutação não sobrevive."""

import random
from types import SimpleNamespace

import pytest

from empiremuse.rules import radio_shuffle, radio_station
from empiremuse.rules.radio_shuffle import Track, next_pick, remember
from empiremuse.utils.radio import custom_source, lastfm_feed
from empiremuse.utils.radio.custom import CustomRadioRunner
from empiremuse.utils.radio.custom_source import CustomSession, ResolvedTracks, StreakLimit
from empiremuse.utils.radio.runner import Phase, ProcessLimits
from tests.cogs.test_radio_runner import FakePorts
from tests.persistence.test_radio_store import FakeDb

pytestmark = pytest.mark.critical


def test_the_contract_numbers_are_the_literal_ones():
    assert custom_source.FAIL_STREAK_MAX == 5
    assert custom_source.FAIL_TTL_S == 86_400
    assert custom_source.RESOLVED_TTL_S == 604_800
    assert custom_source.RETRY_AFTER_S == 600 and custom_source.REBUILD_BACKOFF_S == 20
    assert custom_source.STOCK_SLOTS == 2
    assert lastfm_feed.RATE_PER_S == 1.0 and lastfm_feed.BURST == 3.0
    assert lastfm_feed.FRESH_S == 86_400 and lastfm_feed.STALE_S == 604_800
    assert lastfm_feed.TAG_LIMIT == 100 and lastfm_feed.TAG_PAGES == 2 and lastfm_feed.ARTIST_LIMIT == 30
    assert lastfm_feed.MAX_BODY == 524_288
    assert radio_shuffle.TRACK_WINDOW == 50 and radio_shuffle.ARTIST_WINDOW == 4
    assert radio_station.MAX_STATIONS_PER_GUILD == 10 and radio_station.MAX_ARTISTS == 10
    assert (
        radio_station.MAX_PLAYLISTS == 5 and radio_station.POOL_MAX == 300 and radio_station.POOL_PER_ARTIST_MAX == 30
    )
    assert radio_station.NAME_MAX == 32


class TestTheDrawWindows:
    def test_no_track_returns_within_fifty_and_no_artist_within_four(self):
        pool = [Track(f"Artista {i % 30}", f"Faixa {i}") for i in range(120)]  # 30 artistas, 4 faixas cada
        rng, history = random.Random(7), []
        for _ in range(400):
            pick = next_pick(pool, history, rng)
            assert pick not in history[-50:]
            assert all(pick.artist != t.artist for t in history[-4:])
            history = remember(history, pick)

    def test_the_windows_fall_by_half_when_nothing_is_left(self):
        pool = [Track("Só", "1"), Track("Só", "2")]  # um artista: a janela de artista cai a zero e a de faixa a 1

        picks = []
        history = []
        rng = random.Random(1)
        for _ in range(6):
            pick = next_pick(pool, history, rng)
            picks.append(pick)
            history = remember(history, pick)

        assert all(
            a != b for a, b in zip(picks, picks[1:])
        )  # com 2 faixas a janela de faixa é 1: nunca repete em seguida

    def test_the_history_keeps_only_fifty(self):
        history = []
        for number in range(80):
            history = remember(history, Track("a", str(number)))

        assert len(history) == 50 and history[0].title == "30"


class TestFailureAndPause:
    @pytest.mark.asyncio
    async def test_exactly_five_failures_in_a_row_stop_the_draw_for_the_rebuild(self):
        pool = [Track(f"A{i}", f"T{i}") for i in range(20)]
        session = CustomSession(pool, ResolvedTracks(FakeDb()))
        calls = []

        async def resolve(track):
            calls.append(track)
            return None

        with pytest.raises(StreakLimit):
            await session.next_playable(resolve)

        assert len(calls) == 5

    @pytest.mark.asyncio
    async def test_the_paused_station_retries_in_ten_minutes_and_a_normal_backoff_is_shorter(self):
        runner = CustomRadioRunner("1:2", FakePorts(), ProcessLimits(), publish=lambda state: None)
        runner.no_tracks = True
        assert runner._backoff() == 600.0

        runner.no_tracks = False
        assert runner._backoff() < 600.0

    @pytest.mark.asyncio
    async def test_a_failure_is_cached_for_a_day_and_not_a_minute_more(self):
        now = [1000.0]
        cache = ResolvedTracks(FakeDb(), clock=lambda: now[0])
        track = Track("A", "1")
        await cache.mark(track)

        now[0] += 86_400
        assert await cache.failed(track) is True
        now[0] += 1
        assert await cache.failed(track) is False
