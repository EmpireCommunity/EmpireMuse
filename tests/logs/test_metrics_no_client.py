"""``_node_sample``: ``frameStats`` do ``stats`` do nó vira os contadores do ``NodeSample``; ausente (-1) vira ``None``."""

from types import SimpleNamespace

from empiremuse.utils.client import _node_sample, _with_java_figures


def _no(**quadros) -> SimpleNamespace:
    stats = SimpleNamespace(
        players=2,
        playing_players=1,
        lavalink_load=0.5,
        memory_used=104857600,
        has_frame_stats=bool(quadros),
        **{"frames_sent": -1, "frames_nulled": -1, "frames_deficit": -1, **quadros},
    )
    return SimpleNamespace(stats=stats, is_available=True)


class TestNodeSampleQuadros:
    def test_frame_stats_are_copied_into_the_sample(self):
        amostra = _node_sample("LOCAL", _no(frames_sent=2990, frames_nulled=4, frames_deficit=-10))

        assert (amostra.frames_sent, amostra.frames_nulled, amostra.frames_deficit) == (2990, 4, -10)
        assert amostra.players_playing == 1

    def test_absent_frame_stats_are_none_not_minus_one(self):
        amostra = _node_sample("LOCAL", _no())

        assert (amostra.frames_sent, amostra.frames_nulled, amostra.frames_deficit) == (None, None, None)

    def test_a_real_deficit_of_minus_one_is_kept(self):
        amostra = _node_sample("LOCAL", _no(frames_sent=3001, frames_nulled=0, frames_deficit=-1))

        assert amostra.frames_deficit == -1

    def test_java_figures_keep_the_frame_counters(self):
        amostra = _node_sample("LOCAL", _no(frames_sent=3000, frames_nulled=0, frames_deficit=0))

        (completa,) = _with_java_figures([amostra], 1.0, 200.0)

        assert (completa.frames_sent, completa.frames_nulled, completa.frames_deficit) == (3000, 0, 0)


class TestStatsDoNo:
    """``Stats`` (``wavelink/stats.py``) diz se o Lavalink mandou ``frameStats``: o ``-1`` sozinho não distingue ausente de déficit -1."""

    @staticmethod
    def _stats(**extra):
        from empiremuse.wavelink.stats import Stats

        data = {
            "uptime": 1,
            "players": 1,
            "playingPlayers": 1,
            "memory": {"free": 1, "used": 1, "allocated": 1, "reservable": 1},
            "cpu": {"cores": 1, "systemLoad": 0.1, "lavalinkLoad": 0.1},
            **extra,
        }
        return Stats(None, data)

    def test_without_frame_stats_they_are_absent(self):
        assert self._stats().has_frame_stats is False
        assert self._stats(frameStats=None).has_frame_stats is False

    def test_with_frame_stats_they_are_present_even_when_the_deficit_is_minus_one(self):
        stats = self._stats(frameStats={"sent": 3001, "nulled": 0, "deficit": -1})

        assert stats.has_frame_stats is True and stats.frames_deficit == -1
