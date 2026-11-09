"""F3-6 — código curto de erro para cruzar o relato do usuário com o log.

* ``empiremuse.rules.error_code.new_error_code()`` devolve ``ERR-`` + 6 hexadecimais em maiúsculas, aleatório de
  ``secrets`` (não deriva de ID, token nem hora);
* só o erro INESPERADO ganha código; o MESMO código aparece para o usuário (slash e prefixo, com e sem ``embed_links``),
  na mensagem do log e no webhook (campo do embed e nome do anexo);
* o erro esperado (``GenericError``, permissões) não ganha código nem vai para o log de erro;
* falhar ao gerar o código nunca causa um novo erro: a mensagem genérica sai como antes.

Sem rede, sem Discord e sem ``.env``; o aleatório é fixado trocando ``secrets.token_hex``.
"""

import logging
import secrets
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from empiremuse.cogs import error_handler
from empiremuse.cogs.error_handler import ErrorHandler

TOKEN_FALSO = "MTIzNDU2Nzg5MDEyMzQ1Njc4OTAx.GabcDE.abcdefghijklmnopqrstuvwxyz1"


def falhar_ao_gerar(monkeypatch):
    def quebra(nbytes=None):
        raise RuntimeError("sem entropia (simulado)")

    monkeypatch.setattr(secrets, "token_hex", quebra)


@pytest.fixture
def handler(monkeypatch) -> ErrorHandler:
    h = ErrorHandler.__new__(ErrorHandler)
    h.bot = SimpleNamespace(
        config={"AUTO_ERROR_REPORT_WEBHOOK": "", "ERROR_REPORT_WEBHOOK": "", "SUPPORT_SERVER": ""},
        http=SimpleNamespace(token=TOKEN_FALSO),
        pool=SimpleNamespace(shutdown=AsyncMock()),
        user=SimpleNamespace(mention="<@1>"),
    )
    h.components = []
    h.webhook_max_concurrency = SimpleNamespace(acquire=AsyncMock(), release=AsyncMock())
    h.send_webhook = AsyncMock()
    monkeypatch.setattr(error_handler.asyncio, "sleep", AsyncMock())
    return h


@pytest.fixture
def enviado(monkeypatch) -> AsyncMock:
    enviar = AsyncMock()
    monkeypatch.setattr(error_handler, "send_message", enviar)
    return enviar


@pytest.fixture
def anexos(monkeypatch) -> list[tuple[str, str]]:
    """``(texto, nome)`` de cada anexo que iria para o webhook."""
    guardados: list[tuple[str, str]] = []

    def guardar(texto, nome):
        guardados.append((texto, nome))
        return f"arquivo:{nome}"

    monkeypatch.setattr(error_handler, "string_to_file", guardar)
    return guardados


class TestFuncaoPura:
    @pytest.mark.smoke
    @pytest.mark.parametrize(
        ("texto", "vale"),
        [
            ("ERR-7F3A2C", True),
            ("7F3A2C", False),
        ],
    )
    def test_error_code_re(self, texto, vale):
        from empiremuse.rules.error_code import ERROR_CODE_RE

        assert bool(ERROR_CODE_RE.fullmatch(texto)) is vale


class TestFalhaAoGerarOCodigo:
    def test_safe_error_code_devolve_vazio_e_registra_so_o_tipo_do_erro(self, monkeypatch, caplog):
        from empiremuse.rules.error_code import safe_error_code

        falhar_ao_gerar(monkeypatch)
        with caplog.at_level(logging.WARNING, logger="empiremuse.rules.error_code"):
            codigo = safe_error_code()
        assert codigo == ""
        assert "RuntimeError" in caplog.text and "sem entropia" not in caplog.text
