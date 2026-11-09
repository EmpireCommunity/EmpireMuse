"""Item 7 (P1) — achados Low/Info do Vigil: V-P1-7 (surrogate solto no arquivo), V-P1-8 (tokens do OAuth do YouTube) e o nome do logger no cabeçalho.

``configure_logging`` real (console em ``StringIO``, arquivo em ``tmp_path``); o ``oauth_command`` roda com a view e a sessão falsas.
"""

import asyncio
import io
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.cogs import ll_yt_oauth
from empiremuse.logs.console import ConsoleFormatter, FileFormatter
from empiremuse.logs.redact import REDACTED, redact
from empiremuse.logs.setup import configure_logging
from empiremuse.logs.settings import LogSettings
from tests.logs.apoio import agora_utc, arquivo_do_log, registro

pytestmark = pytest.mark.critical

SURROGATE = "\ud800"


@pytest.fixture
def destinos(tmp_path):
    console = io.StringIO()
    configure_logging(
        LogSettings(logging.INFO, False, True, frozenset(), 300),
        [],
        stream=console,
        logs_dir=tmp_path / ".logs",
        now=agora_utc,
    )
    return SimpleNamespace(console=console, arquivo=arquivo_do_log(tmp_path / ".logs"))


def ler_arquivo(destinos) -> str:
    return destinos.arquivo.read_bytes().decode("utf-8")  # estrito: o que está em disco tem de ser UTF-8 válido


# ============================================================================ V-P1-7 — surrogate solto


class TestSurrogateSolto:
    def test_a_lone_surrogate_in_the_message_reaches_the_file_escaped_and_the_console(self, destinos, capsys):
        logging.getLogger("empiremuse.teste").warning("título " + SURROGATE + " fim")

        err = capsys.readouterr().err
        assert "[log] falha ao emitir um registro" not in err and "UnicodeEncodeError" not in err
        arquivo = ler_arquivo(destinos)
        assert "título" in arquivo and "fim" in arquivo and "\\ud800" in arquivo and SURROGATE not in arquivo
        assert "título" in destinos.console.getvalue() and "fim" in destinos.console.getvalue()


# ============================================================================ V-P1-8 — tokens do OAuth do YouTube


class ViewFalsa:
    """Substituta do ``YtOauthView``: já "autenticada", com os tokens obtidos em tempo de execução."""

    dados: dict = {}

    def __init__(self, **kwargs):
        self.interaction = None
        self.exception_txt = None
        self.data = dict(self.dados)

    async def wait(self):
        return None


def cog_com_sessao_que_para():
    class Sessao:
        def get(self, *args, **kwargs):
            raise RuntimeError("userinfo parado")

    cog = ll_yt_oauth.YtOauthLL.__new__(ll_yt_oauth.YtOauthLL)
    cog.bot = SimpleNamespace(get_color=lambda *a: 0, session=Sessao())
    ctx = SimpleNamespace(
        guild=SimpleNamespace(me=None), send=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock()))
    )
    return cog, ctx


class TestTokensDoOauth:
    def test_the_refresh_and_access_tokens_are_registered_as_soon_as_they_are_obtained(self, monkeypatch):
        refresh, access = "refresh-oauth-runtime-P1x9", "access-oauth-runtime-Q2y8"
        ViewFalsa.dados = {"refresh_token": refresh, "access_token": access}
        monkeypatch.setattr(ll_yt_oauth, "YtOauthView", ViewFalsa)
        cog, ctx = cog_com_sessao_que_para()
        assert redact(f"x {refresh}") == f"x {refresh}"

        with pytest.raises(RuntimeError, match="userinfo parado"):
            asyncio.run(cog.oauth_command(ctx))

        assert redact(f"x {refresh}") == f"x {REDACTED}" and redact(f"x {access}") == f"x {REDACTED}"

    def test_the_url_encoded_forms_are_masked_too(self, monkeypatch):
        from urllib.parse import quote

        refresh = "1//0g refresh/com+especiais=R3z"
        ViewFalsa.dados = {"refresh_token": refresh, "access_token": "access-com-formas-S4w7"}
        monkeypatch.setattr(ll_yt_oauth, "YtOauthView", ViewFalsa)
        cog, ctx = cog_com_sessao_que_para()

        with pytest.raises(RuntimeError):
            asyncio.run(cog.oauth_command(ctx))

        assert refresh not in redact(refresh) and quote(refresh, safe="") not in redact(quote(refresh, safe=""))


# ============================================================================ nome do logger no cabeçalho

NOME_FORJADO = "x\nERROR forjado"


class TestNomeDoLogger:
    @pytest.mark.parametrize(
        "formatador",
        [lambda: ConsoleFormatter(False), lambda: ConsoleFormatter(True), FileFormatter],
        ids=["console", "console-cor", "arquivo"],
    )
    def test_a_line_break_in_the_logger_name_cannot_forge_a_line(self, formatador):
        saida = formatador().format(registro("mensagem", nome=NOME_FORJADO))

        assert saida.count("\n") == 0 and "x⏎ERROR forjado: mensagem" in saida

    def test_end_to_end_a_forged_logger_name_gives_one_header_in_each_destination(self, destinos):
        logging.getLogger(NOME_FORJADO).warning("do logger forjado")

        for texto in (destinos.console.getvalue(), ler_arquivo(destinos)):
            assert len(texto.splitlines()) == 1 and "x⏎ERROR forjado" in texto
