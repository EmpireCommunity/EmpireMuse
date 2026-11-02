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
    def make_world(resolved_id, *, queue=()):
        """Um player de rádio personalizada com as portas reais da personalizada (``CustomRadioPorts``) e um banco falso."""
        from unittest.mock import AsyncMock, MagicMock

        from empiremuse.rules.radio_shuffle import Track
        from empiremuse.utils.radio.custom_ports import CustomRadioPorts
        from empiremuse.utils.radio.custom_source import ResolvedTracks
        from tests.persistence.test_radio_store import FakeDb

        current = partial()
        current.id = "a"
        radio_track = Track("Artista", "Faixa")
        db = FakeDb()

        async def resolve(track):
            track.id = resolved_id

        class Queue(list):
            def appendleft(self, item):
                self.insert(0, item)

        player = SimpleNamespace(
            radio_custom=True,
            radio_id="1:2",
            node=SimpleNamespace(identifier="N"),
            current=current,
            queue=Queue(queue),
            resolve_track=resolve,
            process_next=AsyncMock(),
        )
        ports = object.__new__(CustomRadioPorts)
        ports._player = lambda: player
        ports.current = radio_track
        ports.pool = SimpleNamespace(radio=SimpleNamespace(resolved=ResolvedTracks(db)))
        ports.play_next = AsyncMock()
        ports._refresh_panel = MagicMock()
        player.bot = SimpleNamespace(
            pool=SimpleNamespace(
                radio=SimpleNamespace(manager=SimpleNamespace(runner=lambda rid: SimpleNamespace(ports=ports)))
            )
        )
        return SimpleNamespace(player=player, current=current, ports=ports, db=db, track=radio_track)

    @staticmethod
    def make_event(message=NO_STREAM):
        import empiremuse.wavelink as wavelink

        return wavelink.TrackException({"node": SimpleNamespace(identifier="N"), "exception": {"message": message}})

    @pytest.mark.asyncio
    async def test_a_no_stream_failure_plays_the_next_candidate_and_replaces_the_dead_cache_entry(self):
        from empiremuse.utils.music.models import event_steps
        from empiremuse.utils.radio.custom_source import ResolvedTracks

        w = self.make_world("b")
        await ResolvedTracks(w.db).store(
            w.track, SimpleNamespace(id="a", info={"uri": "u", "sourceName": "soundcloud"})
        )

        consumed = await event_steps._he_custom_radio_no_stream(w.player, self.make_event())

        assert consumed is True and w.player.queue == [w.current] and w.current.id == "b"
        w.player.process_next.assert_awaited_once()
        assert w.current.info["extra"]["rejected"] == ["a"]
        assert (await ResolvedTracks(w.db).get(w.track))["encoded"] == "b"  # a entrada de 7 dias morta foi trocada

    @pytest.mark.asyncio
    async def test_when_the_search_only_returns_the_refused_candidate_the_track_fails_and_the_stocked_one_plays(self):
        from empiremuse.utils.music.models import event_steps
        from empiremuse.utils.radio.custom_source import ResolvedTracks

        w = self.make_world(None, queue=["stocked"])

        consumed = await event_steps._he_custom_radio_no_stream(w.player, self.make_event())

        assert consumed is True
        w.player.process_next.assert_awaited_once()  # a faixa adiantada toca
        assert w.player.queue == ["stocked"] and w.ports.current is None
        assert await ResolvedTracks(w.db).failed(w.track) is True  # a falha ficou marcada: sai do sorteio por 24 h

    @pytest.mark.asyncio
    async def test_with_nothing_stocked_the_radio_draws_again_at_once(self):
        from empiremuse.utils.music.models import event_steps

        w = self.make_world(None)

        await event_steps._he_custom_radio_no_stream(w.player, self.make_event())

        w.ports.play_next.assert_awaited_once()
        w.player.process_next.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_after_three_refusals_the_cached_entry_is_forgotten_and_the_normal_path_runs(self):
        from empiremuse.utils.music.models import event_steps
        from empiremuse.utils.radio.custom_source import ResolvedTracks

        w = self.make_world("x")
        w.current.info["extra"]["rejected"] = ["1", "2", "3"]
        await ResolvedTracks(w.db).store(w.track, SimpleNamespace(id="a", info={"uri": "u", "sourceName": "s"}))

        consumed = await event_steps._he_custom_radio_no_stream(w.player, self.make_event())

        assert consumed is False
        assert await ResolvedTracks(w.db).get(w.track) is None

    @pytest.mark.asyncio
    async def test_other_errors_and_other_players_keep_the_normal_path(self):
        from empiremuse.utils.music.models import event_steps

        w = self.make_world("b")
        assert await event_steps._he_custom_radio_no_stream(w.player, self.make_event("boom")) is False
        w.player.radio_custom = False
        assert await event_steps._he_custom_radio_no_stream(w.player, self.make_event()) is False
