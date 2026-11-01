"""``_node_sample``: ``frameStats`` do ``stats`` do nó vira os contadores do ``NodeSample``; ausente (-1) vira ``None``."""

from types import SimpleNamespace

from empiremuse.utils.client import _node_sample, _with_java_figures


def _no(**quadros) -> SimpleNamespace:
    stats = SimpleNamespace(
        players=2,
        lavalink_load=0.5,
        memory_used=104857600,
        **{"frames_sent": -1, "frames_nulled": -1, "frames_deficit": -1, **quadros},
    )
    return SimpleNamespace(stats=stats, is_available=True)


class TestNodeSampleQuadros:
    def test_frame_stats_are_copied_into_the_sample(self):
        amostra = _node_sample("LOCAL", _no(frames_sent=2990, frames_nulled=4, frames_deficit=-10))

        assert (amostra.frames_sent, amostra.frames_nulled, amostra.frames_deficit) == (2990, 4, -10)

    def test_absent_frame_stats_are_none_not_minus_one(self):
        amostra = _node_sample("LOCAL", _no())

        assert (amostra.frames_sent, amostra.frames_nulled, amostra.frames_deficit) == (None, None, None)

    def test_java_figures_keep_the_frame_counters(self):
        amostra = _node_sample("LOCAL", _no(frames_sent=3000, frames_nulled=0, frames_deficit=0))

        (completa,) = _with_java_figures([amostra], 1.0, 200.0)

        assert (completa.frames_sent, completa.frames_nulled, completa.frames_deficit) == (3000, 0, 0)
