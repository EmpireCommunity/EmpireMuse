"""Item 7 (P3, gate K15) — (1) todo produtor de ``safe_error_code()`` passa ``extra={"error_code": code}``; (2) o ``logs_loop`` monta as amostras NA THREAD DO LOOP.

(1) Vale para ``_he_log_exception`` (``event_steps``, três caminhos: WARNING, ERROR com ``severity=fault`` e DEBUG de espelhamento) e para
``log_lavalink_failure`` (``helpers``, usado pelo ``autoqueue_steps``): o registro carrega ``error_code`` igual ao código devolvido quando ele não é
``""`` e NÃO tem o atributo quando é ``""``. Ponta a ponta com ``configure_logging`` real: 5 falhas iguais em 60 s não perdem nenhum código.
(2) Só o ``process_stats`` (psutil) pode rodar em outra thread; iterar ``bot.music.players``/``nodes``/``guilds`` fora do loop quebra com
"dictionary changed size during iteration". Relógio e ``sleep`` injetados.
"""

import io
import logging
import threading
from types import SimpleNamespace

import pytest

from empiremuse.logs.setup import configure_logging, flush_logging
from empiremuse.logs.settings import LogSettings
from empiremuse.rules.error_code import ERROR_CODE_RE
from empiremuse.utils.music.models import event_steps, helpers
from empiremuse.utils.music.models.event_steps import MIRROR_NOT_FOUND_CAUSES, _he_log_exception
from empiremuse.utils.music.models.helpers import log_lavalink_failure
from tests.logs.apoio import arquivo_do_log, Relogio
from tests.logs.test_logs_loop_resumo import PYTHON, Bot, Cenario, no_com_stats, pings
from tests.logs.test_player_events import excecao, jogador

pytestmark = pytest.mark.critical


class Cru(logging.Handler):
    """Registros crus do root (sem filtros), com o ``error_code`` fotografado no momento da emissão."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.registros: list[logging.LogRecord] = []

    def emit(self, record):
        self.registros.append(record)


@pytest.fixture
def cru():
    raiz = logging.getLogger()
    handler, nivel = Cru(), raiz.level
    raiz.addHandler(handler)
    raiz.setLevel(logging.DEBUG)
    yield handler
    raiz.removeHandler(handler)
    raiz.setLevel(nivel)


# ============================================================================ (1) _he_log_exception


CAMINHOS = [
    pytest.param({"causa": "java.lang.RuntimeException: falha", "severidade": "common"}, logging.WARNING, id="warning"),
    pytest.param({"causa": "java.lang.RuntimeException: falha", "severidade": "fault"}, logging.ERROR, id="erro-fault"),
    pytest.param({"causa": MIRROR_NOT_FOUND_CAUSES[0], "severidade": "common"}, logging.DEBUG, id="debug-espelhamento"),
]


class TestHeLogException:
    @pytest.mark.parametrize(("evento", "nivel"), CAMINHOS)
    def test_the_record_carries_the_returned_code_in_every_path(self, cru, evento, nivel):
        player = jogador(1)

        codigo = _he_log_exception(player, excecao(player, **evento), player.current)

        (registro,) = [r for r in cru.registros if r.levelno == nivel]
        assert ERROR_CODE_RE.fullmatch(codigo) and registro.error_code == codigo and codigo in registro.getMessage()


class TestLogLavalinkFailure:
    def chamar(self):
        return log_lavalink_failure("Falha ao tocar", message="m", cause="c", severity="common", node="N1")

    def test_the_record_carries_the_returned_code(self, cru):
        codigo = self.chamar()

        (registro,) = cru.registros
        assert ERROR_CODE_RE.fullmatch(codigo) and registro.error_code == codigo and codigo in registro.getMessage()


# ============================================================================ (1) ponta a ponta: nenhum código some


class Destinos:
    def __init__(self, tmp_path) -> None:
        self.console = io.StringIO()
        self.relogio = Relogio()
        configure_logging(
            LogSettings(logging.INFO, False, True, frozenset(), 300),
            [],
            stream=self.console,
            logs_dir=tmp_path / ".logs",
            clock=self.relogio,
        )
        self.arquivo = arquivo_do_log(tmp_path / ".logs")

    def textos(self) -> list[str]:
        flush_logging()
        return [self.console.getvalue(), self.arquivo.read_text(encoding="utf-8")]


class TestNenhumCodigoSome:
    def produzir_cinco(self, produtor) -> list[str]:
        return [produtor() for _ in range(5)]

    def conferir(self, destinos, codigos) -> None:
        assert len(set(codigos)) == 5, "os 5 códigos precisam ser diferentes para o teste valer"
        for texto in destinos.textos():
            assert all(codigo in texto for codigo in codigos), "um código mostrado no embed sumiu do log"
            assert "repetida" not in texto, "as linhas com código não entram na contagem de repetidas"
            assert texto.count(codigos[0]) == 5, "as 2ª a 5ª linhas apontam para o 1º código (rota da K8)"

    def test_five_equal_track_failures_keep_every_code_and_point_to_the_first(self, tmp_path):
        destinos = Destinos(tmp_path)
        player = jogador(1)
        evento = excecao(player)

        codigos = self.produzir_cinco(lambda: _he_log_exception(player, evento, player.current))

        self.conferir(destinos, codigos)


# ============================================================================ (2) logs_loop: amostras na thread do loop


class DictVigiado(dict):
    def __init__(self, *args, registro):
        super().__init__(*args)
        self.registro = registro

    def _ver(self):
        self.registro.append(threading.current_thread())

    def items(self):
        self._ver()
        return super().items()

    def values(self):
        self._ver()
        return super().values()

    def keys(self):
        self._ver()
        return super().keys()

    def __iter__(self):
        self._ver()
        return super().__iter__()

    def __len__(self):
        self._ver()
        return super().__len__()

    def get(self, *args):
        self._ver()
        return super().get(*args)


class ListaVigiada(list):
    def __init__(self, *args, registro):
        super().__init__(*args)
        self.registro = registro

    def __iter__(self):
        self.registro.append(threading.current_thread())
        return super().__iter__()

    def __len__(self):
        self.registro.append(threading.current_thread())
        return super().__len__()


class BotVigiado(Bot):
    def __init__(self, registro, **campos):
        super().__init__(**campos)
        self._registro = registro

    @property
    def latency(self):
        self._registro.append(threading.current_thread())
        return 0.05

    @latency.setter
    def latency(self, valor):
        pass


def cenario_vigiado(mutacao=None):
    acessos: list[threading.Thread] = []
    no = no_com_stats("N-OK")
    bot = BotVigiado(
        acessos,
        latency=0.05,
        guilds=3,
        nos=DictVigiado({no.identifier: no}, registro=acessos),
        jogadores=DictVigiado(pings([float(n) for n in range(1, 21)]), registro=acessos),
    )
    bot.guilds = ListaVigiada(range(3), registro=acessos)
    cenario = Cenario(bots=[bot])
    chamadas_stats: list[threading.Thread] = []

    def process_stats(*args):
        chamadas_stats.append(threading.current_thread())
        if mutacao:
            mutacao(bot)
        return PYTHON

    cenario.process_stats = process_stats
    return cenario, acessos, chamadas_stats


class TestAmostrasNaThreadDoLoop:
    def test_every_read_of_players_nodes_guilds_and_latency_happens_on_the_loop_thread(self):
        cenario, acessos, _ = cenario_vigiado()

        linhas = cenario.rodar()

        assert linhas, "o resumo não saiu"
        assert acessos, "o teste não prova nada se ninguém leu os contêineres"
        assert set(acessos) == {threading.current_thread()}

    def test_the_summary_survives_the_containers_changing_while_the_stats_run(self):
        def mutar(bot):
            bot.music.players[len(bot.music.players) + 1000] = SimpleNamespace(
                ping=99.0, current=object(), is_paused=False
            )
            bot.music.nodes[f"NOVO-{len(bot.music.nodes)}"] = no_com_stats(f"NOVO-{len(bot.music.nodes)}")
            bot.guilds.append(object())

        cenario, _, _ = cenario_vigiado(mutacao=mutar)

        linhas = cenario.rodar()

        assert (
            len([linha for linha in linhas if "servidores" in linha]) == 2
        )  # os dois resumos saíram, sem perder nenhum
        assert "WARNING" not in cenario.console.getvalue()


class TestDoisCodigosDiferentes:
    def test_two_identical_track_exceptions_show_both_codes_the_second_on_the_pointer_line(self, tmp_path):
        destinos = Destinos(tmp_path)
        player = jogador(1)
        evento = excecao(player)

        primeiro = _he_log_exception(player, evento, player.current)
        segundo = _he_log_exception(player, evento, player.current)

        assert primeiro != segundo
        for texto in destinos.textos():
            (linha,) = [x for x in texto.splitlines() if segundo in x]
            assert f"traceback igual ao de {primeiro}" in linha
