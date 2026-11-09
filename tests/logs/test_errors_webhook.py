# ruff: noqa: F811
"""Item 7, P4 — B22 (V-L1, [!]2 aprovado): o traceback mandado ao webhook de erro passa pelo saneamento e pelo redator único.

O arquivo do webhook (``string_to_file``) não pode conter a senha de um nó, a URI do Mongo nem o token de OUTRO bot do pool; ESC é saneado.
Desde o E4a o anexo leva só o tipo da exceção e o código (``tests/logs/test_webhook_minimo.py``); estes testes seguem como
guarda: mesmo que o conteúdo do anexo volte a crescer, nenhum segredo pode estar nele.
"""

import pytest

from empiremuse.logs.redact import register_secret
from tests.cogs.test_error_handler import _Contexto, _interacao, arquivos_do_webhook, enviado, handler  # noqa: F401
from tests.logs.apoio import token_discord

pytestmark = pytest.mark.critical

SENHA_DO_NO = "".join(["senha-do-no-", "webhook-P4-K1"])
URI_DO_MONGO = "".join(["mongodb+srv://", "usuario:", "senha-mongo-P4-L2", "@cluster.exemplo.invalido/banco"])
TOKEN_DE_OUTRO_BOT = token_discord("p")
ESC = "\x1b"


@pytest.fixture
def segredos_do_pool():
    for valor in (SENHA_DO_NO, URI_DO_MONGO, TOKEN_DE_OUTRO_BOT):
        register_secret(valor)


def erro_com_segredos() -> RuntimeError:
    try:
        raise RuntimeError(
            f"no {SENHA_DO_NO} falhou; db {URI_DO_MONGO}; outro bot {TOKEN_DE_OUTRO_BOT}; cor {ESC}[31mvermelha{ESC}[0m"
        )
    except RuntimeError as e:
        return e


def proibidos_no_arquivo(texto: str) -> list[str]:
    return [p for p in (SENHA_DO_NO, "senha-mongo-P4-L2", TOKEN_DE_OUTRO_BOT, ESC) if p in texto]


class TestWebhookDeErro:
    @pytest.mark.asyncio
    async def test_the_interaction_report_has_no_node_password_mongo_uri_other_bot_token_or_escape(
        self, handler, enviado, arquivos_do_webhook, segredos_do_pool
    ):
        handler.bot.config["AUTO_ERROR_REPORT_WEBHOOK"] = "https://discord.com/api/webhooks/1/segredo"

        await handler.process_interaction_error(_interacao(), erro_com_segredos())

        handler.send_webhook.assert_awaited_once()
        relatorio = "\n".join(arquivos_do_webhook)
        assert "RuntimeError" in relatorio, "o relatório precisa continuar útil (tipo da exceção)"
        assert proibidos_no_arquivo(relatorio) == []

    @pytest.mark.asyncio
    async def test_the_url_userinfo_of_the_mongo_uri_is_gone_even_if_it_was_never_registered(
        self, handler, enviado, arquivos_do_webhook
    ):
        handler.bot.config["AUTO_ERROR_REPORT_WEBHOOK"] = "https://discord.com/api/webhooks/1/segredo"
        try:
            raise RuntimeError("falha em mongodb://dono:senha-nao-registrada-M3@host.exemplo.invalido/db")
        except RuntimeError as e:
            erro = e

        await handler.process_interaction_error(_interacao(), erro)

        assert "senha-nao-registrada-M3" not in "\n".join(arquivos_do_webhook)

    @pytest.mark.asyncio
    async def test_this_bots_own_token_is_still_masked(self, handler, enviado, arquivos_do_webhook):
        from tests.cogs.test_error_handler import TOKEN_FALSO

        handler.bot.config["AUTO_ERROR_REPORT_WEBHOOK"] = "https://discord.com/api/webhooks/1/segredo"
        try:
            raise RuntimeError(f"token {TOKEN_FALSO}")
        except RuntimeError as e:
            erro = e

        await handler.process_interaction_error(_interacao(), erro)

        assert TOKEN_FALSO not in "\n".join(arquivos_do_webhook)
