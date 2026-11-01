"""Item 8 — alarme de CPU e de quadros de áudio: dispara depois de N resumos seguidos, tem histerese, cooldown e avisa a volta ao normal.

Relógio injetado (o ``now`` é argumento); o laço real roda com o ``sleep`` falso do ``Cenario`` do resumo.
"""

import pytest

from empiremuse.logs.alarm import (
    COOLDOWN_S,
    CPU_DEGRADED_PERCENT,
    CPU_RECOVERED_PERCENT,
    FRAMES_DEGRADED_RATIO,
    DegradationAlarm,
)
from empiremuse.logs.metrics import NodeSample
from tests.logs.test_logs_loop_resumo import Bot, Cenario, no_com_stats

pytestmark = pytest.mark.critical

RESUMO = 300.0
ALTA = CPU_DEGRADED_PERCENT + 5
ENTRE = (CPU_DEGRADED_PERCENT + CPU_RECOVERED_PERCENT) / 2
BAIXA = CPU_RECOVERED_PERCENT - 20


def no(identificador="N1", *, enviados=3000, anulados=0, deficit=0):
    return NodeSample(identificador, 5, 10.0, 200.0, enviados, anulados, deficit, 5)


def sem_quadros(identificador="N1", *, tocando=1):
    """Sessão (ou nó) no ar sem ``frameStats``: o Lavalink só deixa de mandar quando ninguém toca nela."""
    return NodeSample(identificador, 5, 10.0, 200.0, players_playing=tocando)


def sem_dados(identificador="N1"):
    return NodeSample(identificador, None, None, None)


class TestAlarmeDeCpu:
    def test_cpu_alerts_once_after_n_samples_stays_quiet_in_the_band_and_alerts_the_recovery_once(self):
        alarme = DegradationAlarm()
        saida = []
        # (cpu, o que deve sair): a 1ª amostra alta ainda não dispara; a 2ª dispara; mais altas e a faixa do meio não repetem; a volta
        # só sai depois de N amostras baixas seguidas.
        amostras = [ALTA, ALTA, ALTA, ENTRE, BAIXA, ENTRE, BAIXA, BAIXA, BAIXA]
        for passo, cpu in enumerate(amostras):
            saida.append(alarme.observe(passo * RESUMO, cpu_percent=cpu, nodes=[]))

        avisos = [(passo, linhas) for passo, linhas in enumerate(saida) if linhas]
        assert [passo for passo, _ in avisos] == [1, 7]
        assert "degradada" in avisos[0][1][0] and "normalizada" in avisos[1][1][0]
        assert all(len(linhas) == 1 for _, linhas in avisos)

    def test_spikes_split_by_a_normal_or_in_between_sample_never_alert(self):
        alarme = DegradationAlarm()

        saida = [
            alarme.observe(passo * RESUMO, cpu_percent=cpu, nodes=[])
            for passo, cpu in enumerate([BAIXA, ALTA, BAIXA, ALTA, ENTRE, ALTA, ENTRE, ALTA])
        ]

        assert saida == [[]] * 8

    def test_an_unknown_cpu_does_not_count_as_normal_or_as_high(self):
        alarme = DegradationAlarm()
        alarme.observe(0, cpu_percent=ALTA, nodes=[])

        assert alarme.observe(RESUMO, cpu_percent=None, nodes=[]) == []
        assert alarme.observe(2 * RESUMO, cpu_percent=ALTA, nodes=[]) != []  # a 1ª alta ainda valia

    def test_a_new_degradation_inside_the_cooldown_waits_until_it_ends(self):
        alarme = DegradationAlarm()
        passo = 0
        emitidos = []
        # degrada (t=300), normaliza e volta a degradar cedo: o novo aviso espera o cooldown acabar com o problema ainda ativo
        for cpu in [ALTA, ALTA, BAIXA, BAIXA, ALTA, ALTA, ALTA]:
            passo += 1
            emitidos.append((passo * RESUMO, alarme.observe(passo * RESUMO, cpu_percent=cpu, nodes=[])))

        textos = [(quando, linhas[0]) for quando, linhas in emitidos if linhas]
        assert [("degradada" in t) for _, t in textos] == [
            True,
            False,
        ]  # só o 1º degradado e a volta; o 2º ficou retido
        depois = alarme.observe(COOLDOWN_S + 10 * RESUMO, cpu_percent=ALTA, nodes=[])
        assert len(depois) == 1 and "degradada" in depois[0]


class TestAlarmeDeQuadros:
    def test_frames_alert_per_node_with_counts_only_and_recover(self):
        alarme = DegradationAlarm()
        ruim = no("N1", enviados=2700, anulados=200, deficit=100)  # 10% falharam

        primeiro = alarme.observe(0, cpu_percent=None, nodes=[ruim, no("N2")])
        segundo = alarme.observe(RESUMO, cpu_percent=None, nodes=[ruim, no("N2")])
        sem_repetir = alarme.observe(2 * RESUMO, cpu_percent=None, nodes=[ruim, no("N2")])
        volta1 = alarme.observe(3 * RESUMO, cpu_percent=None, nodes=[no("N1"), no("N2")])
        volta2 = alarme.observe(4 * RESUMO, cpu_percent=None, nodes=[no("N1"), no("N2")])

        assert primeiro == [] and sem_repetir == [] and volta1 == []
        assert len(segundo) == 1 and "nó N1" in segundo[0] and "anulados 200" in segundo[0] and "N2" not in segundo[0]
        assert 0.05 == FRAMES_DEGRADED_RATIO and "10.0%" in segundo[0]
        assert len(volta2) == 1 and "nó N1" in volta2[0] and "normalizados" in volta2[0]

    def test_a_small_sample_an_unavailable_node_and_a_negative_deficit_are_not_failures(self):
        alarme = DegradationAlarm()
        pequena = no("N1", enviados=100, anulados=90)  # poucos quadros esperados: não dá para julgar
        negativo = no("N3", enviados=3000, anulados=0, deficit=-500)
        for passo in range(4):
            linhas = alarme.observe(passo * RESUMO, cpu_percent=None, nodes=[pequena, sem_dados("N2"), negativo])
            assert linhas == []

    def test_node_without_frame_stats_is_healthy_and_a_node_name_with_control_chars_is_sanitized(self):
        alarme = DegradationAlarm()
        ruim = NodeSample("N\n1", 5, 1.0, 1.0, 1000, 3000, 0)
        alarme.observe(0, cpu_percent=None, nodes=[NodeSample("N0", 0, 1.0, 1.0, players_playing=0)])

        alarme.observe(RESUMO, cpu_percent=None, nodes=[ruim])
        linhas = alarme.observe(2 * RESUMO, cpu_percent=None, nodes=[ruim])

        assert len(linhas) == 1 and "\n" not in linhas[0]


def degradar(alarme, nos):
    alarme.observe(0, cpu_percent=None, nodes=nos)
    return alarme.observe(RESUMO, cpu_percent=None, nodes=nos)


class TestQuadrosAusentesNaoSaoRecuperacao:
    def test_a_node_without_data_while_players_are_playing_or_unavailable_never_recovers_the_alarm(self):
        alarme = DegradationAlarm()
        assert degradar(alarme, [no("N1", enviados=2700, anulados=200, deficit=100)])  # degradado
        # LOCAL indisponível: o Java preenche CPU e memória, mas players e quadros ficam sem dado
        indisponivel_com_java = NodeSample("N1", None, 50.0, 900.0)

        for passo in range(2, 8):
            amostra = indisponivel_com_java if passo % 2 else sem_quadros("N1", tocando=2)
            assert alarme.observe(passo * RESUMO, cpu_percent=None, nodes=[amostra]) == []

        saudavel = [no("N1")]
        alarme.observe(8 * RESUMO, cpu_percent=None, nodes=saudavel)
        assert "normalizados" in alarme.observe(9 * RESUMO, cpu_percent=None, nodes=saudavel)[0]

    def test_a_node_where_nobody_is_playing_has_nothing_to_measure_and_recovers(self):
        alarme = DegradationAlarm()
        assert degradar(alarme, [no("N1", enviados=2700, anulados=200, deficit=100)])

        parado = sem_quadros("N1", tocando=0)
        assert alarme.observe(2 * RESUMO, cpu_percent=None, nodes=[parado]) == []
        assert "normalizados" in alarme.observe(3 * RESUMO, cpu_percent=None, nodes=[parado])[0]


class TestArredondamentoDoLavalink:
    def test_averages_that_add_up_to_2999_frames_still_count(self):
        # o Lavalink divide os três campos por inteiro: 2800 + 150 + 49 = 2999 e 199 quadros falharam (6,6%)
        alarme = DegradationAlarm()

        linhas = degradar(alarme, [no("N1", enviados=2800, anulados=150, deficit=49)])

        assert len(linhas) == 1 and "6.6%" in linhas[0]

    def test_a_really_small_sample_is_still_ignored(self):
        alarme = DegradationAlarm()

        assert degradar(alarme, [no("N1", enviados=1500, anulados=1000, deficit=0)]) == []


class TestSessoesDoMesmoNo:
    def test_a_failing_session_alerts_whatever_the_order_of_the_idle_and_healthy_ones(self):
        ruim = no("N1", enviados=2700, anulados=200, deficit=100)
        for sessoes in ([sem_quadros("N1"), ruim], [ruim, sem_quadros("N1")], [no("N1"), ruim, no("N1")]):
            linhas = degradar(DegradationAlarm(), sessoes)

            assert len(linhas) == 1 and "anulados 200" in linhas[0]

    def test_idle_and_healthy_sessions_alone_do_not_alert(self):
        assert degradar(DegradationAlarm(), [sem_quadros("N1"), no("N1")]) == []


class TestNoLacoDoResumo:
    def test_the_real_loop_logs_the_alert_after_the_summary_and_only_once(self):
        bot = Bot(0.05, 1, {}, {})
        cenario = Cenario(bots=[bot])
        cenario.process_stats = lambda *args: (ALTA, 300.0)  # sempre alta: dispara no 2º resumo e não repete

        resumos = [linha for linha in cenario.rodar() if "servidores" in linha]
        alertas = [linha for linha in cenario.console.getvalue().splitlines() if "alerta:" in linha]

        assert len(resumos) == 2  # t=300 e t=600
        assert len(alertas) == 1 and "CPU do processo degradada" in alertas[0]
        assert " WARNING " in alertas[0]

    def test_the_loop_gives_the_alarm_every_bot_session_of_a_node_not_only_the_first(self):
        def bot_com(frames):
            no_do_bot = no_com_stats("N-OK")
            no_do_bot.stats.frames_sent, no_do_bot.stats.frames_nulled, no_do_bot.stats.frames_deficit = frames
            return Bot(0.05, 1, {"N-OK": no_do_bot}, {})

        ocioso = bot_com((3000, 0, 0))
        ruim = bot_com((2700, 200, 100))  # 10% falharam, na SEGUNDA sessão do mesmo nó
        cenario = Cenario(bots=[ocioso, ruim])
        cenario.pool.get_all_bots = lambda: [ocioso, ruim]  # ordem fixa (o real devolve um ``set``)

        cenario.rodar()
        alertas = [linha for linha in cenario.console.getvalue().splitlines() if "alerta:" in linha]

        assert len(alertas) == 1 and "nó N-OK" in alertas[0] and "anulados 200" in alertas[0]
