"""``configure_logging`` com o arquivo diário: poda na subida, suspensão legal e o gancho ``tick_logging`` (T16 ponta a ponta)."""

import io
import logging
from datetime import UTC, datetime, timedelta

import pytest

from empiremuse.logs.setup import configure_logging, tick_logging
from empiremuse.logs.settings import LogSettings, parse_log_settings
from tests.logs.apoio import Relogio

pytestmark = pytest.mark.critical

AGORA = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


class RelogioUtc:
    def __init__(self) -> None:
        self.agora = AGORA

    def __call__(self) -> datetime:
        return self.agora


def nome(dias_atras: int) -> str:
    return f"disnake-{(AGORA - timedelta(days=dias_atras)).date().isoformat()}.log"


def instalar(tmp_path, *, hold=False, dias=183, relogio=None):
    console = io.StringIO()
    relogio = relogio or RelogioUtc()
    settings = LogSettings(logging.INFO, False, True, frozenset(), 300, retention_days=dias, retention_hold=hold)
    configure_logging(settings, [], stream=console, logs_dir=tmp_path / ".logs", clock=Relogio(), now=relogio)
    return console, relogio


def semear(tmp_path, *dias_atras: int) -> None:
    pasta = tmp_path / ".logs"
    pasta.mkdir(exist_ok=True)
    for dias in dias_atras:
        (pasta / nome(dias)).write_text("antigo\n", encoding="utf-8")


def presentes(tmp_path) -> set[str]:
    return {p.name for p in (tmp_path / ".logs").iterdir()}


def test_a_subida_poda_o_vencido_e_poe_o_arquivo_de_hoje(tmp_path):
    semear(tmp_path, 184, 182)

    instalar(tmp_path)

    assert presentes(tmp_path) == {nome(182), nome(0)}


def test_a_retencao_configurada_vale(tmp_path):
    semear(tmp_path, 40, 20)

    instalar(tmp_path, dias=30)

    assert presentes(tmp_path) == {nome(20), nome(0)}


def test_suspensao_legal_nao_apaga_e_avisa_na_subida(tmp_path):
    semear(tmp_path, 184, 1000)

    console, _ = instalar(tmp_path, hold=True)

    assert {nome(184), nome(1000)} <= presentes(tmp_path)
    assert "LOG_RETENTION_HOLD" in console.getvalue()


def test_o_gancho_do_tick_poda_depois_de_uma_hora_e_nao_antes(tmp_path):
    _, relogio = instalar(tmp_path)
    semear(tmp_path, 200)

    tick_logging()
    assert nome(200) in presentes(tmp_path)

    relogio.agora += timedelta(hours=1)
    tick_logging()
    assert nome(200) not in presentes(tmp_path)


def test_falha_na_poda_avisa_sem_derrubar_nem_citar_caminho(tmp_path, monkeypatch):
    from empiremuse.utils import file_log

    semear(tmp_path, 300)
    monkeypatch.setattr(file_log.os, "unlink", lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "negado")))

    console, _ = instalar(tmp_path)

    texto = console.getvalue()
    assert "não puderam ser apagados" in texto and nome(300) not in texto and str(tmp_path) not in texto


def test_sem_arquivo_ligado_o_tick_segue_sem_erro(tmp_path):
    configure_logging(LogSettings(logging.INFO, False, False, frozenset(), 300), [], stream=io.StringIO())

    tick_logging()


def test_parse_leva_a_retencao_ate_o_handler():
    settings, avisos = parse_log_settings({"LOG_RETENTION_DAYS": "400", "LOG_RETENTION_HOLD": "true"}, {})

    assert (settings.retention_days, settings.retention_hold, avisos) == (400, True, [])
