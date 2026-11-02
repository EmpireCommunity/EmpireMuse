"""Item 7, P4 — ganchos globais de exceção no logger ``empiremuse.errors`` (B15) e erros esperados × inesperados (B17).

Nomes novos em ``logs/errors.py`` (Turbine; Keystone confirma): ``loop_exception_handler(loop, context)`` e ``install_exception_handlers(loop)``.
``sys.excepthook``/``threading.excepthook`` já existem (P1, ``install_early_hooks``) e passam a registrar no logger ``empiremuse.errors``.
"""

# ruff: noqa: F811
import asyncio
import logging
import re
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import disnake
import pytest
from disnake.ext import commands

from empiremuse.logs.errors import install_exception_handlers, loop_exception_handler
from empiremuse.logs.hooks import install_early_hooks
from empiremuse.logs.setup import configure_logging
from empiremuse.utils.music.errors import GenericError
from tests.cogs.test_error_handler import _interacao, enviado, handler  # noqa: F401  (fixtures)
from tests.logs.apoio_erros import configuracao, novo_fluxo
from tests.logs.apoio import excecao
from empiremuse.i18n.service import get_i18n

pytestmark = pytest.mark.critical

LOGGER_DE_ERROS = "empiremuse.errors"


class Captura(logging.Handler):
    """Guarda uma FOTO do registro: os filtros dos handlers seguintes mutam o original (``exc_info`` vira ``None``)."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.registros: list[SimpleNamespace] = []

    def emit(self, registro: logging.LogRecord) -> None:
        mensagem = registro.getMessage()
        self.registros.append(
            SimpleNamespace(
                levelno=registro.levelno, exc_info=registro.exc_info, name=registro.name, getMessage=lambda: mensagem
            )
        )


@pytest.fixture
def ganchos(monkeypatch):
    """Ganchos instalados + ``configure_logging`` real; devolve ``(registros do logger de erros, saída do console)``."""
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    captura, saida = Captura(), novo_fluxo()
    logger = logging.getLogger(LOGGER_DE_ERROS)
    logger.addHandler(captura)
    nivel = logger.level
    logger.setLevel(logging.DEBUG)
    install_early_hooks({}, stream=novo_fluxo())
    configure_logging(configuracao(), [], stream=saida)
    yield captura.registros, saida
    logger.removeHandler(captura)
    logger.setLevel(nivel)


class TestGanchosDeProcesso:
    def test_an_unhandled_exception_goes_to_the_errors_logger_with_its_traceback(self, ganchos):
        registros, saida = ganchos

        sys.excepthook(*excecao("falha-no-processo-A1", RuntimeError))

        (registro,) = [r for r in registros if r.levelno >= logging.ERROR]
        assert registro.exc_info and registro.exc_info[0] is RuntimeError
        assert "falha-no-processo-A1" in saida.getvalue() and "Traceback" in saida.getvalue()

    def test_an_exception_in_a_thread_goes_to_the_errors_logger_with_the_thread_name(self, ganchos):
        registros, saida = ganchos

        def falhar() -> None:
            raise RuntimeError("falha-na-thread-B2")

        thread = threading.Thread(target=falhar, name="trabalhadora-p4")
        thread.start()
        thread.join(10)

        erros = [r for r in registros if r.levelno >= logging.ERROR]
        assert erros and erros[0].exc_info[0] is RuntimeError
        assert "trabalhadora-p4" in saida.getvalue() + erros[0].getMessage()


class TestLacoDeEventos:
    def test_the_loop_handler_logs_the_exception_with_traceback_at_error(self, ganchos):
        registros, saida = ganchos
        loop = asyncio.new_event_loop()
        try:
            erro = excecao("falha-na-tarefa-C3", RuntimeError)[1]
            loop_exception_handler(loop, {"message": "Task exception was never retrieved", "exception": erro})
        finally:
            loop.close()

        (registro,) = [r for r in registros if r.levelno >= logging.ERROR]
        assert registro.exc_info[1] is erro and "falha-na-tarefa-C3" in saida.getvalue()

    def test_cancelled_error_on_shutdown_is_not_an_error(self, ganchos):
        registros, saida = ganchos
        loop = asyncio.new_event_loop()
        try:
            loop_exception_handler(loop, {"message": "cancelada", "exception": asyncio.CancelledError()})
        finally:
            loop.close()

        assert [r for r in registros if r.levelno >= logging.WARNING] == []
        assert "Traceback" not in saida.getvalue()

    @pytest.mark.asyncio
    async def test_a_forgotten_failing_task_reaches_the_errors_logger_through_the_installed_handler(self, ganchos):
        registros, _ = ganchos
        install_exception_handlers(asyncio.get_running_loop())

        async def falhar() -> None:
            raise RuntimeError("tarefa-esquecida-E5")

        tarefa = asyncio.get_running_loop().create_task(falhar())
        await asyncio.wait([tarefa])
        del tarefa
        import gc

        gc.collect()
        await asyncio.sleep(0)

        assert any(r.exc_info and "tarefa-esquecida-E5" in str(r.exc_info[1]) for r in registros)


class TestOnError:
    @pytest.mark.asyncio
    async def test_on_error_logs_at_error_with_the_event_name_and_prints_nothing(self, ganchos, capsys):
        from empiremuse.utils.client import BotCore

        registros, saida = ganchos
        bot = BotCore(
            command_prefix=commands.when_mentioned,
            intents=disnake.Intents.none(),
            pool=SimpleNamespace(config={"OWNER_IDS": ""}, loop=None, i18n=get_i18n()),
            identifier="teste",
        )
        try:
            raise RuntimeError("falha-no-evento-F6")
        except RuntimeError:
            await bot.on_error("on_message")

        (registro,) = [r for r in registros if r.levelno >= logging.ERROR]
        assert registro.exc_info[0] is RuntimeError
        assert "on_message" in registro.getMessage() + saida.getvalue()
        assert "Traceback" not in capsys.readouterr().err


def _nomes_de_erro(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name.startswith("empiremuse") and r.levelno >= logging.WARNING]


class TestErrosEsperados:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "erro",
        [
            GenericError("mensagem pro usuário"),
            commands.MissingPermissions(["manage_guild"]),
            commands.CommandOnCooldown(commands.Cooldown(1, 5.0), 3.0, commands.BucketType.user),
            commands.BadArgument("argumento inválido"),
        ],
        ids=["generic", "permissao", "cooldown", "argumento"],
    )
    async def test_an_expected_user_error_never_reaches_warning_or_above(self, handler, enviado, caplog, capsys, erro):
        with caplog.at_level(logging.DEBUG):
            await handler.process_interaction_error(_interacao(), erro)

        assert _nomes_de_erro(caplog) == []
        assert "Traceback" not in capsys.readouterr().err


class TestErrosInesperados:
    @pytest.mark.asyncio
    async def test_the_unexpected_error_logs_at_error_with_the_traceback_and_the_code_the_user_sees(
        self, handler, enviado, caplog
    ):
        with caplog.at_level(logging.DEBUG):
            await handler.process_interaction_error(_interacao(), excecao("falha-inesperada-G7", RuntimeError)[1])

        mostrado = enviado.await_args.kwargs["embed"].description
        (codigo,) = re.findall(r"ERR-[\w-]+", mostrado)
        erros = [r for r in _nomes_de_erro(caplog) if r.levelno == logging.ERROR]
        assert erros and erros[0].exc_info
        assert any(codigo in r.getMessage() for r in erros)

    @pytest.mark.asyncio
    async def test_a_repeated_unexpected_error_prints_the_traceback_only_once(self, handler, enviado):
        saida = novo_fluxo()
        configure_logging(configuracao(), [], stream=saida)
        erro = excecao("falha-repetida-H8", RuntimeError)[1]

        for _ in range(3):
            await handler.process_interaction_error(_interacao(), erro)

        assert saida.getvalue().count("Traceback (most recent call last)") == 1

    @pytest.mark.asyncio
    async def test_the_report_to_the_user_still_hides_the_detail(self, handler, enviado):
        await handler.process_interaction_error(_interacao(), excecao("detalhe-interno-I9", RuntimeError)[1])

        assert "detalhe-interno-I9" not in enviado.await_args.kwargs["embed"].description
