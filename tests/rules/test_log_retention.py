"""``rules/log_retention``: idade do arquivo de log pelo NOME e leitura de ``LOG_RETENTION_DAYS`` (T16 puro, T18)."""

from datetime import date, timedelta

import pytest

from empiremuse.rules.log_retention import is_expired, log_file_date, log_file_name, parse_retention_days

HOJE = date(2026, 11, 9)


def nome_com_idade(dias: int, prefixo: str = "disnake") -> str:
    return log_file_name(prefixo, HOJE - timedelta(days=dias))


@pytest.mark.critical
@pytest.mark.parametrize(
    ("dias", "vencido"), [(184, True), (183, False), (182, False), (0, False), (-3, False), (2000, True)]
)
def test_a_idade_vem_do_nome_e_so_acima_do_limite_vence(dias: int, vencido: bool) -> None:
    assert is_expired(nome_com_idade(dias), HOJE, 183) is vencido
    assert is_expired(nome_com_idade(dias, "access"), HOJE, 183) is vencido


@pytest.mark.critical
@pytest.mark.parametrize(
    "nome",
    [
        "disnake.log",
        "disnake-2026-01-01.log.1",
        "disnake-2026-01-01.log.gz",
        "x-disnake-2026-01-01.log",
        "disnake-2026-01-01.logx",
        "other-2026-01-01.log",
        "disnake-2026-1-1.log",
        "disnake-2026-13-45.log",
        "disnake-٢٠٢٦-٠١-٠١.log",
        "disnake-2026-01-01.log\n",
        "../disnake-2026-01-01.log",
        "",
    ],
)
def test_nome_fora_do_padrao_exato_nunca_vence(nome: str) -> None:
    assert log_file_date(nome) is None
    assert is_expired(nome, HOJE, 30) is False


@pytest.mark.parametrize(
    ("bruto", "dias", "avisa"),
    [
        (None, 183, False),
        ("", 183, False),
        ("  ", 183, False),
        (183, 183, False),
        ("30", 30, False),
        (" 730 ", 730, False),
        ("abc", 183, True),
        ("1.5", 183, True),
        (True, 183, True),
        (29, 30, True),
        (-5, 30, True),
        (731, 730, True),
        (10**9, 730, True),
    ],
)
def test_parse_retention_days(bruto: object, dias: int, avisa: bool) -> None:
    resultado, aviso = parse_retention_days(bruto)

    assert resultado == dias
    assert (aviso is not None) is avisa
