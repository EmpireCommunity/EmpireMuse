"""Item 7 (P1, Turbine) — B1: ``parse_log_settings`` é pura (config e ``environ`` injetados) e devolve os avisos sem logar.

Os valores de config chegam como o ``config_loader`` os entrega: textos para ``LOG_*``, ``bool`` para ``ENABLE_LOGGER`` e ``int`` para
``METRICS_INTERVAL``. Chaves ausentes valem o padrão.
"""

import dataclasses
import logging

import pytest

from empiremuse.logs.settings import LogSettings, parse_log_settings

pytestmark = pytest.mark.critical

IMAGEM = {"EMPIREMUSE_VERSION": "2.1.0"}
G1, G2 = "123456789012345678", "987654321098765432"


def ler(config=None, ambiente=None):
    return parse_log_settings(config or {}, ambiente if ambiente is not None else {})


class TestPadroes:
    def test_defaults_outside_the_image(self):
        settings, avisos = ler()

        assert settings == LogSettings(
            level=logging.INFO, color=True, file=True, debug_guilds=frozenset(), metrics_interval=300
        )
        assert avisos == []

    def test_defaults_inside_the_image_turn_the_file_off(self):
        settings, avisos = ler(ambiente=IMAGEM)

        assert settings.file is False and settings.color is True and avisos == []


class TestNivel:
    @pytest.mark.parametrize(
        ("valor", "esperado"),
        [
            ("DEBUG", logging.DEBUG),
            ("debug", logging.DEBUG),
            ("INFO", logging.INFO),
            ("Info", logging.INFO),
            ("warning", logging.WARNING),
            ("WARNING", logging.WARNING),
            ("error", logging.ERROR),
            ("ERROR", logging.ERROR),
        ],
    )
    def test_valid_levels_ignore_case(self, valor, esperado):
        settings, avisos = ler({"LOG_LEVEL": valor})

        assert settings.level == esperado and avisos == []

    @pytest.mark.parametrize("valor", ["verbose", "CRITICAL", "10", "info!", "trace"])
    def test_an_invalid_level_falls_back_to_info_with_one_warning(self, valor):
        settings, avisos = ler({"LOG_LEVEL": valor})

        assert settings.level == logging.INFO
        assert len(avisos) == 1 and "LOG_LEVEL" in avisos[0]


class TestCor:
    @pytest.mark.parametrize(("valor", "esperado"), [("on", True), ("ON", True), ("off", False), ("OFF", False)])
    def test_log_color(self, valor, esperado):
        assert ler({"LOG_COLOR": valor})[0].color is esperado

    def test_no_color_set_turns_color_off_even_with_log_color_on(self):
        assert ler({"LOG_COLOR": "on"}, {"NO_COLOR": "1"})[0].color is False


class TestArquivo:
    def test_auto_outside_the_image_is_on(self):
        assert ler({"LOG_FILE": "auto"})[0].file is True

    def test_auto_inside_the_image_is_off(self):
        assert ler({"LOG_FILE": "auto"}, IMAGEM)[0].file is False

    @pytest.mark.parametrize("ambiente", [{}, IMAGEM], ids=["fora", "dentro"])
    def test_on_and_off_are_explicit(self, ambiente):
        assert ler({"LOG_FILE": "on"}, ambiente)[0].file is True
        assert ler({"LOG_FILE": "off"}, ambiente)[0].file is False

    @pytest.mark.parametrize("log_file", [None, "auto"], ids=["ausente", "auto"])
    def test_the_legacy_enable_logger_equals_on_when_log_file_is_missing_or_auto(self, log_file):
        config = {"ENABLE_LOGGER": True} | ({"LOG_FILE": log_file} if log_file else {})

        assert ler(config, IMAGEM)[0].file is True


class TestServidoresEmDebug:
    def test_valid_ids_become_a_frozenset_of_ints(self):
        settings, avisos = ler({"LOG_DEBUG_GUILDS": f"{G1}, {G2}"})

        assert settings.debug_guilds == frozenset({int(G1), int(G2)}) and avisos == []

    def test_garbage_is_ignored_with_a_warning_that_does_not_echo_it(self):
        settings, avisos = ler({"LOG_DEBUG_GUILDS": f"abc-secreto,{G1}"})

        assert settings.debug_guilds == frozenset({int(G1)})
        assert len(avisos) == 1 and "abc-secreto" not in avisos[0]

    def test_fourteen_digits_are_too_short(self):
        curto = "1" * 14

        settings, avisos = ler({"LOG_DEBUG_GUILDS": f"{curto},{G1}"})

        assert settings.debug_guilds == frozenset({int(G1)}) and len(avisos) == 1 and curto not in avisos[0]

    def test_twenty_two_digits_are_too_long(self):
        longo = "1" * 22

        settings, avisos = ler({"LOG_DEBUG_GUILDS": f"{longo},{G1}"})

        assert settings.debug_guilds == frozenset({int(G1)}) and len(avisos) == 1 and longo not in avisos[0]

    @pytest.mark.parametrize("digitos", [15, 21])
    def test_the_boundaries_fifteen_and_twenty_one_digits_are_valid(self, digitos):
        valor = "1" * digitos

        settings, avisos = ler({"LOG_DEBUG_GUILDS": valor})

        assert settings.debug_guilds == frozenset({int(valor)}) and avisos == []

    def test_sixty_ids_keep_only_the_first_fifty_with_a_warning(self):
        ids = [str(10**16 + n) for n in range(60)]

        settings, avisos = ler({"LOG_DEBUG_GUILDS": ",".join(ids)})

        assert settings.debug_guilds == frozenset(int(i) for i in ids[:50])
        assert len(avisos) == 1 and not any(i in avisos[0] for i in ids)

    def test_non_ascii_digits_are_not_snowflakes(self):
        settings, avisos = ler({"LOG_DEBUG_GUILDS": "١٢٣٤٥٦٧٨٩٠١٢٣٤٥٦"})

        assert settings.debug_guilds == frozenset() and len(avisos) == 1


class TestIntervaloDasMetricas:
    @pytest.mark.parametrize(
        ("valor", "esperado"), [(0, 0), (1, 60), (30, 60), (59, 60), (60, 60), (300, 300), (3600, 3600)]
    )
    def test_metrics_interval(self, valor, esperado):
        settings, avisos = ler({"METRICS_INTERVAL": valor})

        assert settings.metrics_interval == esperado and avisos == []


class TestRetencao:
    """T18: ``LOG_RETENTION_DAYS`` (padrão 183, limites 30-730) e ``LOG_RETENTION_HOLD``."""

    def test_defaults(self):
        settings, avisos = ler()

        assert (settings.retention_days, settings.retention_hold) == (183, False) and avisos == []

    @pytest.mark.parametrize(
        ("valor", "dias", "avisa"),
        [
            (183, 183, False),
            ("365", 365, False),
            ("30", 30, False),
            ("730", 730, False),
            ("abc", 183, True),
            (5, 30, True),
            (900, 730, True),
        ],
    )
    def test_dias_com_limites_e_aviso(self, valor, dias, avisa):
        settings, avisos = ler({"LOG_RETENTION_DAYS": valor})

        assert settings.retention_days == dias and bool(avisos) is avisa

    @pytest.mark.parametrize(
        ("valor", "esperado"),
        [("true", True), ("on", True), (True, True), ("false", False), ("", False), (None, False)],
    )
    def test_suspensao_legal(self, valor, esperado):
        assert ler({"LOG_RETENTION_HOLD": valor})[0].retention_hold is esperado
