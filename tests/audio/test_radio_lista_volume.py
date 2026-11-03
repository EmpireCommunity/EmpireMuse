"""A lista de estações e o volume da rádio ao vivo (item 8): quem anda por ela (anterior e próxima), o volume que cabe e o que a fonte aceita."""

import pytest

from empiremuse.rules.errors import RuleError
from empiremuse.rules.radio import LIST_MAX, VOLUME_DEFAULT, check_source, clamp_volume, station_neighbor

pytestmark = pytest.mark.critical

A, B, C, D = (
    f"{n}" * 8 + "-" + f"{n}" * 4 + "-" + f"{n}" * 4 + "-" + f"{n}" * 4 + "-" + f"{n}" * 12 for n in (1, 2, 3, 4)
)


class TestNeighbor:
    def test_next_and_previous_wrap_around(self):
        stations = [A, B, C]

        assert station_neighbor(stations, A, 1) == B and station_neighbor(stations, C, 1) == A
        assert station_neighbor(stations, A, -1) == C and station_neighbor(stations, B, -1) == A

    def test_unavailable_stations_are_skipped_in_either_direction(self):
        stations = [A, B, C, D]

        assert station_neighbor(stations, A, 1, skip={B}) == C
        assert station_neighbor(stations, A, -1, skip={D}) == C
        assert station_neighbor(stations, A, 1, skip={B, C, D}) is None  # ninguém disponível: não há para onde ir

    def test_no_list_a_short_list_or_a_current_outside_it_has_nowhere_to_go(self):
        assert station_neighbor([], A, 1) is None
        assert station_neighbor([A], A, 1) is None
        assert station_neighbor([A, B], C, 1) is None
        assert station_neighbor([A, B], None, 1) is None

    def test_two_stations_swap(self):
        assert station_neighbor([A, B], A, 1) == B and station_neighbor([A, B], A, -1) == B


class TestVolume:
    @pytest.mark.parametrize(("raw", "expected"), [(5, 10), (10, 10), (55, 55), (150, 150), (400, 150), (99.9, 99)])
    def test_it_is_clamped_to_10_150(self, raw, expected):
        assert clamp_volume(raw) == expected

    @pytest.mark.parametrize("raw", [None, "50", True, []])
    def test_what_is_not_a_number_is_the_default(self, raw):
        assert clamp_volume(raw) == VOLUME_DEFAULT


class TestSource:
    BASE = {"station": A, "reserve": None}

    def test_the_default_shape_is_unchanged(self):
        assert check_source("live", self.BASE) == {"station": A, "reserve": None}

    def test_list_and_volume_are_kept_when_valid(self):
        source = check_source("live", {**self.BASE, "list": [A, B, C], "volume": 60})

        assert source == {"station": A, "reserve": None, "list": [A, B, C], "volume": 60}

    def test_the_default_volume_and_a_one_station_list_are_not_stored(self):
        source = check_source("live", {**self.BASE, "list": [A], "volume": 100})

        assert source == {"station": A, "reserve": None}

    @pytest.mark.parametrize(
        "extra",
        [
            {"list": [A, A]},
            {"list": ["lixo", B]},
            {"list": "abc"},
            {"list": [A] + [f"{n:08x}-0000-0000-0000-000000000000" for n in range(LIST_MAX)]},
            {"volume": 9},
            {"volume": 151},
            {"volume": True},
            {"volume": "60"},
            {"outra": 1},
        ],
    )
    def test_anything_else_is_a_source_error(self, extra):
        assert isinstance(check_source("live", {**self.BASE, **extra}), RuleError)

    def test_a_custom_radio_still_takes_only_a_playlist(self):
        assert isinstance(check_source("custom", {"playlist": "Rock", "volume": 50}), RuleError)
