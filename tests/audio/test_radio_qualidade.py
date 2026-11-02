"""R5: o pontinho de qualidade de cada estação nos seletores (limites nomeados em ``rules/radio_quality.py``)."""

from types import SimpleNamespace

import pytest

from empiremuse.rules.radio_quality import (
    BITRATE_GOOD_KBPS,
    BITRATE_OK_KBPS,
    FAILING,
    GOOD,
    OK,
    WEAK,
    station_quality,
)
from empiremuse.rules.radio_stations import Station
from empiremuse.utils.radio.directory import _check_ok
from empiremuse.utils.radio.picker import station_option


@pytest.mark.parametrize(
    ("bitrate", "check_ok", "unavailable", "expected"),
    [
        (BITRATE_GOOD_KBPS, True, False, GOOD),
        (320, True, False, GOOD),
        (BITRATE_GOOD_KBPS, None, False, OK),  # boa, mas o diretório não confirmou
        (BITRATE_GOOD_KBPS - 1, True, False, OK),
        (BITRATE_OK_KBPS, True, False, OK),
        (BITRATE_OK_KBPS - 1, True, False, WEAK),
        (0, True, False, WEAK),  # taxa desconhecida
        (320, False, False, FAILING),  # a checagem do diretório falhou
        (320, True, True, FAILING),  # a nossa saúde a marcou
    ],
)
def test_the_quality_follows_the_named_thresholds(bitrate, check_ok, unavailable, expected):
    assert station_quality(bitrate, check_ok=check_ok, unavailable=unavailable) == expected


@pytest.mark.parametrize(
    ("raw", "expected"), [(1, True), (True, True), (0, False), (False, False), (None, None), ("x", None)]
)
def test_the_directory_flag_is_read_strictly(raw, expected):
    assert _check_ok(raw) is expected


def test_the_select_option_shows_the_dot_and_says_when_our_health_marked_it():
    hub = SimpleNamespace(health=SimpleNamespace(unavailable=lambda uuid: uuid == "bad"))
    tr = lambda key, **kw: key  # noqa: E731
    good = station_option(hub, tr, Station("ok", "Rock FM", "http://x", "MP3", 128, "Brasil"), True)
    bad = station_option(hub, tr, Station("bad", "Rock FM", "http://x", "MP3", 128, "Brasil"), True)

    assert good.label.startswith("🟢 ") and "radio.live.unavailable" not in good.description
    assert bad.label.startswith("🔴 ") and "radio.live.unavailable" in bad.description
