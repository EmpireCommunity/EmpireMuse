"""2.1.2 (B) — o aviso do disnake "… not installed, voice will NOT be supported" não chega ao console nem ao arquivo.

Origem: ``Client.__init__`` do disnake loga em ``disnake.client`` (WARNING) quando faltam PyNaCl e/ou dave.py; o bot toca
áudio pelo Lavalink e não usa voz do Discord. O filtro descarta SÓ esse registro, antes do ``LevelGate``.
"""

import io
import logging
from pathlib import Path

import pytest

from empiremuse.logs.setup import configure_logging, flush_logging, tick_logging
from empiremuse.logs.settings import LogSettings
from tests.logs.apoio import arquivo_do_log, Relogio

pytestmark = pytest.mark.critical

SUFIXO = "not installed, voice will NOT be supported"
VARIANTES = [
    ("PyNaCl", "is"),
    ("dave.py", "is"),
    ("PyNaCl and dave.py", "are"),
]


@pytest.fixture
def raiz() -> logging.Logger:
    return logging.getLogger()


@pytest.fixture
def console() -> io.StringIO:
    return io.StringIO()


@pytest.fixture
def relogio() -> Relogio:
    return Relogio()


@pytest.fixture
def instalar(raiz: logging.Logger, console: io.StringIO, relogio: Relogio, tmp_path: Path) -> None:
    """Chama o ``configure_logging`` real (console em ``StringIO``, arquivo em ``tmp_path``)."""
    for handler in list(raiz.handlers):
        if type(handler).__module__.startswith("_pytest"):
            raiz.removeHandler(handler)
    configure_logging(
        LogSettings(logging.INFO, False, True, frozenset(), 300),
        [],
        stream=console,
        logs_dir=tmp_path / ".logs",
        clock=relogio,
    )


def saidas(console: io.StringIO, tmp_path: Path) -> list[str]:
    """O texto que chegou ao console e ao arquivo (nesta ordem)."""
    return [console.getvalue(), (arquivo_do_log(tmp_path / ".logs")).read_text(encoding="utf-8")]


class TestDescartado:
    @pytest.mark.parametrize(("pacotes", "verbo"), VARIANTES)
    def test_the_voice_dependency_notice_reaches_neither_console_nor_file(
        self, instalar: None, console: io.StringIO, tmp_path: Path, pacotes: str, verbo: str
    ) -> None:
        logging.getLogger("disnake.client").warning("%s %s " + SUFIXO, pacotes, verbo)

        for texto in saidas(console, tmp_path):
            assert SUFIXO not in texto and "voice" not in texto

    def test_the_repeat_counter_does_not_register_the_dropped_notice(
        self, instalar: None, console: io.StringIO, relogio: Relogio, tmp_path: Path
    ) -> None:
        for pacotes, verbo in VARIANTES * 2:
            logging.getLogger("disnake.client").warning("%s %s " + SUFIXO, pacotes, verbo)
        logging.getLogger("empiremuse.teste").warning("uma ocorrência só")

        flush_logging()
        relogio.avancar(61)
        tick_logging()

        for texto in saidas(console, tmp_path):
            assert "repetida" not in texto and "voice" not in texto
            assert texto.count("uma ocorrência só") == 1


class TestRestanteSegueSaindo:
    def test_another_warning_from_disnake_client_still_goes_out(
        self, instalar: None, console: io.StringIO, tmp_path: Path
    ) -> None:
        logging.getLogger("disnake.client").warning("Shard ID %s heartbeat blocked", None)

        for texto in saidas(console, tmp_path):
            assert "Shard ID None heartbeat blocked" in texto

    @pytest.mark.parametrize(("pacotes", "verbo"), VARIANTES)
    def test_the_same_sentence_from_another_logger_still_goes_out(
        self, instalar: None, console: io.StringIO, tmp_path: Path, pacotes: str, verbo: str
    ) -> None:
        logging.getLogger("empiremuse.x").warning("%s %s " + SUFIXO, pacotes, verbo)

        for texto in saidas(console, tmp_path):
            assert f"{pacotes} {verbo} {SUFIXO}" in texto


class TestFalhaDoFiltro:
    def test_a_record_whose_message_cannot_be_formatted_is_let_through(self) -> None:
        from empiremuse.logs.context import VoiceDependencyNoiseFilter

        registro = logging.getLogger("disnake.client").makeRecord(
            "disnake.client", logging.WARNING, __file__, 1, "%s %s " + SUFIXO, ("so-um-argumento",), None
        )

        assert VoiceDependencyNoiseFilter().filter(registro) is True
