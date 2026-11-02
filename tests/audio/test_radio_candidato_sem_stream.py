"""ERR-65FEA7 (b): o candidato que o nó não consegue tocar ("No playable stream") passa ao seguinte, até 3 por faixa (contrato R5 §4)."""

from types import SimpleNamespace

import pytest

from empiremuse.utils.music.models import autoqueue_steps as steps
from empiremuse.utils.music.models.step_state import ResolveTrackState

NO_STREAM = "No playable stream found for track: 1341254653"


def candidate(identifier: str):
    return SimpleNamespace(
        id=identifier,
        title="Сказка",
        duration=180_000,
        is_stream=False,
        info={"sourceName": "soundcloud", "uri": f"https://example.test/{identifier}"},
    )


def partial(rejected=()):
    return SimpleNamespace(
        id=None, title="Сказка", duration=180_000, info={"extra": {"rejected": list(rejected)}, "sourceName": "last.fm"}
    )


def pick(track, result):
    return steps._rt_pick_candidate(track, result, ResolveTrackState(check_duration=True))


def test_a_rejected_candidate_is_skipped_for_the_next_one():
    result = [candidate("a"), candidate("b")]

    assert pick(partial(rejected=["a"]), result) is result[1]


def test_without_rejections_the_first_candidate_still_wins():
    result = [candidate("a"), candidate("b")]

    assert pick(partial(), result) is result[0]


def test_no_stream_rejects_the_chosen_candidate_up_to_three_times():
    track = partial()
    outcomes = []
    for identifier in ("a", "b", "c", "d"):
        track.id = identifier
        outcomes.append(steps.reject_no_stream(track, NO_STREAM))

    assert outcomes == [True, True, True, False]
    assert track.info["extra"]["rejected"] == ["a", "b", "c"]
    assert track.id == "d"  # o quarto foi recusado: a faixa fica com ele e falha como antes


@pytest.mark.parametrize("message", ["Something else failed", "", None])
def test_any_other_error_is_not_a_reason_to_try_the_next_candidate(message):
    track = partial()
    track.id = "a"

    assert steps.reject_no_stream(track, message) is False
    assert track.id == "a"


class TestTheRadioPlayerSwapsTheCandidate:
    @staticmethod
    def make_player(resolved_id):
        from unittest.mock import AsyncMock

        current = partial()
        current.id = "a"

        async def resolve(track):
            track.id = resolved_id

        queue = SimpleNamespace(appendleft=lambda item: queued.append(item))
        queued = []
        player = SimpleNamespace(
            radio_custom=True,
            node=SimpleNamespace(identifier="N"),
            current=current,
            queue=queue,
            resolve_track=resolve,
            process_next=AsyncMock(),
        )
        return player, current, queued

    @staticmethod
    def make_event(message=NO_STREAM):
        import empiremuse.wavelink as wavelink

        event = wavelink.TrackException({"node": SimpleNamespace(identifier="N"), "exception": {"message": message}})
        return event

    @pytest.mark.asyncio
    async def test_a_no_stream_failure_requeues_the_track_with_the_next_candidate(self):
        from empiremuse.utils.music.models import event_steps

        player, current, queued = self.make_player("b")

        consumed = await event_steps._he_custom_radio_no_stream(player, self.make_event())

        assert consumed is True and queued == [current] and current.id == "b"
        player.process_next.assert_awaited_once()
        assert current.info["extra"]["rejected"] == ["a"]

    @pytest.mark.asyncio
    async def test_when_no_other_candidate_resolves_the_event_is_still_consumed_and_the_radio_draws_again(self):
        from empiremuse.utils.music.models import event_steps

        player, current, queued = self.make_player(None)

        consumed = await event_steps._he_custom_radio_no_stream(player, self.make_event())

        assert consumed is True and queued == []
        player.process_next.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_other_errors_and_other_players_keep_the_normal_path(self):
        from empiremuse.utils.music.models import event_steps

        player, _, _ = self.make_player("b")
        assert await event_steps._he_custom_radio_no_stream(player, self.make_event("boom")) is False
        player.radio_custom = False
        assert await event_steps._he_custom_radio_no_stream(player, self.make_event()) is False
