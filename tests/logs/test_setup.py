"""Item 7 (P1, Turbine) — ``configure_logging`` (B8, B19, B6 ponta a ponta): handlers, cadeia de filtros, bibliotecas e arquivo.

O console é um ``StringIO``; o arquivo vai para ``tmp_path``; o relógio do agregador é injetado e o ``environ`` das configurações também.
"""

import io
import logging
import os
import stat
import sys

import pytest

from empiremuse.logs.setup import LIBRARY_LOGGERS, configure_logging, flush_logging, tick_logging
from empiremuse.logs.settings import LogSettings, parse_log_settings
from empiremuse.utils.file_log import DailyRetentionFileHandler
from tests.logs.apoio import arquivo_do_log, Espiao, Relogio

pytestmark = pytest.mark.critical

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="modos de arquivo POSIX")
A, B = 111111111111111111, 222222222222222222
CADEIA = ["ContextFilter", "VoiceDependencyNoiseFilter", "LevelGate", "SanitizeAndRedact", "RepeatAggregator"]


def ajustes(*, nivel=logging.INFO, cor=False, arquivo=False, guildas=()):
    return LogSettings(nivel, cor, arquivo, frozenset(guildas), 300)


@pytest.fixture
def raiz():
    return logging.getLogger()


@pytest.fixture
def relogio():
    return Relogio()


@pytest.fixture
def console():
    return io.StringIO()


@pytest.fixture
def instalar(raiz, console, relogio, tmp_path):
    """Chama o ``configure_logging`` real e devolve os handlers que ele instalou (console, arquivo)."""

    def _instalar(settings=None, segredos=(), **extra):
        for handler in list(
            raiz.handlers
        ):  # o handler de captura do pytest formataria os argumentos de todo DEBUG e mascararia o que se mede
            if type(handler).__module__.startswith("_pytest"):
                raiz.removeHandler(handler)
        antes = list(raiz.handlers)
        configure_logging(
            settings or ajustes(),
            segredos,
            stream=extra.pop("stream", console),
            logs_dir=extra.pop("logs_dir", tmp_path / ".logs"),
            clock=relogio,
            **extra,
        )
        novos = [h for h in raiz.handlers if h not in antes]
        consoles = [h for h in novos if not isinstance(h, logging.FileHandler)]
        arquivos = [h for h in novos if isinstance(h, logging.FileHandler)]
        return consoles, arquivos

    return _instalar


def nomes(filtros) -> list[str]:
    return [type(f).__name__ for f in filtros]


class TestHandlers:
    def test_one_console_handler_on_the_given_stream_and_none_for_the_file_when_off(self, instalar, console):
        consoles, arquivos = instalar(ajustes(arquivo=False))

        assert len(consoles) == 1 and arquivos == []
        assert isinstance(consoles[0], logging.StreamHandler) and consoles[0].stream is console

    def test_the_file_handler_is_added_when_the_file_is_on(self, instalar):
        consoles, arquivos = instalar(ajustes(arquivo=True))

        assert len(consoles) == 1 and len(arquivos) == 1
        assert isinstance(arquivos[0], DailyRetentionFileHandler)

    @pytest.mark.parametrize("com_arquivo", [False, True])
    def test_the_filter_chain_is_the_same_and_in_the_contract_order(self, instalar, com_arquivo):
        consoles, arquivos = instalar(ajustes(arquivo=com_arquivo))

        for handler in consoles + arquivos:
            assert nomes(handler.filters) == CADEIA


class TestNivelDaRaiz:
    def test_the_root_follows_the_configured_level(self, instalar, raiz):
        instalar(ajustes(nivel=logging.WARNING))

        assert raiz.level == logging.WARNING

    def test_the_root_is_debug_when_there_are_debug_guilds(self, instalar, raiz):
        instalar(ajustes(nivel=logging.INFO, guildas={A}))

        assert raiz.level == logging.DEBUG

    def test_with_debug_guilds_the_gate_drops_other_servers_before_formatting_anything(self, instalar, console):
        instalar(ajustes(nivel=logging.INFO, guildas={A}))
        espiao = Espiao()

        logging.getLogger("empiremuse.player").debug("faixa %s", espiao, extra={"guild": B})
        logging.getLogger("empiremuse.player").debug("sem servidor %s", espiao)

        assert espiao.chamadas == 0 and console.getvalue() == ""

    @pytest.mark.parametrize("nome", ["charset_normalizer", "requests", "curl_cffi", "dns.resolver"])
    def test_third_party_debug_inside_the_context_of_a_server_in_debug_stays_out(self, instalar, console, nome):
        from empiremuse.logs.context import log_context

        instalar(ajustes(nivel=logging.INFO, guildas={A}))

        with log_context(guild=A):
            logging.getLogger(nome).debug("detalhe interno de biblioteca")
            logging.getLogger("empiremuse.player").debug("detalhe nosso")

        saida = console.getvalue()
        assert "detalhe interno de biblioteca" not in saida and "detalhe nosso" in saida


class TestBibliotecas:
    @pytest.mark.parametrize("nivel", [logging.DEBUG, logging.INFO, logging.ERROR])
    def test_the_libraries_are_always_at_warning(self, instalar, nivel):
        instalar(ajustes(nivel=nivel))

        assert all(logging.getLogger(nome).level == logging.WARNING for nome in LIBRARY_LOGGERS)

    def test_a_library_info_is_dropped_and_its_warning_comes_out(self, instalar, console):
        instalar(ajustes(nivel=logging.DEBUG))

        logging.getLogger("aiohttp.client").info("conexão aberta")
        logging.getLogger("aiohttp.client").warning("conexão lenta")

        assert "conexão aberta" not in console.getvalue() and "conexão lenta" in console.getvalue()


class TestIdempotencia:
    def test_a_second_call_leaves_one_console_handler_and_at_most_one_file_handler(self, raiz, instalar):
        antes = list(raiz.handlers)
        instalar(ajustes(arquivo=True))
        instalar(ajustes(arquivo=True))

        novos = [h for h in raiz.handlers if h not in antes]
        assert len([h for h in novos if not isinstance(h, logging.FileHandler)]) == 1
        assert len([h for h in novos if isinstance(h, logging.FileHandler)]) == 1

    def test_after_the_second_call_a_record_is_written_once(self, instalar, console):
        instalar(ajustes())
        instalar(ajustes())

        logging.getLogger("empiremuse.teste").info("uma vez só")

        assert console.getvalue().count("uma vez só") == 1


class TestArquivoB19:
    @POSIX
    def test_the_file_exists_with_mode_0600_and_the_folder_0700(self, instalar, tmp_path):
        instalar(ajustes(arquivo=True))

        arquivo = arquivo_do_log(tmp_path / ".logs")
        assert arquivo.is_file()
        assert stat.S_IMODE(arquivo.stat().st_mode) == 0o600 and stat.S_IMODE(arquivo.parent.stat().st_mode) == 0o700

    def test_auto_inside_the_image_creates_nothing(self, instalar, tmp_path):
        settings, _ = parse_log_settings({"LOG_FILE": "auto"}, {"EMPIREMUSE_VERSION": "2.1.0"})

        consoles, arquivos = instalar(settings)

        assert arquivos == [] and not (tmp_path / ".logs").exists() and len(consoles) == 1


class TestSegredosEAgregador:
    def test_the_secrets_given_to_configure_logging_are_masked_in_every_destination(self, instalar, console, tmp_path):
        instalar(ajustes(arquivo=True), ["senha-do-configure-logging-1"])

        logging.getLogger("empiremuse.teste").warning("usando senha-do-configure-logging-1 agora")

        for texto in (console.getvalue(), (arquivo_do_log(tmp_path / ".logs")).read_text(encoding="utf-8")):
            assert "senha-do-configure-logging-1" not in texto and "***" in texto

    def test_flush_logging_emits_the_pending_repeat_count_in_both_destinations(self, instalar, console, tmp_path):
        instalar(ajustes(arquivo=True))
        for n in range(3):
            logging.getLogger("empiremuse.wavelink").warning("nó %s caiu", n)

        flush_logging()

        for texto in (console.getvalue(), (arquivo_do_log(tmp_path / ".logs")).read_text(encoding="utf-8")):
            assert texto.count("repetida 2×") == 1
