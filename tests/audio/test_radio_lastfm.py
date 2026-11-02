"""As listas do Last.fm da rádio personalizada (item 8, R5): balde de 1 req/s com rajada 3, cache de 24 h com a velha servindo até 7 d, artista desconhecido
sem erro e nada de chave nem URL no log. Sem rede: o ``fetch`` é um dublê."""

import logging
from types import SimpleNamespace

import pytest

from empiremuse.rules.radio_shuffle import Track
from empiremuse.utils.radio.lastfm_feed import (
    ARTIST_LIMIT,
    BURST,
    FRESH_S,
    STALE_S,
    LastFmFeed,
    LastFmMissing,
    LastFmUnavailable,
)
from tests.persistence.test_radio_store import FakeDb

pytestmark = pytest.mark.critical


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


def tag_page(*pairs):
    return {"tracks": {"track": [{"name": t, "artist": {"name": a}} for a, t in pairs]}}


def artist_page(*pairs):
    return {"toptracks": {"track": [{"name": t, "artist": {"name": a}} for a, t in pairs]}}


def make(fetch, *, key="SECRETKEY"):
    clock = Clock()
    pool = SimpleNamespace(database=FakeDb(), last_fm=SimpleNamespace(api_key=key) if key else None)
    feed = LastFmFeed(pool, fetch=fetch, clock=clock, sleep=clock.sleep, monotonic=clock)
    return SimpleNamespace(feed=feed, clock=clock, pool=pool)


class Calls:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    async def __call__(self, params):
        self.calls.append(dict(params))
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, Exception):
            raise response
        return response


class TestTagAndArtist:
    @pytest.mark.asyncio
    async def test_a_tag_reads_two_pages_and_keeps_only_valid_pairs(self):
        calls = Calls(tag_page(("A", "1"), ("B", "2")), tag_page(("C", "3")))
        w = make(calls)

        tracks = await w.feed.tag_tracks("Rock")

        assert tracks == [Track("A", "1"), Track("B", "2"), Track("C", "3")]
        assert [c["page"] for c in calls.calls] == ["1", "2"] and calls.calls[0]["tag"] == "Rock"

    @pytest.mark.asyncio
    async def test_hostile_text_and_malformed_items_are_dropped(self):
        bad = {
            "tracks": {
                "track": [
                    {"name": "ok", "artist": {"name": "A‮B"}},
                    {"name": 5},
                    "x",
                    {"name": "t" * 101, "artist": {"name": "A"}},
                    {"name": "good", "artist": {"name": "Z"}},
                ]
            }
        }
        w = make(Calls(bad))

        assert await w.feed.tag_tracks("rock") == [Track("Z", "good")] * 2  # as duas páginas trazem a mesma

    @pytest.mark.asyncio
    async def test_an_unknown_artist_is_an_empty_list_marked_unknown_not_an_error(self):
        w = make(Calls({"error": 6, "message": "The artist you supplied could not be found"}))

        assert await w.feed.artist_tracks("Xyzzy") == ([], True)

    @pytest.mark.asyncio
    async def test_the_artist_request_asks_for_30_tracks_with_autocorrect(self):
        calls = Calls(artist_page(("Djavan", "Oceano")))
        w = make(calls)

        await w.feed.artist_tracks("djavan")

        assert calls.calls[0]["limit"] == str(ARTIST_LIMIT) and calls.calls[0]["autocorrect"] == "1"


class TestCache:
    @pytest.mark.asyncio
    async def test_a_fresh_list_is_not_fetched_again(self):
        calls = Calls(artist_page(("A", "1")))
        w = make(calls)
        await w.feed.artist_tracks("A")

        await w.feed.artist_tracks("a")  # a mesma chave normalizada

        assert len(calls.calls) == 1

    @pytest.mark.asyncio
    async def test_after_24_hours_it_is_fetched_again(self):
        calls = Calls(artist_page(("A", "1")), artist_page(("A", "2")))
        w = make(calls)
        await w.feed.artist_tracks("A")
        w.clock.now += FRESH_S + 1

        assert (await w.feed.artist_tracks("A"))[0] == [Track("A", "2")]

    @pytest.mark.asyncio
    async def test_when_lastfm_fails_the_old_list_serves_up_to_7_days_and_not_beyond(self):
        calls = Calls(artist_page(("A", "1")), LastFmUnavailable())
        w = make(calls)
        await w.feed.artist_tracks("A")
        w.clock.now += FRESH_S + 1

        assert (await w.feed.artist_tracks("A"))[0] == [Track("A", "1")]

        w.clock.now += STALE_S
        with pytest.raises(LastFmUnavailable):
            await w.feed.artist_tracks("A")


class TestRecipe:
    @pytest.mark.asyncio
    async def test_a_genre_unites_its_tags(self):
        calls = Calls(tag_page(("A", "1")))
        w = make(calls)

        result = await w.feed.recipe_tracks({"tags": ["pop", "indie pop"]})

        assert {c["tag"] for c in calls.calls} == {"pop", "indie pop"} and len(result.tracks) == 4

    @pytest.mark.asyncio
    async def test_one_failing_artist_does_not_drop_the_station_but_all_failing_does(self):
        class Flaky:
            def __init__(self):
                self.n = 0

            async def __call__(self, params):
                self.n += 1
                if params["artist"] == "Ruim":
                    raise LastFmUnavailable
                return artist_page(("Bom", "x"))

        w = make(Flaky())
        result = await w.feed.recipe_tracks({"artists": ["Bom", "Ruim"]})
        assert result.tracks == [Track("Bom", "x")]

        with pytest.raises(LastFmUnavailable):
            await make(Flaky()).feed.recipe_tracks({"artists": ["Ruim"]})

    @pytest.mark.asyncio
    async def test_unknown_artists_are_listed_in_the_result(self):
        w = make(Calls({"error": 6}))

        result = await w.feed.recipe_tracks({"artists": ["Xyzzy"]})

        assert result.unknown == ("Xyzzy",) and result.tracks == []

    @pytest.mark.asyncio
    async def test_without_a_key_nothing_is_fetched(self):
        calls = Calls(artist_page())
        w = make(calls, key="")

        with pytest.raises(LastFmMissing):
            await w.feed.recipe_tracks({"tags": ["rock"]})

        assert calls.calls == [] and w.feed.configured is False


class TestRate:
    @pytest.mark.asyncio
    async def test_fifty_cold_artists_never_exceed_one_request_per_second_after_the_burst(self):
        calls = Calls(artist_page(("A", "1")))
        w = make(calls)
        start = w.clock.now

        for number in range(50):
            await w.feed.artist_tracks(f"Artista {number}")

        elapsed = w.clock.now - start
        assert len(calls.calls) == 50 and elapsed >= 50 - BURST - 1

    @pytest.mark.asyncio
    async def test_a_response_with_an_error_code_is_unavailable_and_logs_no_key(self, caplog):
        w = make(Calls({"error": 29, "message": "Rate Limit Exceeded SECRETKEY"}))

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(LastFmUnavailable):
                await w.feed.tag_tracks("rock")

        assert "SECRETKEY" not in caplog.text
