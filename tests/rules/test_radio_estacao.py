import json
import random
from pathlib import Path

import pytest

from empiremuse.rules.errors import RuleError
from empiremuse.rules.radio_genres import check_catalog, find_genre, genre_groups, recipe
from empiremuse.rules.radio_shuffle import Track, build_pool, next_pick, remember
from empiremuse.rules.radio_station import (
    MAX_STATIONS_PER_GUILD,
    STATION_GENRE_UNKNOWN,
    STATION_LIMIT,
    STATION_NAME_INVALID,
    STATION_NAME_TAKEN,
    STATION_PLAYLIST_MISSING,
    STATION_SOURCE_INVALID,
    StationDoc,
    check_station_doc,
    from_document,
    to_document,
)

CATALOG_FILE = Path(__file__).parents[2] / "empiremuse" / "utils" / "radio" / "data" / "genres.json"
STATION_ID = "0123456789abcdef01234567"


@pytest.fixture(scope="module")
def catalog():
    checked = check_catalog(json.loads(CATALOG_FILE.read_text(encoding="utf-8")))
    assert not isinstance(checked, RuleError)
    return checked


def doc(kind, source, **extra):
    return {"_id": STATION_ID, "guild_id": "5", "name": "Minha", "kind": kind, "source": source, **extra}


class TestCatalog:
    def test_the_shipped_catalog_has_33_genres_in_two_selectable_groups(self, catalog):
        groups = dict(genre_groups(catalog))

        assert len(catalog) == 33
        assert (len(groups["international"]), len(groups["brazilian"])) == (21, 12)

    def test_every_genre_has_exactly_one_recipe(self, catalog):
        assert all(bool(g.tags) != bool(g.artists) for g in catalog)
        assert recipe(find_genre(catalog, "rock")) == {"tags": ["rock"]}
        assert find_genre(catalog, "forro").artists[0] == "Luiz Gonzaga"

    def test_an_unknown_genre_id_is_not_found(self, catalog):
        assert find_genre(catalog, "free-text") is None
        assert find_genre(catalog, None) is None

    def test_a_genre_with_two_recipes_is_refused(self):
        raw = {
            "version": 1,
            "groups": ["international", "brazilian"],
            "genres": [{"id": "x1", "group": "international", "tags": ["a"], "artists": [f"A{i}" for i in range(12)]}],
        }

        assert isinstance(check_catalog(raw), RuleError)

    def test_a_brazilian_recipe_with_too_few_artists_is_refused(self):
        raw = {
            "version": 1,
            "groups": ["international", "brazilian"],
            "genres": [
                {"id": "x1", "group": "international", "tags": ["a"]},
                {"id": "x2", "group": "brazilian", "artists": ["só um"]},
            ],
        }

        assert check_catalog(raw).key == "x2"


class TestStationDoc:
    def check(self, kind, source, **kw):
        name = kw.pop("name", "Minha")
        return check_station_doc({"name": name, "kind": kind, "source": source}, **kw)

    def test_a_genre_station_is_accepted_and_the_name_is_normalized(self):
        station = self.check("genre", {"genre": "rock"}, name="  Rock   Pesado ")

        assert isinstance(station, StationDoc)
        assert (station.name, station.name_norm, station.source) == ("Rock Pesado", "rock pesado", {"genre": "rock"})

    def test_two_sources_at_once_are_refused(self):
        error = self.check("genre", {"genre": "rock", "artists": ["A"]})

        assert (error.code, error.key) == (STATION_SOURCE_INVALID, "kind")

    def test_an_unknown_genre_is_refused(self):
        assert self.check("genre", {"genre": "nope"}).code == STATION_GENRE_UNKNOWN

    def test_eleven_artists_are_refused_and_ten_pass(self):
        many = [f"Artista {i}" for i in range(11)]

        assert self.check("artists", {"artists": many}).code == STATION_SOURCE_INVALID
        assert not isinstance(self.check("artists", {"artists": many[:10]}), RuleError)

    def test_a_repeated_artist_is_dropped_ignoring_case(self):
        station = self.check("artists", {"artists": ["Djavan", "djavan", "Gal Costa"]})

        assert station.source == {"artists": ["Djavan", "Gal Costa"]}

    def test_a_playlist_that_does_not_exist_is_refused_and_named(self):
        error = self.check("playlists", {"playlists": ["Rock"]}, playlists={"Pop"})

        assert (error.code, error.key, error.value) == (STATION_PLAYLIST_MISSING, "playlists", "Rock")

    def test_a_name_already_used_in_the_server_is_refused_ignoring_case(self):
        assert self.check("genre", {"genre": "rock"}, name="MINHA", taken={"minha"}).code == STATION_NAME_TAKEN

    def test_the_eleventh_station_is_refused(self):
        error = self.check("genre", {"genre": "rock"}, count=MAX_STATIONS_PER_GUILD)

        assert (error.code, error.key) == (STATION_LIMIT, "limit")
        assert not isinstance(self.check("genre", {"genre": "rock"}, count=MAX_STATIONS_PER_GUILD - 1), RuleError)

    @pytest.mark.parametrize("name", ["", "   ", "x" * 33, "a\u202eb", "a\nb", 5, None])
    def test_a_bad_name_is_refused(self, name):
        assert self.check("genre", {"genre": "rock"}, name=name).code == STATION_NAME_INVALID

    def test_a_stored_document_round_trips_and_a_bad_one_is_refused(self):
        stored = {
            "_id": STATION_ID,
            "guild_id": "5",
            "name": "Minha",
            "kind": "artists",
            "source": {"artists": ["A"]},
            "rev": 2,
            "updated_by": "7",
            "updated_at": 9.5,
            "unknown": ["A"],
        }

        station = from_document(stored)

        assert (station.id, station.guild_id, station.rev, station.unknown) == (STATION_ID, 5, 2, ("A",))
        assert to_document(station)["name_norm"] == "minha"
        assert isinstance(from_document({**stored, "_id": "xx"}), RuleError)
        assert isinstance(from_document({**stored, "source": {"genre": "rock"}}), RuleError)


class TestShuffle:
    def pool(self, artists, per):
        return [Track(f"Artista {a}", f"Faixa {a}-{t}") for a in range(artists) for t in range(per)]

    def test_a_thousand_picks_never_break_the_windows(self):
        rng, pool, history = random.Random(7), self.pool(8, 12), []

        for _ in range(1000):
            pick = next_pick(pool, history, rng)
            assert pick not in history[-min(50, len(pool) // 2) :]
            assert pick.artist not in [t.artist for t in history[-4:]]
            history = remember(history, pick)

    def test_a_pool_of_three_tracks_from_one_artist_never_stalls(self):
        rng, pool, history = random.Random(1), [Track("A", "1"), Track("A", "2"), Track("A", "3")], []

        for _ in range(50):
            pick = next_pick(pool, history, rng)
            assert pick in pool
            history = remember(history, pick)

    def test_an_empty_pool_gives_nothing(self):
        assert next_pick([], [], random.Random(0)) is None

    def test_the_pool_is_deduplicated_and_capped_per_artist_and_overall(self):
        rng = random.Random(3)
        tracks = [Track("Mesmo", f"t{i}") for i in range(100)] + [Track("MESMO", "T1"), Track("", "x"), Track("A", " ")]

        assert len(build_pool(tracks, rng)) == 30
        many = [Track(f"A{a}", f"t{t}") for a in range(40) for t in range(30)]
        assert len(build_pool(many, rng)) == 300
