"""ERR-65FEA7 (b): busca comum troca o candidato sem stream e termina ao esgotar as opções."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.utils.music.models.player_autoqueue import PlayerAutoqueueMixin
from empiremuse.utils.music.models.tracks import PartialTrack

pytestmark = [pytest.mark.asyncio, pytest.mark.regression]


def candidate(identifier):
    return SimpleNamespace(
        id=identifier,
        title="Song",
        author="Artist",
        duration=180_000,
        thumb="",
        is_stream=False,
        info={"sourceName": "soundcloud", "uri": f"https://example.invalid/{identifier}"},
    )


def resolver(results):
    return SimpleNamespace(
        native_yt=False,
        node=SimpleNamespace(
            get_tracks=AsyncMock(return_value=results),
            info={"sourceManagers": []},
            partial_providers=["scsearch:{title} - {author}"],
        ),
        bot=SimpleNamespace(pool=SimpleNamespace(partial_track_cache={})),
    )


def partial():
    return PartialTrack(title="Song", author="Artist", duration=180_000, source_name="spotify")


@pytest.mark.parametrize("rejected", [["a"], ["a", "b"]])
async def test_resolution_never_falls_back_to_a_rejected_candidate(rejected):
    track = partial()
    track.info["extra"]["rejected"] = rejected
    player = resolver([candidate(identifier) for identifier in rejected])

    await PlayerAutoqueueMixin.resolve_track(player, track)

    assert track.id is None
    assert player.bot.pool.partial_track_cache == {}


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    from empiremuse.utils.music.models import event_steps

    monkeypatch.setattr(event_steps.asyncio, "sleep", AsyncMock())


def playback_world(results):
    from tests.audio.test_playback_retry_youtube import make_player

    track = partial()
    player = make_player(track)
    search = resolver(results)
    player.node.__dict__.update(search.node.__dict__)
    player.bot.pool = search.bot.pool

    async def resolve(track, force=False):
        await PlayerAutoqueueMixin.resolve_track(player, track, force=force)

    player.resolve_track = AsyncMock(side_effect=resolve)
    return player, track


@pytest.mark.parametrize("source", ["spotify", "last.fm"])
async def test_common_search_track_retries_next_candidate_before_dropping(source):
    from empiremuse.utils.music.models.event_steps import _he_dispatch
    from tests.audio.test_playback_retry_youtube import fail

    player, track = playback_world([candidate("a"), candidate("b")])
    track.info["sourceName"] = source
    player.node.info["sourceManagers"] = ["soundcloud"]
    player.node.only_use_native_search_providers = False
    await player.resolve_track(track)

    await _he_dispatch(player, fail(player, "soundy-plugin", "No playable stream found"))

    assert track.id == "b"
    assert track.info["extra"]["resolved_by_search"] is True
    assert player.node.get_tracks.call_args.args[0].startswith("scsearch:")
    assert list(player.queue) == [track]
    assert track not in player.failed_tracks
    assert player.locked is False
    player.process_next.assert_awaited_once()
    player.report_error.assert_not_awaited()


@pytest.mark.parametrize("count", [1, 2, 5])
async def test_no_stream_failures_stop_when_candidates_or_retry_budget_run_out(count):
    from empiremuse.utils.music.models.event_steps import _he_dispatch
    from tests.audio.test_playback_retry_youtube import fail

    player, track = playback_world([candidate(str(i)) for i in range(count)])
    await player.resolve_track(track)
    attempted = []
    for _ in range(6):
        attempted.append(track.id)
        await _he_dispatch(player, fail(player, "soundy-plugin", "No playable stream found"))
        if not player.queue:
            break
        player.current = player.queue.popleft()

    assert len(attempted) == min(count, 4)  # primeira escolha + até 3 trocas
    assert len(attempted) == len(set(attempted))
    assert track in player.failed_tracks
    assert player.locked is False
    assert not player.queue


@pytest.mark.parametrize("marker", [None, False, 1, "true"])
async def test_direct_or_unmarked_track_is_not_retried(marker):
    from empiremuse.utils.music.models.event_steps import _he_dispatch
    from tests.audio.test_playback_retry_youtube import fail

    player, track = playback_world([candidate("a"), candidate("b")])
    track.id = "direct-encoded"
    if marker is not None:
        track.info["extra"]["resolved_by_search"] = marker

    await _he_dispatch(player, fail(player, "soundy-plugin", "No playable stream found"))

    player.resolve_track.assert_not_awaited()
    assert not player.queue
    assert track in player.failed_tracks


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("source", ["spotify", "http", "soundcloud"])
async def test_selection_records_current_resolution_path_even_over_an_old_marker(source, cached):
    from empiremuse.utils.music.models.autoqueue_steps import _rt_cache_key

    track = partial()
    track.info["sourceName"] = source
    track.info["uri"] = "https://example.invalid/track"
    track.info["extra"]["resolved_by_search"] = source != "spotify"
    player = resolver([candidate("a")])
    player.node.info["sourceManagers"] = ["soundcloud"]
    player.node.only_use_native_search_providers = False
    if cached:
        player.bot.pool.partial_track_cache[_rt_cache_key(track)] = [candidate("a")]

    await PlayerAutoqueueMixin.resolve_track(player, track)

    assert track.id == "a"
    assert track.info["extra"]["resolved_by_search"] is (source == "spotify")
