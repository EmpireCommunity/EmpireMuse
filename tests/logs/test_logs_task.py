"""Item 7 (P3, Gambit) — K9: a tarefa de manutenção do log do ``BotPool`` (``start_logs_task`` / ``logs_loop``).

``sleep`` e relógio injetados (como no ``heartbeat_loop``): nada dorme. O ``configure_logging`` é REAL com o mesmo relógio, então o ``tick_logging``
aparece pelo efeito (a linha ``↻ … repetida N×`` sai quando a janela fecha) e o resumo pelo texto ``📊`` no console.
"""

import asyncio
import io
import logging
import re
from types import SimpleNamespace

import pytest

from empiremuse.logs.metrics import MetricsCollector
from empiremuse.logs.setup import configure_logging
from empiremuse.logs.settings import LogSettings
from empiremuse.utils.client import BotPool
from tests.logs.apoio import Relogio

pytestmark = pytest.mark.critical

JANELA = 60.0


class Parar(BaseException):
    pass


class Cenario:
    def __init__(self, intervalo: int) -> None:
        self.relogio = Relogio()
        self.console = io.StringIO()
        configure_logging(
            LogSettings(logging.INFO, False, False, frozenset(), intervalo), [], stream=self.console, clock=self.relogio
        )
        self.pool = BotPool.__new__(BotPool)
        self.pool.config = {"METRICS_INTERVAL": intervalo}
        self.pool.bots = []
        self.pool.guild_bots = {}
        self.pool.metrics = MetricsCollector()
        self.pool.logs_task = None
        self.dormiu: list[float] = []

    def sleep_ate(self, vezes: int):
        async def sleep(segundos):
            self.dormiu.append(segundos)
            self.relogio.avancar(segundos)
            if len(self.dormiu) >= vezes:
                raise Parar

        return sleep

    def rodar(self, vezes: int) -> str:
        with pytest.raises(Parar):
            asyncio.run(self.pool.logs_loop(clock=self.relogio, sleep=self.sleep_ate(vezes)))
        return self.console.getvalue()


def resumos(saida: str) -> list[str]:
    """Linhas de resumo do console: o 📊 vem do formatter (categoria ``empiremuse.metrics``), então se conta pelo logger + 'servidores'."""
    return [linha for linha in saida.splitlines() if re.search(r"empiremuse\.metrics: (📊 )?servidores", linha)]


def repetir_aviso(vezes: int = 3) -> None:
    for n in range(vezes):
        logging.getLogger("empiremuse.wavelink").warning("nó %s caiu", n)


class TestTarefaComIntervaloZero:
    def test_it_sleeps_the_aggregation_window_each_turn(self):
        cenario = Cenario(0)

        cenario.rodar(4)

        assert cenario.dormiu == [JANELA] * 4

    def test_it_ticks_the_aggregator_so_a_closed_window_prints_the_repeat_count(self):
        cenario = Cenario(0)
        repetir_aviso(3)

        saida = cenario.rodar(3)

        assert saida.count("repetida 2×") == 1


class TestTarefaComResumo:
    def test_the_summary_comes_out_every_metrics_interval_and_not_before(self):
        assert "📊" not in Cenario(300).rodar(4)

    def test_the_summary_carries_the_counters_of_the_interval_and_resets_them(self):
        cenario = Cenario(300)
        for guild in range(37):
            cenario.pool.metrics.track_started(guild)

        saida = cenario.rodar(11)

        linhas = [linha for linha in saida.splitlines() if "📊" in linha]
        assert "faixas iniciadas 37" in linhas[0] and "faixas iniciadas 0" in linhas[1]


class TestRobustez:
    def test_an_exception_in_one_turn_is_one_warning_line_and_the_loop_goes_on(self):
        cenario = Cenario(0)
        chamadas = []

        def amostrar(ativos):
            chamadas.append(ativos)
            if len(chamadas) <= 2:
                raise RuntimeError("falhou a amostragem")

        cenario.pool.metrics = SimpleNamespace(sample_players=amostrar)

        saida = cenario.rodar(5)

        assert len(chamadas) in (4, 5) and cenario.dormiu == [JANELA] * 5
        avisos = [linha for linha in saida.splitlines() if "WARNING" in linha]
        assert avisos and all("RuntimeError" in linha for linha in avisos)

    def test_cancellation_is_not_swallowed(self):
        cenario = Cenario(0)

        async def principal():
            tarefa = asyncio.create_task(cenario.pool.logs_loop(clock=cenario.relogio, sleep=asyncio.sleep))
            await asyncio.sleep(0)
            tarefa.cancel()
            with pytest.raises(asyncio.CancelledError):
                await tarefa

        asyncio.run(principal())


class TestCicloDeVida:
    @pytest.fixture
    def laco(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.mark.parametrize("intervalo", [0, 300])
    def test_start_logs_task_always_creates_the_task_even_with_the_summary_off(self, laco, intervalo):
        pool = Cenario(intervalo).pool
        pool.loop = laco

        pool.start_logs_task()

        assert isinstance(pool.logs_task, asyncio.Task)
        pool.logs_task.cancel()
        laco.run_until_complete(asyncio.gather(pool.logs_task, return_exceptions=True))

    def test_shutdown_cancels_the_logs_task(self):
        cancelada = []
        pool = BotPool.__new__(BotPool)
        pool.bots, pool.guild_bots, pool.heartbeat_task, pool.nodes_expected = [], {}, None, False
        pool.update_watch_task, pool.helper_watch_tasks = None, set()
        pool.logs_task = SimpleNamespace(cancel=lambda: cancelada.append(True))
        pool.lavalink_instance = None
        pool.i18n = SimpleNamespace(close=lambda: None)

        asyncio.run(pool.shutdown(0))

        assert cancelada == [True]
