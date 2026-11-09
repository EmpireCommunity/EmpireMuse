"""Paginação pura do select compartilhado da Jukebox (T-435)."""

import pytest

from empiremuse.rules.jukebox_select import plan

pytestmark = pytest.mark.critical


@pytest.mark.parametrize("total", [0, 1, 22, 23, 24, 25, 26, 45, 46, 47, 48, 100])
@pytest.mark.parametrize("fixed", [0, 2])
def test_pages_cover_every_playlist_once_with_room_for_controls(total, fixed):
    first = plan(total, fixed, 1)
    seen = []
    for number in range(1, first.pages + 1):
        page = plan(total, fixed, number)
        count = page.stop - page.start
        assert count + (fixed if number == 1 else 0) + bool(page.previous) + bool(page.next) <= 25
        seen.extend(range(page.start, page.stop))
        assert page.previous == (number - 1 if number > 1 else None)
        assert page.next == (number + 1 if number < page.pages else None)
    assert seen == list(range(total))
    if total == 100:
        assert first.pages == 5


def test_reserved_entries_leave_22_playlists_and_a_next_control_or_a_23rd_playlist():
    assert plan(24, 2, 1).stop == 22
    assert plan(23, 2, 1).stop == 23
    assert plan(100, 2, 2).stop - plan(100, 2, 2).start == 23


@pytest.mark.parametrize("page, expected", [(-1, 1), (0, 1), (999, 5)])
def test_out_of_range_pages_are_clamped(page, expected):
    assert plan(100, 0, page).page == expected
