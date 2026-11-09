"""Item 7 (P1, Turbine) — B3 (nome do bot saneado), B5 (``extra`` vence o contexto) e B6 (``LevelGate``, V-L7).

Registros montados à mão, sem handler. O contexto vive em ``ContextVar``: cada tarefa do asyncio enxerga o seu.
"""

import asyncio
import logging

import pytest

from empiremuse.logs.context import ContextFilter, LevelGate, log_bot, log_context, log_guild
from tests.logs.apoio import registro

pytestmark = pytest.mark.critical

A, B = 111111111111111111, 222222222222222222
INFO, DEBUG, WARNING, ERROR = logging.INFO, logging.DEBUG, logging.WARNING, logging.ERROR


def contexto_de(record):
    ContextFilter().filter(record)
    return getattr(record, "bot", None), getattr(record, "guild", None)


class TestContexto:
    def test_a_record_inside_the_context_gets_the_bot_and_the_guild(self):
        with log_context(bot="Alfa", guild=A):
            assert contexto_de(registro("x")) == ("Alfa", A)

    def test_the_context_is_restored_on_exit_and_on_error(self):
        with pytest.raises(RuntimeError), log_context(bot="Alfa", guild=A):
            raise RuntimeError("falha dentro do contexto")

        assert log_bot.get() is None and log_guild.get() is None

    def test_nested_contexts_override_and_restore(self):
        with log_context(bot="Alfa", guild=A):
            with log_context(guild=B):
                assert contexto_de(registro("x"))[1] == B
            assert contexto_de(registro("x")) == ("Alfa", A)

    def test_each_asyncio_task_sees_its_own_context(self):
        vistos = {}

        async def tarefa(guild, pronto, liberar):
            with log_context(bot=f"bot{guild}", guild=guild):
                pronto.set()
                await liberar.wait()
                vistos[guild] = contexto_de(registro("x"))

        async def principal():
            liberar = asyncio.Event()
            prontos = [asyncio.Event(), asyncio.Event()]
            tarefas = [asyncio.create_task(tarefa(g, p, liberar)) for g, p in zip((A, B), prontos)]
            for pronto in prontos:
                await pronto.wait()
            liberar.set()
            await asyncio.gather(*tarefas)

        asyncio.run(principal())

        assert vistos == {A: (f"bot{A}", A), B: (f"bot{B}", B)}


class TestExtraVence:
    def test_extra_guild_wins_over_the_context(self):
        with log_context(guild=A):
            assert contexto_de(registro("x", guild=B))[1] == B

    @pytest.mark.parametrize("valor", ["123", "1\nERROR forjado", 12.5, ["1"], None])
    def test_a_guild_that_is_not_an_int_is_not_accepted(self, valor):
        guild = contexto_de(registro("x", guild=valor))[1]

        assert guild is None or isinstance(guild, int)
        assert guild != valor or valor is None


class TestNomeDoBot:
    def test_the_bot_name_is_sanitised_against_line_breaks_and_escape_sequences(self):
        with log_context(bot="ev\nil\x1b[2J‮"):
            bot = contexto_de(registro("x"))[0]

        assert bot == "ev⏎il�[2J�"


# ============================================================================ B6 — LevelGate


def com_servidor(nome, nivel, guild=None):
    record = registro("x", nivel=nivel, nome=nome)
    record.guild = guild
    return record


class TestLevelGate:
    def gate(self, nivel=INFO, guildas=(A,)):
        return LevelGate(nivel, frozenset(guildas))

    @pytest.mark.parametrize("nivel", [INFO, WARNING, ERROR, logging.CRITICAL])
    def test_records_at_or_above_the_level_always_pass(self, nivel):
        assert self.gate().filter(com_servidor("qualquer.coisa", nivel)) is True

    def test_a_debug_of_a_server_in_debug_passes(self):
        assert self.gate().filter(com_servidor("empiremuse.player", DEBUG, A)) is True

    def test_a_debug_of_another_server_does_not_pass(self):
        assert self.gate().filter(com_servidor("empiremuse.player", DEBUG, B)) is False

    @pytest.mark.parametrize(
        "nome", ["empiremuse", "empiremuse.wavelink", "empiremuse.player.fila", "empiremuse.utils.music.models"]
    )
    def test_our_own_loggers_pass_in_debug_for_a_server_in_debug(self, nome):
        assert self.gate().filter(com_servidor(nome, DEBUG, A)) is True

    @pytest.mark.parametrize(
        "nome",
        [
            "charset_normalizer",
            "requests",
            "curl_cffi",
            "dns.resolver",
            "dnspython",
            "disnake.gateway",
            "empiremusex",
            "empiremuse_extra.x",
        ],
    )
    def test_third_party_debug_never_passes_even_inside_a_server_in_debug(self, nome):
        assert self.gate().filter(com_servidor(nome, DEBUG, A)) is False

    def test_the_gate_does_not_format_the_arguments(self):
        chamadas = []

        class Espia:
            def __str__(self):
                chamadas.append(1)
                return "espia"

        record = registro("x %s", Espia(), nivel=DEBUG, nome="empiremuse.player")
        record.guild = B

        self.gate().filter(record)

        assert chamadas == []


class TestLevelGateAuditoria:
    """Linha de auditoria (``extra={"empiremuse_emit": True}``) passa mesmo com ``LOG_LEVEL=ERROR``; o resto abaixo do nível não."""

    def test_a_marked_record_passes_below_the_level_and_an_unmarked_one_does_not(self):
        gate = LevelGate(ERROR, frozenset())

        assert gate.filter(registro("auditoria", nivel=INFO, empiremuse_emit=True)) is True
        assert gate.filter(registro("rotina", nivel=INFO)) is False
        assert gate.filter(registro("rotina", nivel=INFO, empiremuse_emit=False)) is False
        assert gate.filter(registro("rotina", nivel=INFO, empiremuse_emit="yes")) is False  # só o True literal vale

    def test_end_to_end_at_error_level_the_audit_line_is_written_each_time_and_routine_info_is_not(self):
        import io

        from empiremuse.logs.setup import configure_logging
        from empiremuse.logs.settings import LogSettings
        from tests.logs.apoio import Relogio

        saida = io.StringIO()
        configure_logging(LogSettings(ERROR, False, False, frozenset(), 300), [], stream=saida, clock=Relogio())
        log = logging.getLogger("empiremuse.auditoria_teste")

        for _ in range(3):
            log.info("linha de auditoria", extra={"empiremuse_emit": True})
        log.info("rotina comum")

        texto = saida.getvalue()
        assert texto.count("linha de auditoria") == 3 and "rotina comum" not in texto
