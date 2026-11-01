"""Item 7 (P2, Turbine) — relatório de subida: B9 (``BootReport``), B10 (versão nova), B11 (sem ``print``), B11b (stderr do Java).

``BootReport`` e ``check_new_version`` vêm de ``empiremuse.logs.boot`` (contrato 3.12). A captura é um handler SEM filtros no root (vê o
registro cru, antes do redator): o que o código mandou ao log, não o que o redator limpou. Relógio e rede injetados; nada dorme.
"""
# ruff: noqa: F811 - as fixtures importadas de test_secret_sources são usadas como parâmetro

import ast
import asyncio
import io
import json
import logging
import re
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from empiremuse import paths
from empiremuse.logs.boot import BootReport, check_new_version
from empiremuse.logs.redact import register_secret
from empiremuse.logs.setup import configure_logging, flush_logging
from empiremuse.logs.settings import LogSettings
from empiremuse.rules.release import Version, parse_current_version
from empiremuse.updater import releases
from empiremuse.updater.git_flow import UpdateError
from empiremuse.i18n import loader as client_i18n
from empiremuse.utils import client
from empiremuse.utils.music import local_lavalink
from tests.logs.test_secret_sources import fabrica, no_json, runtime, sem_rede  # noqa: F401 - fixtures reaproveitadas
from tests.support.raiz import RAIZ

pytestmark = pytest.mark.critical

LOAD_MODULES_REAL = client.BotCore.load_modules  # antes de qualquer teste trocar o ``BotCore`` por um dublê


class Captura(logging.Handler):
    """Registros crus que chegam ao root (sem filtro nenhum): o que o código mandou, antes de redigir."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.registros: list[logging.LogRecord] = []
        self.fotos: list[SimpleNamespace] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.registros.append(record)
        # o ``SanitizeAndRedact`` dos outros handlers muda o mesmo registro depois (zera o ``exc_info``): a foto é imutável
        self.fotos.append(
            SimpleNamespace(
                name=record.name,
                levelno=record.levelno,
                mensagem=record.getMessage(),
                exc_tipo=record.exc_info[0] if record.exc_info else None,
            )
        )

    def limpar(self) -> None:
        self.registros.clear()
        self.fotos.clear()

    def do_logger(self, prefixo: str) -> list[logging.LogRecord]:
        return [r for r in self.registros if r.name == prefixo or r.name.startswith(prefixo + ".")]


@pytest.fixture
def captura():
    raiz = logging.getLogger()
    handler, nivel = Captura(), raiz.level
    raiz.addHandler(handler)
    raiz.setLevel(logging.DEBUG)
    yield handler
    raiz.removeHandler(handler)
    raiz.setLevel(nivel)


def so_boot(captura) -> list[logging.LogRecord]:
    return captura.do_logger("empiremuse.boot")


# ============================================================================ B9 — BootReport


class TestBootReport:
    @pytest.mark.parametrize(
        ("status", "nivel"), [("ok", logging.INFO), ("warn", logging.WARNING), ("fail", logging.ERROR)]
    )
    def test_each_status_maps_to_its_level_on_the_boot_logger(self, captura, status, nivel):
        BootReport().step("Etapa X", status, "detalhe Y")

        (registro,) = so_boot(captura)
        assert registro.name == "empiremuse.boot" and registro.levelno == nivel
        assert "Etapa X" in registro.getMessage() and "detalhe Y" in registro.getMessage()

    def test_deferred_steps_are_silent_until_flush_and_then_come_out_in_order(self, captura):
        relatorio = BootReport()

        relatorio.defer("Pasta de execução", "ok", "/dados")
        relatorio.defer("Python", "ok", "3.14")
        relatorio.defer("Aviso", "warn", "algo")
        assert so_boot(captura) == []
        relatorio.flush_deferred()

        assert [r.levelno for r in so_boot(captura)] == [logging.INFO, logging.INFO, logging.WARNING]
        mensagens = [r.getMessage() for r in so_boot(captura)]
        assert "Pasta de execução" in mensagens[0] and "Python" in mensagens[1] and "Aviso" in mensagens[2]

    def test_a_detail_with_a_registered_secret_is_masked_by_the_pipeline(self, tmp_path):
        console = io.StringIO()
        register_secret("senha-no-detalhe-da-etapa-1")
        configure_logging(LogSettings(logging.INFO, False, False, frozenset(), 300), [], stream=console)

        BootReport().step("Nó", "ok", "conectado com senha-no-detalhe-da-etapa-1")

        assert "senha-no-detalhe-da-etapa-1" not in console.getvalue() and "***" in console.getvalue()


# ============================================================================ B9 — o que a subida real manda ao log


class TestSubidaReal:
    def test_the_mongo_uri_with_a_password_never_reaches_the_boot_lines(self, fabrica, captura, monkeypatch):
        senha = "senha-do-mongo-da-subida-Z9"
        uri = f"mongodb+srv://usuario:{senha}@cluster0.exemplo.net/banco"
        monkeypatch.setattr(client, "MongoDatabase", MagicMock())
        pool = fabrica(MONGO=uri)

        pool.setup()

        boot = so_boot(captura)
        assert boot, "a subida não registrou nenhuma etapa em empiremuse.boot"
        for registro in boot:
            texto = registro.getMessage()
            assert (
                senha not in texto
                and "usuario" not in texto
                and "cluster0.exemplo.net" not in texto
                and uri not in texto
            )
        assert any("mongo" in r.getMessage().lower() for r in boot)

    def test_a_node_with_a_password_never_shows_it_during_the_setup(self, fabrica, captura):
        senha = "senha-do-no-da-subida-Y8"
        pool = fabrica(LAVALINK_NODE_1=no_json(senha, "NO_DA_SUBIDA"))

        pool.setup()

        textos = [r.getMessage() for r in captura.registros]
        assert not any(senha in t for t in textos)

    @pytest.mark.asyncio
    async def test_a_node_check_shows_only_the_identifier_and_the_result_never_the_password(self, captura, monkeypatch):
        senha = "senha-do-no-verificado-X7"

        class Resposta:
            status = 403

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class Sessao:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def get(self, *args, **kwargs):
                return Resposta()

        monkeypatch.setattr(client.asyncio, "sleep", AsyncMock())
        monkeypatch.setattr(client.aiohttp, "ClientSession", Sessao)
        pool = client.BotPool.__new__(client.BotPool)

        await pool.check_node(
            {"host": "10.1.2.3", "port": 2333, "password": senha, "identifier": "NO_VERIFICADO", "retries": 1}
        )

        textos = [r.getMessage() for r in captura.registros if r.name.startswith("empiremuse")]
        assert any("NO_VERIFICADO" in t for t in textos)
        assert not any(senha in t or "10.1.2.3" in t for t in textos)


class TestFalhaDeCog:
    @pytest.fixture
    def bot_falso(self):
        from disnake.ext import commands

        class Carregador:
            falhar = "ll_yt_oauth"

            def __init__(self) -> None:
                self.user = "bot"
                self.identifier = "id"
                self.bot_ready = True
                self.config = {"ENABLE_DISCORD_URLS_PLAYBACK": True}
                self.slash_commands = []
                self.remove_slash_command = MagicMock()

            def unload_extension(self, nome):
                raise commands.ExtensionNotLoaded(nome)

            def load_extension(self, nome):
                if nome.endswith("." + self.falhar):
                    raise RuntimeError(f"explodiu ao carregar {nome} com detalhe-interno")

        return Carregador()

    def test_a_failing_cog_is_reported_with_its_module_and_the_exception_type_without_a_traceback(
        self, bot_falso, captura
    ):
        captura.registros.clear()
        logging.getLogger().setLevel(logging.INFO)

        status = client.BotCore.load_modules(bot_falso)

        assert any("ll_yt_oauth" in f for f in status["failed"])
        achados = [r for r in captura.registros if "ll_yt_oauth" in r.getMessage() and r.levelno >= logging.WARNING]
        assert achados and all(r.exc_info is None for r in achados)
        assert any("RuntimeError" in r.getMessage() for r in achados)


# ============================================================================ B10 — versão nova (relógio e rede injetados)


def lista_de(*versoes: str):
    async def listar(session):
        return [
            releases.Release(Version(*map(int, v.split("."))), f"v{v}", f"https://exemplo.invalido/{v}")
            for v in versoes
        ]

    return listar


async def checar(captura, *, lista, atual, timeout=10.0):
    captura.registros.clear()
    await check_new_version(list_releases=lista, current=lambda: atual, timeout=timeout)
    return [r for r in captura.registros if r.name.startswith("empiremuse")]


class TestVersaoNova:
    @pytest.mark.asyncio
    async def test_a_higher_release_gives_an_info_line_with_both_versions(self, captura):
        registros = await checar(captura, lista=lista_de("2.2.0", "2.1.0"), atual=Version(2, 1, 0))

        infos = [r for r in registros if r.levelno == logging.INFO]
        assert len(infos) == 1 and "Versão 2.2.0 disponível (instalada 2.1.0)" in infos[0].getMessage()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("versoes", [("2.1.0",), ("2.0.0", "2.1.0"), ("2.0.9",), ()])
    async def test_an_equal_or_older_release_logs_nothing_above_debug(self, captura, versoes):
        registros = await checar(captura, lista=lista_de(*versoes), atual=Version(2, 1, 0))

        assert [r for r in registros if r.levelno > logging.DEBUG] == []

    @pytest.mark.asyncio
    async def test_an_error_gives_a_single_debug_line_and_nothing_louder(self, captura):
        async def falha(session):
            raise UpdateError("Não consegui consultar as versões publicadas agora.")

        registros = await checar(captura, lista=falha, atual=Version(2, 1, 0))

        assert len(registros) == 1 and registros[0].levelno == logging.DEBUG

    @pytest.mark.asyncio
    async def test_a_timeout_gives_only_debug_and_does_not_hang(self, captura):
        async def pendurada(session):
            await asyncio.Event().wait()

        registros = await checar(captura, lista=pendurada, atual=Version(2, 1, 0), timeout=0)

        assert registros and all(r.levelno == logging.DEBUG for r in registros)


# ============================================================================ B11 — nenhum print nos módulos do P2 (AST)


# ============================================================================ B11b — stderr do Java do Lavalink local (V-L9)


class SemQuebra(str):
    """Última linha do Java sem ``\\n`` no fim (o processo morreu no meio dela)."""


class StderrFalso:
    """Fluxo que serve linhas de ``str`` e de ``bytes`` (o código pode ler de qualquer forma) e sinaliza o fim da leitura."""

    class Linha(str):
        def decode(self, *args, **kwargs):
            return str(self)

        def encode(self, *args, **kwargs):
            return str(self).encode("utf-8")

    class Fim(str):
        def __eq__(self, other):
            return other in ("", b"", None) or str.__eq__(self, other)

        __hash__ = str.__hash__

        def __bool__(self):
            return False

        def decode(self, *args, **kwargs):
            return ""

    def __init__(self, linhas, terminou: threading.Event) -> None:
        self._linhas = [self.Linha(linha if isinstance(linha, SemQuebra) else linha + "\n") for linha in linhas]
        self._fim = terminou

    def readline(self, *args):
        if self._linhas:
            return self._linhas.pop(0)
        self._fim.set()
        return self.Fim("")

    def __iter__(self):
        return self

    def __next__(self):
        linha = self.readline()
        if not linha:
            raise StopIteration
        return linha

    def close(self):
        return None


class ProcessoFalso:
    def __init__(self, linhas, terminou, **kwargs) -> None:
        self.args, self.kwargs = kwargs.get("args"), kwargs
        self.stderr = StderrFalso(linhas, terminou)
        self.stdout = None
        self.pid = 4242
        self.returncode = None

    def wait(self, *args, **kwargs):
        self.returncode = 0
        return 0

    def poll(self):
        return None

    def kill(self):
        return None

    def terminate(self):
        return None


@pytest.fixture
def lavalink_com_stderr(tmp_path, monkeypatch):
    """Roda o ``run_lavalink`` REAL com Java, arquivos e ``Popen`` dublados; devolve o console do ``configure_logging`` real."""
    monkeypatch.setenv("EMPIREMUSE_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    (tmp_path / "lavalink").mkdir()
    monkeypatch.setattr(
        local_lavalink.platform, "architecture", lambda *a, **k: ("64bit", "ELF")
    )  # o real roda ``file`` via Popen
    monkeypatch.setattr(local_lavalink, "_configured_java", lambda caminho: "java")
    monkeypatch.setattr(local_lavalink, "prepare_lavalink_files", lambda *a, **k: None)
    monkeypatch.setattr(local_lavalink, "_java_command", lambda *a, **k: ["java", "-jar", "Lavalink.jar"])
    monkeypatch.setattr(local_lavalink, "java_major_version", lambda cmd, *a, **k: 21)

    def executar(linhas, arquivo=False):
        terminou = threading.Event()
        monkeypatch.setattr(
            local_lavalink.subprocess,
            "Popen",
            lambda args, **kwargs: ProcessoFalso(linhas, terminou, args=args, **kwargs),
        )
        console = io.StringIO()
        configure_logging(
            LogSettings(logging.INFO, False, arquivo, frozenset(), 300), [], stream=console, logs_dir=tmp_path / ".logs"
        )
        local_lavalink.run_lavalink(
            lavalink_java_path="java", lavalink_home=str(tmp_path / "lavalink"), lavalink_offline=True
        )
        assert terminou.wait(timeout=3), "o stderr do Java nunca foi lido até o fim"
        flush_logging()
        return console.getvalue()

    return executar


class TestStderrDoJava:
    def test_a_line_with_the_lavalink_password_goes_through_the_wavelink_local_logger_redacted(
        self, lavalink_com_stderr
    ):
        senha = "senha-do-lavalink-no-stderr-W7"
        register_secret(senha)

        saida = lavalink_com_stderr([f"ERROR conexão recusada com a senha {senha} no cabeçalho"])

        linhas = [linha for linha in saida.splitlines() if "empiremuse.wavelink.local" in linha]
        assert len(linhas) == 1 and "WARNING" in linhas[0]
        assert senha not in saida and "***" in linhas[0] and "conexão recusada" in linhas[0]

    @pytest.mark.parametrize("sujeira", ["\x1b[2J\x1b[31mlimpa a tela", "tem \x9b e ‮ dentro", "carriage\rreturn"])
    def test_control_characters_in_a_stderr_line_are_neutralised(self, lavalink_com_stderr, sujeira):
        saida = lavalink_com_stderr([sujeira])

        assert len(saida.splitlines()) == 1 and "empiremuse.wavelink.local" in saida
        assert not any(c in saida for c in ("\x1b", "\x9b", "‮", "\r"))


# ============================================================================ integração — subida falsa com console e arquivo reais


class TestIntegracaoDaSubida:
    @pytest.fixture
    def destinos(self, tmp_path):
        console = io.StringIO()
        configure_logging(
            LogSettings(logging.INFO, False, True, frozenset(), 300), [], stream=console, logs_dir=tmp_path / ".logs"
        )
        return SimpleNamespace(console=console, arquivo=tmp_path / ".logs" / "disnake.log")

    def textos(self, destinos):
        return [destinos.console.getvalue(), destinos.arquivo.read_text(encoding="utf-8")]

    def test_a_fake_boot_comes_out_in_order_in_both_destinations(self, destinos):
        relatorio = BootReport()
        relatorio.defer("Pasta de execução", "ok", "/dados")
        relatorio.defer("Python", "ok", "3.14")
        relatorio.flush_deferred()
        relatorio.step("Banco", "ok", "local", elapsed=0.002)
        relatorio.step("Cogs", "warn", "1 falhou")
        relatorio.step("Lavalink", "fail", "sem nós")

        for texto in self.textos(destinos):
            posicoes = [texto.index(nome) for nome in ("Pasta de execução", "Python", "Banco", "Cogs", "Lavalink")]
            assert posicoes == sorted(posicoes)
            assert texto.count("Pasta de execução") == 1 and texto.count("Python") == 1
            assert "INFO" in texto and "WARNING" in texto and "ERROR" in texto

    def test_a_secret_in_a_step_detail_is_masked_in_both_destinations(self, destinos):
        register_secret("segredo-da-etapa-integrada-5")

        BootReport().step("Nó", "warn", "falhou com segredo-da-etapa-integrada-5")

        for texto in self.textos(destinos):
            assert "segredo-da-etapa-integrada-5" not in texto and "***" in texto


# ============================================================================ V-P2-1 — linhas distintas do Java não se agregam entre si

SONDA_DO_VIGIL = [
    "Exception in thread main java.lang.IllegalStateException: falhou ao subir",
    "\tat com.exemplo.Servidor.iniciar(Servidor.java:42)",
    "\tat com.exemplo.Principal.main(Principal.java:7)",
    "Caused by: java.net.BindException: Address already in use",
    "linha com \x1b[2J e \u202e dentro",
    SemQuebra("última linha sem quebra"),
]


class TestLinhasDistintasDoJava:
    def textos(self, lavalink_com_stderr, tmp_path, linhas):
        console = lavalink_com_stderr(linhas, arquivo=True)
        return console, (tmp_path / ".logs" / "disnake.log").read_text(encoding="utf-8")

    def test_six_distinct_lines_give_six_lines_in_each_destination_and_no_repeat_summary(
        self, lavalink_com_stderr, tmp_path
    ):
        for texto in self.textos(lavalink_com_stderr, tmp_path, SONDA_DO_VIGIL):
            linhas = [linha for linha in texto.splitlines() if linha]
            assert len(linhas) == 6, linhas
            assert all("empiremuse.wavelink.local" in linha for linha in linhas)
            assert "↻" not in texto and "repetida" not in texto

    def test_controls_and_the_registered_password_are_still_neutralised_in_the_distinct_lines(
        self, lavalink_com_stderr, tmp_path
    ):
        senha = "senha-do-java-nas-distintas-V2"
        register_secret(senha)
        linhas = [f"tentando com {senha}", *SONDA_DO_VIGIL]

        for texto in self.textos(lavalink_com_stderr, tmp_path, linhas):
            assert senha not in texto and "***" in texto
            assert not any(c in texto for c in ("\x1b", "\u202e", "\r"))
            assert len([linha for linha in texto.splitlines() if linha]) == 7

    def test_percent_signs_inside_a_java_line_never_break_the_formatting(self, lavalink_com_stderr, tmp_path):
        linha = "GC pausa 100% do heap %d %s %(x)s fim"

        for texto in self.textos(lavalink_com_stderr, tmp_path, [linha]):
            assert linha in texto and "[log] falha" not in texto


# ============================================================================ CodeRabbit #126 — initial_setup, tarefa da versão nova, linhas longas


class ConfigPermissiva(dict):
    """``bot.config`` que o ``setup`` lê chave por chave: o que não foi dado vale ``False``."""

    def __missing__(self, chave):
        return False


def montar_pool_com_bot(fabrica, monkeypatch, *, carregador=None, cooldowns_falham=False):
    """``BotPool.setup`` REAL com um bot falso cujo ``initial_setup`` fica guardado (as corrotinas) para o teste rodar à mão."""
    corrotinas = []
    bot = MagicMock(identifier="bot1")
    bot.loop.create_task.side_effect = corrotinas.append
    bot.wait_until_ready = AsyncMock()
    bot.update_appinfo = AsyncMock()
    bot.session = object()
    bot.config = ConfigPermissiva({"AUTO_SYNC_COMMANDS": False})
    if carregador is not None:
        bot.load_modules = lambda **kwargs: LOAD_MODULES_REAL(carregador, **kwargs)
    else:
        bot.load_modules = MagicMock(return_value={})
    if cooldowns_falham:
        bot.sync_command_cooldowns = MagicMock(side_effect=ValueError("cooldowns quebraram"))
    monkeypatch.setattr(client, "MongoDatabase", MagicMock())
    pool = fabrica()
    monkeypatch.setattr(client, "BotCore", lambda **kwargs: bot)
    pool.check_pool_ready = AsyncMock()
    return pool, bot, corrotinas


def carregador_que_falha():
    from disnake.ext import commands

    class Carregador:
        def __init__(self) -> None:
            self.user, self.identifier, self.bot_ready = "bot", "id", False
            self.config = {"ENABLE_DISCORD_URLS_PLAYBACK": True}
            self.slash_commands = []
            self.remove_slash_command = MagicMock()

        def unload_extension(self, nome):
            raise commands.ExtensionNotLoaded(nome)

        def load_extension(self, nome):
            raise RuntimeError(f"explodiu ao carregar {nome} com detalhe-interno")

    return Carregador()


def rodar_initial_setup(corrotinas) -> None:
    for corrotina in corrotinas:
        asyncio.run(corrotina)


class TestInitialSetup:
    def test_a_cog_failure_gives_no_error_record_and_no_traceback_above_debug(self, fabrica, captura, monkeypatch):
        pool, bot, corrotinas = montar_pool_com_bot(fabrica, monkeypatch, carregador=carregador_que_falha())
        pool.setup()
        captura.limpar()

        rodar_initial_setup(corrotinas)

        proprios = [f for f in captura.fotos if f.name.startswith("empiremuse")]
        assert not [f for f in proprios if f.levelno >= logging.ERROR], "falha de cog não pode virar ERROR"
        assert not [f for f in proprios if f.exc_tipo and f.levelno > logging.DEBUG], "traceback só em DEBUG"

    def test_a_cog_failure_reports_the_module_and_type_and_that_the_initial_setup_stopped_as_warnings(
        self,
        fabrica,
        captura,
        monkeypatch,
    ):
        pool, bot, corrotinas = montar_pool_com_bot(fabrica, monkeypatch, carregador=carregador_que_falha())
        pool.setup()
        captura.limpar()

        rodar_initial_setup(corrotinas)

        avisos = [f.mensagem for f in captura.fotos if f.levelno == logging.WARNING]
        assert any("RuntimeError" in texto and ".py" in texto for texto in avisos)
        assert any("configuração inicial" in texto.lower() for texto in avisos)
        assert not any("detalhe-interno" in texto for texto in avisos), "a mensagem da exceção só vale no DEBUG"

    def test_the_initial_setup_still_finishes_after_a_cog_failure(self, fabrica, monkeypatch):
        pool, bot, corrotinas = montar_pool_com_bot(fabrica, monkeypatch, carregador=carregador_que_falha())
        pool.setup()

        rodar_initial_setup(corrotinas)

        assert bot.bot_ready is True and bot.update_appinfo.await_count == 1

    def test_a_failure_that_is_not_from_a_cog_keeps_the_error_with_its_traceback(self, fabrica, captura, monkeypatch):
        pool, bot, corrotinas = montar_pool_com_bot(fabrica, monkeypatch, cooldowns_falham=True)
        pool.setup()
        captura.limpar()

        rodar_initial_setup(corrotinas)

        erros = [f for f in captura.fotos if f.levelno >= logging.ERROR and f.name.startswith("empiremuse")]
        assert erros and any(f.exc_tipo is ValueError for f in erros)


class TestTarefaDaVersaoNova:
    def test_after_setup_it_is_a_task_and_after_shutdown_it_is_cancelled_and_still_there(self, fabrica):
        pool = fabrica()
        pool.setup()

        tarefa = pool.version_check_task
        assert isinstance(tarefa, asyncio.Task)

        asyncio.run(pool.shutdown(0))
        pool.loop.run_until_complete(asyncio.sleep(0))

        assert tarefa.cancelled() or tarefa.done()
        assert pool.version_check_task is tarefa


class TestLinhasLongasDoJava:
    BASE = "x" * 300

    def textos(self, lavalink_com_stderr, tmp_path, linhas):
        console = lavalink_com_stderr(linhas, arquivo=True)
        return console, (tmp_path / ".logs" / "disnake.log").read_text(encoding="utf-8")

    def test_two_long_lines_that_differ_only_after_the_256th_character_are_two_records(
        self, lavalink_com_stderr, tmp_path
    ):
        linhas = [self.BASE + "-final-A", self.BASE + "-final-B"]

        for texto in self.textos(lavalink_com_stderr, tmp_path, linhas):
            assert "-final-A" in texto and "-final-B" in texto
            assert "repetida" not in texto and "↻" not in texto
            assert len([linha for linha in texto.splitlines() if linha]) == 2


class TestOrdemDaSubidaDoIdioma:
    """I-12 (item 21): ``i18n.load`` → ``migrate_language`` → bots, sempre nessa ordem; a migração que falha não derruba a subida."""

    def montar(self, fabrica, monkeypatch, *, migracao_falha=False, carga_falha=False):
        ordem = []
        pool = fabrica()

        def carregar(**kwargs):
            ordem.append("load")
            if carga_falha:
                raise client_i18n.I18nError("catálogo obrigatório inválido")

        async def migrar():
            ordem.append("migrate")
            if migracao_falha:
                raise RuntimeError("banco fora")
            return 0, 0

        monkeypatch.setattr(pool.i18n, "load", carregar)
        monkeypatch.setattr(pool.servers, "migrate_language", migrar)
        fabrica_de_bot = client.BotCore

        def bot_registrado(**kwargs):
            ordem.append("bot")
            return fabrica_de_bot(**kwargs)

        monkeypatch.setattr(client, "BotCore", bot_registrado)
        return pool, ordem

    def test_load_then_the_migration_then_the_first_bot(self, fabrica, monkeypatch):
        pool, ordem = self.montar(fabrica, monkeypatch)

        pool.setup()

        assert ordem[:3] == ["load", "migrate", "bot"]

    def test_a_failing_migration_leaves_the_servers_for_the_next_boot_and_the_boot_goes_on(self, fabrica, monkeypatch):
        pool, ordem = self.montar(fabrica, monkeypatch, migracao_falha=True)

        pool.setup()

        assert ordem[:3] == ["load", "migrate", "bot"]

    def test_a_broken_required_catalog_stops_the_boot_before_the_migration_and_before_any_bot(
        self, fabrica, monkeypatch
    ):
        pool, ordem = self.montar(fabrica, monkeypatch, carga_falha=True)

        with pytest.raises(client_i18n.I18nError):
            pool.setup()

        assert ordem == ["load"]
