"""A fonte da rádio personalizada (item 8, R5): o pool por tipo de estação, o cache negativo de 24 h e as cinco falhas seguidas que pausam."""

import random
from types import SimpleNamespace

import pytest

from empiremuse.rules.radio_shuffle import Track
from empiremuse.rules.radio_station import CustomStation
from empiremuse.utils.radio.custom_source import (
    FAIL_STREAK_MAX,
    FAIL_TTL_S,
    CustomSession,
    EmptyStation,
    ResolvedTracks,
    NoPlayableTracks,
    load_pool,
)
from empiremuse.utils.radio.lastfm_feed import FeedResult
from tests.persistence.test_radio_store import FakeDb

pytestmark = pytest.mark.critical

STATION_ID = "0123456789abcdef01234567"


def station(kind, source):
    return CustomStation(id=STATION_ID, guild_id=5, name="Minha", kind=kind, source=source)


class FakeFeed:
    def __init__(self, tracks=(), unknown=()):
        self.tracks, self.unknown, self.recipes = list(tracks), tuple(unknown), []

    async def recipe_tracks(self, recipe):
        self.recipes.append(recipe)
        return FeedResult(list(self.tracks), self.unknown)


def hub(feed):
    return SimpleNamespace(lastfm=feed)


async def no_playlists(names):
    raise AssertionError("não era para ler playlist")


class TestLoadPool:
    @pytest.mark.asyncio
    async def test_a_genre_station_asks_lastfm_for_the_catalog_recipe(self):
        feed = FakeFeed([Track("A", "1"), Track("B", "2")])

        loaded = await load_pool(hub(feed), station("genre", {"genre": "rock"}), no_playlists)

        assert feed.recipes == [{"tags": ["rock"]}] and len(loaded.pool) == 2

    @pytest.mark.asyncio
    async def test_an_artists_station_reports_the_unknown_ones(self):
        feed = FakeFeed([Track("A", "1")], unknown=["Xyzzy"])

        loaded = await load_pool(hub(feed), station("artists", {"artists": ["A", "Xyzzy"]}), no_playlists)

        assert feed.recipes == [{"artists": ["A", "Xyzzy"]}] and loaded.unknown == ("Xyzzy",)

    @pytest.mark.asyncio
    async def test_a_playlist_station_reads_only_its_playlists(self):
        seen = []

        async def playlists(names):
            seen.append(list(names))
            return [Track("A", "1"), Track("A", "1"), Track("B", "2")]

        loaded = await load_pool(hub(FakeFeed()), station("playlists", {"playlists": ["Rock", "Pop"]}), playlists)

        assert seen == [["Rock", "Pop"]] and len(loaded.pool) == 2  # a repetida some

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kind, source",
        [("genre", {"genre": "nope"}), ("artists", {"artists": ["A"]}), ("playlists", {"playlists": ["P"]})],
    )
    async def test_a_station_without_tracks_is_empty(self, kind, source):
        async def empty(names):
            return []

        with pytest.raises(EmptyStation):
            await load_pool(hub(FakeFeed()), station(kind, source), empty)


class TestSession:
    def pool(self):
        return [Track(f"Artista {a}", f"Faixa {a}-{t}") for a in range(6) for t in range(5)]

    @pytest.mark.asyncio
    async def test_it_returns_a_playable_track_and_remembers_it(self):
        session = CustomSession(self.pool(), ResolvedTracks(FakeDb()), random.Random(1))

        async def resolve(track):
            return f"encoded:{track.title}"

        track, resolved = await session.next_playable(resolve)

        assert resolved == f"encoded:{track.title}" and session.history == [track]

    @pytest.mark.asyncio
    async def test_unresolvable_tracks_are_skipped_marked_and_not_picked_again(self):
        failed = ResolvedTracks(FakeDb())
        session = CustomSession(self.pool(), failed, random.Random(2))
        tried = []

        async def resolve(track):
            tried.append(track)
            return None if len(tried) < 3 else "ok"

        track, _ = await session.next_playable(resolve)

        assert len(tried) == 3 and track == tried[-1]
        assert all([await failed.failed(t) for t in tried[:2]]) and not await failed.failed(tried[2])
        assert tried[0] not in session.pool

    @pytest.mark.asyncio
    async def test_five_failures_in_a_row_pause_and_the_streak_resets(self):
        session = CustomSession(self.pool(), ResolvedTracks(FakeDb()), random.Random(3))
        calls = []

        async def resolve(track):
            calls.append(track)
            return None

        with pytest.raises(NoPlayableTracks):
            await session.next_playable(resolve)

        assert len(calls) == FAIL_STREAK_MAX and session.streak == 0

    @pytest.mark.asyncio
    async def test_a_failed_track_from_another_server_is_also_skipped_through_the_database(self):
        db = FakeDb()
        await ResolvedTracks(db).mark(Track("Artista 0", "Faixa 0-0"))
        session = CustomSession(
            [Track("Artista 0", "Faixa 0-0"), Track("B", "2")], ResolvedTracks(db), random.Random(4)
        )
        seen = []

        async def resolve(track):
            seen.append(track)
            return "ok"

        for _ in range(5):
            await session.next_playable(resolve)

        assert Track("Artista 0", "Faixa 0-0") not in seen


class TestResolvedTracks:
    @pytest.mark.asyncio
    async def test_a_failure_expires_after_24_hours(self):
        now = [1000.0]
        failed = ResolvedTracks(FakeDb(), clock=lambda: now[0])
        track = Track("A", "1")
        await failed.mark(track)
        assert await failed.failed(track) is True

        now[0] += FAIL_TTL_S + 1

        assert await failed.failed(track) is False

    @pytest.mark.asyncio
    async def test_the_key_ignores_case_and_spacing_and_a_broken_database_means_not_failed(self):
        db = FakeDb()
        await ResolvedTracks(db).mark(Track("Djavan", "Oceano"))
        assert await ResolvedTracks(db).failed(Track("  DJAVAN", "oceano ")) is True

        db.fail = True
        assert await ResolvedTracks(db).failed(Track("Outro", "x")) is False
