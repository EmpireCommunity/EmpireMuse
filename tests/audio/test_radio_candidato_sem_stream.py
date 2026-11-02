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
        from empiremuse.utils.radio.custom_source import CustomSession, ResolvedTracks
        from tests.persistence.test_radio_store import FakeDb

        current = partial()
        current.id = "a"
        radio_track = Track("Artista", "Faixa")
        current.info["extra"]["radio_track"] = ["Artista", "Faixa"]
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
        ports.current = Track(
            "Primeira", "Faixa antiga"
        )  # o current das portas ficou na primeira: a chave NUNCA vem dele
        cache = ResolvedTracks(db)
        ports.pool = SimpleNamespace(radio=SimpleNamespace(resolved=cache))
        ports.session = CustomSession([radio_track], cache)
        ports._load_failed_expected = False
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

        consumed = await event_steps._he_custom_radio_failure(w.player, self.make_event())

        assert consumed is True and w.player.queue == [w.current] and w.current.id == "b"
        w.player.process_next.assert_awaited_once()
        assert w.current.info["extra"]["rejected"] == ["a"]
        assert (await ResolvedTracks(w.db).get(w.track))["encoded"] == "b"  # a entrada de 7 dias morta foi trocada

    @pytest.mark.asyncio
    async def test_when_the_search_only_returns_the_refused_candidate_the_track_fails_and_the_stocked_one_plays(self):
        from empiremuse.utils.music.models import event_steps
        from empiremuse.utils.radio.custom_source import ResolvedTracks

        w = self.make_world(None, queue=["stocked"])

        consumed = await event_steps._he_custom_radio_failure(w.player, self.make_event())

        assert consumed is True
        w.player.process_next.assert_awaited_once()  # a faixa adiantada toca
        assert w.player.queue == ["stocked"]
        assert await ResolvedTracks(w.db).failed(w.track) is True  # a falha ficou marcada: sai do sorteio por 24 h

    @pytest.mark.asyncio
    async def test_with_nothing_stocked_the_radio_draws_again_at_once(self):
        from empiremuse.utils.music.models import event_steps

        w = self.make_world(None)

        await event_steps._he_custom_radio_failure(w.player, self.make_event())

        w.ports.play_next.assert_awaited_once()
        w.player.process_next.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_after_three_refusals_the_entry_is_forgotten_and_the_radio_still_skips_to_the_next(self):
        from empiremuse.utils.music.models import event_steps
        from empiremuse.utils.radio.custom_source import ResolvedTracks

        w = self.make_world("x")
        w.current.info["extra"]["rejected"] = ["1", "2", "3"]
        await ResolvedTracks(w.db).store(w.track, SimpleNamespace(id="a", info={"uri": "u", "sourceName": "s"}))

        consumed = await event_steps._he_custom_radio_failure(w.player, self.make_event())

        assert consumed is True  # nunca o caminho de espera do player de música
        assert await ResolvedTracks(w.db).get(w.track) is None
        assert await ResolvedTracks(w.db).failed(w.track) is True
        w.ports.play_next.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_any_other_exception_marks_the_track_and_plays_the_next_at_once(self):
        from empiremuse.utils.music.models import event_steps
        from empiremuse.utils.radio.custom_source import ResolvedTracks

        w = self.make_world("b", queue=["stocked"])

        consumed = await event_steps._he_custom_radio_failure(w.player, self.make_event("boom"))

        assert consumed is True and await ResolvedTracks(w.db).failed(w.track) is True
        w.player.process_next.assert_awaited_once()  # a adiantada toca na hora
        assert w.ports.session.streak == 1

    @pytest.mark.asyncio
    async def test_load_failed_after_an_exception_is_the_same_failure_not_a_second_skip(self):
        import empiremuse.wavelink as wavelink
        from empiremuse.utils.music.models import event_steps

        w = self.make_world("b", queue=["one", "two"])
        await event_steps._he_custom_radio_failure(w.player, self.make_event("boom"))
        echo = wavelink.TrackEnd({"node": SimpleNamespace(identifier="N"), "reason": "LOAD_FAILED"})

        assert await event_steps._he_custom_radio_failure(w.player, echo) is True

        assert w.player.process_next.await_count == 1 and w.ports.session.streak == 1

    @pytest.mark.asyncio
    async def test_a_load_failed_without_an_exception_and_a_stuck_track_also_skip(self):
        import empiremuse.wavelink as wavelink
        from empiremuse.utils.music.models import event_steps

        w = self.make_world("b", queue=["one", "two"])
        failed = wavelink.TrackEnd({"node": SimpleNamespace(identifier="N"), "reason": "LOAD_FAILED"})
        stuck = wavelink.TrackStuck({"node": SimpleNamespace(identifier="N")})

        assert await event_steps._he_custom_radio_failure(w.player, failed) is True
        assert await event_steps._he_custom_radio_failure(w.player, stuck) is True

        assert w.player.process_next.await_count == 2 and w.ports.session.streak == 2

    @pytest.mark.asyncio
    async def test_a_normal_end_and_other_players_keep_the_normal_path(self):
        import empiremuse.wavelink as wavelink
        from empiremuse.utils.music.models import event_steps

        w = self.make_world("b")
        finished = wavelink.TrackEnd({"node": SimpleNamespace(identifier="N"), "reason": "FINISHED"})
        assert await event_steps._he_custom_radio_failure(w.player, finished) is False
        w.player.radio_custom = False
        assert await event_steps._he_custom_radio_failure(w.player, self.make_event()) is False

    @pytest.mark.asyncio
    async def test_the_cache_entry_and_the_failure_belong_to_the_track_that_failed_not_the_first_one(self):
        from empiremuse.rules.radio_shuffle import Track
        from empiremuse.utils.music.models import event_steps
        from empiremuse.utils.radio.custom_source import ResolvedTracks

        w = self.make_world("b")
        first = Track("Primeira", "Faixa antiga")

        await event_steps._he_custom_radio_failure(w.player, self.make_event())

        assert (await ResolvedTracks(w.db).get(w.track))["encoded"] == "b"
        assert await ResolvedTracks(w.db).get(first) is None  # a primeira faixa não ganhou o encoded de outra

        w2 = self.make_world(None)
        await event_steps._he_custom_radio_failure(w2.player, self.make_event())
        assert await ResolvedTracks(w2.db).failed(w2.track) is True
        assert await ResolvedTracks(w2.db).failed(first) is False  # nem ficou 24 h fora do sorteio sem ter falhado

    @pytest.mark.asyncio
    async def test_the_panel_follows_the_track_that_is_playing_even_when_it_came_from_the_stock(self):
        w = self.make_world("b")
        w.ports._refresh_panel.reset_mock()

        w.ports.sync_current()

        assert w.ports.current == w.track  # o player toca a faixa etiquetada, o painel passa a mostrar ela
        w.ports._refresh_panel.assert_called_once()
