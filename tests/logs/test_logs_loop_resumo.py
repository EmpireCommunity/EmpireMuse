"""Item 7 (P3, K11h) — o resumo COMPLETO do ``logs_loop`` com um pool falso: as fontes reais de cada campo.

``bot.latency`` (segundos) vira ms; o p95 de voz vem de ``player.ping`` ignorando ``None``; nó com ``stats`` mostra players/CPU/memória e nó sem
``stats`` mostra ``indisponível``; o ``process_stats`` é injetado. Contrato que este teste FIXA (a K11 só diz "devolve ``(cpu, rss)``"):
``process_stats(pid=None) -> (cpu_percent, rss_mib)``, sem argumento para o processo Python e com o ``pid`` do Java (``pool.lavalink_instance.pid``)
quando há Lavalink local. Relógio e ``sleep`` injetados; ``configure_logging`` REAL com o mesmo relógio.
"""

import asyncio
import io
import logging
from types import SimpleNamespace

import pytest

from empiremuse.logs.metrics import MetricsCollector
from empiremuse.logs.setup import configure_logging
from empiremuse.logs.settings import LogSettings
from empiremuse.utils.client import BotPool
from empiremuse.wavelink.stats import Stats
from tests.logs.apoio import Relogio

pytestmark = pytest.mark.critical

MIB = 1024 * 1024
PID_DO_JAVA = 4242
PYTHON = (12.5, 345.6)
JAVA = (80.0, 900.0)


class Parar(BaseException):
    pass


class Bot:
    """Hashable (``get_all_bots`` usa um ``set``)."""

    def __init__(self, latency, guilds, nos, jogadores) -> None:
        self.latency = latency
        self.guilds = list(range(guilds))
        self.bot_ready = True
        self.music = SimpleNamespace(nodes=nos, players=jogadores)


def stats_reais(no, *, players=12, tocando=7, carga=0.035, sistema=0.2, usada=220, alocada=512):
    return Stats(
        no,
        {
            "uptime": 1000,
            "players": players,
            "playingPlayers": tocando,
            "memory": {"free": 1, "used": usada * MIB, "allocated": alocada * MIB, "reservable": alocada * MIB},
            "cpu": {"cores": 4, "systemLoad": sistema, "lavalinkLoad": carga},
            "frameStats": {"sent": 1, "nulled": 0, "deficit": 0},
        },
    )


def no_com_stats(identificador="N-OK", **campos):
    no = SimpleNamespace(identifier=identificador, is_available=True, players={}, stats=None)
    no.stats = stats_reais(no, **campos)
    return no


def no_sem_stats(identificador="N-SEM"):
    return SimpleNamespace(identifier=identificador, is_available=True, players={}, stats=None)


def jogador(ping):
    return SimpleNamespace(ping=ping, current=SimpleNamespace(title="x"), is_paused=False)


class Cenario:
    def __init__(self, *, bots, lavalink_local: bool = False) -> None:
        self.relogio = Relogio()
        self.console = io.StringIO()
        configure_logging(
            LogSettings(logging.INFO, False, False, frozenset(), 300), [], stream=self.console, clock=self.relogio
        )
        self.pool = BotPool.__new__(BotPool)
        self.pool.config = {"METRICS_INTERVAL": 300}
        self.pool.bots = list(bots)
        self.pool.metrics = MetricsCollector()
        self.pool.logs_task = None
        self.pool.lavalink_instance = SimpleNamespace(pid=PID_DO_JAVA) if lavalink_local else None
        self.chamadas: list[tuple] = []

    def process_stats(self, *args):
        self.chamadas.append(args)
        alvo = args[0] if args else None
        return PYTHON if alvo is None else JAVA

    def rodar(self) -> list[str]:
        async def sleep(segundos):
            self.relogio.avancar(segundos)
            self.voltas += 1
            if self.voltas >= 11:
                raise Parar

        self.voltas = 0
        with pytest.raises(Parar):
            asyncio.run(self.pool.logs_loop(clock=self.relogio, sleep=sleep, process_stats=self.process_stats))
        return [linha for linha in self.console.getvalue().splitlines() if "📊" in linha]


def pings(valores, nulos=0):
    return {n: jogador(valor) for n, valor in enumerate([*valores, *([None] * nulos)])}


class TestPrimeiraLinha:
    def test_the_summary_line_has_the_documented_shape_with_active_players_and_peak(self):
        cenario = Cenario(bots=[Bot(0.05, 1, {}, pings([10.0, 20.0, 30.0]))])

        primeira = cenario.rodar()[0]

        assert primeira.count("📊") == 1
        assert "players ativos" in primeira and "(pico" in primeira and "faixas iniciadas" in primeira


class TestNos:
    def test_a_node_with_stats_shows_players_cpu_and_memory(self):
        no = no_com_stats("N-OK", players=12, tocando=7, carga=0.035, sistema=0.2, usada=220, alocada=512)
        cenario = Cenario(bots=[Bot(0.05, 1, {no.identifier: no}, {})])

        linhas = cenario.rodar()

        do_no = [x for x in linhas if "N-OK" in x]
        assert len(do_no) == 2  # um por resumo (t = 300 e 600)
        for linha in do_no:
            assert "nó N-OK" in linha and "indisponível" not in linha
            assert "players 12" in linha or "players 7" in linha
            assert "CPU 3.5%" in linha or "CPU 20%" in linha
            assert "memória 220" in linha or "memória 512" in linha

    def test_a_node_without_stats_is_unavailable(self):
        no = no_sem_stats("N-SEM")
        cenario = Cenario(bots=[Bot(0.05, 1, {no.identifier: no}, {})])

        linhas = cenario.rodar()

        do_no = [x for x in linhas if "N-SEM" in x]
        assert len(do_no) == 2  # um por resumo (t = 300 e 600)
        for linha in do_no:
            assert "indisponível" in linha and "players" not in linha


class TestLavalinkLocal:
    def test_with_a_local_lavalink_the_java_process_figures_are_read_by_its_pid_and_shown(self):
        no = no_sem_stats("LOCAL")
        cenario = Cenario(bots=[Bot(0.05, 1, {"LOCAL": no}, {})], lavalink_local=True)

        linhas = cenario.rodar()

        assert (PID_DO_JAVA,) in cenario.chamadas
        texto = "".join(linhas)
        assert "900" in texto and "80" in texto
        assert len(linhas) == 2 * 2  # a parte do Java entra na linha do nó, não numa linha a mais


class TestFalhaDeFonte:
    def test_a_process_stats_that_returns_none_shows_dashes_and_does_not_stop_the_summary(self):
        cenario = Cenario(bots=[Bot(0.05, 1, {}, {})])
        cenario.process_stats = lambda *args: (None, None)

        primeira = cenario.rodar()[0]

        assert "CPU —" in primeira and "RSS —" in primeira
