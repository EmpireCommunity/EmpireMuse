"""Item 7 (P3, Gambit) — ``empiremuse.logs.metrics``: B13 (``MetricsCollector``) e ``format_summary`` (3.9 / 3.12).

Tudo em memória e puro: nenhum relógio real (``snapshot_and_reset`` recebe o ``now``), nenhum I/O.
"""

import dataclasses
import re
import threading

import pytest

from empiremuse.logs.metrics import ERROR_KINDS, MetricsCollector, MetricsSnapshot, NodeSample, format_summary

pytestmark = pytest.mark.critical


class TestColetor:
    def test_track_starts_are_counted(self):
        coletor = MetricsCollector()
        for guild in (1, 2, 3, 1):
            coletor.track_started(guild)

        assert coletor.snapshot_and_reset(100.0).tracks_started == 4

    def test_errors_are_counted_by_kind(self):
        coletor = MetricsCollector()
        for tipo in ("track_exception", "track_exception", "track_stuck", "node_unavailable", "command_error"):
            coletor.error(tipo)

        assert dict(coletor.snapshot_and_reset(100.0).errors) == {
            "track_exception": 2,
            "track_stuck": 1,
            "node_unavailable": 1,
            "command_error": 1,
        }

    def test_a_kind_outside_the_list_becomes_other_and_free_text_never_becomes_a_key(self):
        coletor = MetricsCollector()
        coletor.error("tipo_inventado")
        coletor.error("Traceback (most recent call last)\nValueError: segredo-no-erro-1")
        coletor.error("track_stuck")

        erros = dict(coletor.snapshot_and_reset(100.0).errors)

        assert erros == {"other": 2, "track_stuck": 1}

    def test_snapshot_and_reset_clears_everything_and_the_interval_is_measured_from_the_previous_snapshot(self):
        coletor = MetricsCollector()
        coletor.track_started(1)
        coletor.sample_players(9)
        coletor.error("track_stuck")
        primeiro = coletor.snapshot_and_reset(100.0)

        segundo = coletor.snapshot_and_reset(160.0)

        assert (primeiro.tracks_started, primeiro.players_peak, dict(primeiro.errors)) == (1, 9, {"track_stuck": 1})
        assert (segundo.tracks_started, segundo.players_peak, dict(segundo.errors)) == (0, 0, {})
        assert segundo.interval == 60.0

    def test_concurrent_increments_lose_nothing(self):
        coletor = MetricsCollector()

        def contar():
            for n in range(1000):
                coletor.track_started(n % 7)
                coletor.error("track_stuck")

        fios = [threading.Thread(target=contar) for _ in range(4)]
        for fio in fios:
            fio.start()
        for fio in fios:
            fio.join(timeout=10)
        foto = coletor.snapshot_and_reset(1.0)

        assert foto.tracks_started == 4000 and dict(foto.errors) == {"track_stuck": 4000}


# ============================================================================ format_summary


def foto(**campos) -> MetricsSnapshot:
    coletor = MetricsCollector()
    for _ in range(campos.pop("iniciadas", 0)):
        coletor.track_started(1)
    coletor.sample_players(campos.pop("pico", 0))
    for tipo, vezes in campos.pop("erros", {}).items():
        for _ in range(vezes):
            coletor.error(tipo)
    return coletor.snapshot_and_reset(300.0)


def resumo(snapshot=None, **sobrescreve):
    base = {
        "guilds": 500,
        "players_active": 480,
        "gateway_ms": [50.0],
        "voice_ms": [float(n) for n in range(1, 101)],
        "cpu_percent": 12.5,
        "rss_mib": 345.6,
        "nodes": [],
    }
    return format_summary(
        snapshot or foto(iniciadas=37, pico=490, erros={"track_exception": 2}), **{**base, **sobrescreve}
    )


class TestResumo:
    def test_without_nodes_it_is_one_line_with_the_documented_fields(self):
        (linha,) = resumo()

        assert linha.startswith("📊") and "\n" not in linha
        assert re.search(r"servidores 500", linha) and re.search(r"players ativos 480 \(pico 490\)", linha)
        assert re.search(r"faixas iniciadas 37", linha)
        assert re.search(r"erros.*track_exception\W+2", linha)
        assert re.search(r"gateway 50\s?ms \(máx 50\s?ms\)", linha)
        assert re.search(r"voz p95 9[5-6]\s?ms", linha)
        assert re.search(r"processo CPU 1[23](\.\d)?%", linha) and re.search(r"RSS 34[56](\.\d)?\s?MiB", linha)

    @pytest.mark.parametrize("quantos", [0, 1, 2, 5])
    def test_with_n_nodes_it_is_exactly_one_plus_n_lines(self, quantos):
        nos = [NodeSample(f"NO{n}", 3, 1.5, 200.0) for n in range(quantos)]

        linhas = resumo(nodes=nos)

        assert len(linhas) == 1 + quantos and all(linha.startswith("📊") for linha in linhas)

    def test_a_node_identifier_with_control_characters_never_adds_a_line(self):
        linhas = resumo(nodes=[NodeSample("N\nERROR falso", 1, 1.0, 1.0)])

        assert len(linhas) == 2 and "\n" not in linhas[1]


class TestQuadrosDoNo:
    """``frameStats`` do Lavalink (``sent``/``nulled``/``deficit`` do último minuto) na linha do nó."""

    def test_the_node_line_shows_the_three_frame_counters(self):
        no = NodeSample("LOCAL", 1, 2.0, 100.0, frames_sent=2990, frames_nulled=4, frames_deficit=10)

        linha = resumo(nodes=[no])[1]

        assert re.search(r"quadros enviados 2990", linha)
        assert re.search(r"anulados 4", linha) and re.search(r"déficit 10", linha)

    def test_a_negative_deficit_is_kept_as_is(self):
        no = NodeSample("LOCAL", 1, 2.0, 100.0, frames_sent=3010, frames_nulled=0, frames_deficit=-10)

        assert re.search(r"déficit -10", resumo(nodes=[no])[1])

    def test_missing_counters_become_the_dash(self):
        linha = resumo(nodes=[NodeSample("LOCAL", 1, 2.0, 100.0)])[1]

        assert re.search(r"quadros enviados —", linha)
        assert re.search(r"anulados —", linha) and re.search(r"déficit —", linha)

    def test_an_unavailable_node_still_says_unavailable(self):
        assert resumo(nodes=[NodeSample("LOCAL", None, None, None)])[1].endswith("indisponível")


class TestRadios:
    """Item 8: a linha principal ganha as rádios do processo, só em contagens."""

    def test_the_main_line_shows_the_counts(self):
        (linha,) = resumo(radios=(5, 3, 2))

        assert linha.endswith("rádios: 5 ligadas (3 tocando, 2 pausadas)")

    def test_without_the_figure_the_line_is_the_same_as_before(self):
        (sem,) = resumo()

        assert "rádios" not in sem
