"""F3-2 — erros inesperados chegam ao usuário sem detalhe interno (regra 4 de .claude/rules/empiremuse/security.md).

Com a falha de gravação do LocalDatabase propagando (KI-10), um OSError cai no tratador global. O usuário vê uma mensagem
genérica em pt-BR (sem repr, caminho ou traceback); o detalhe completo vai para o log do servidor (logger.error com
exc_info) e para o webhook de erro, sem o token do bot. GenericError e as mensagens próprias de ``parse_error`` não mudam.
Sem rede, sem Discord e sem .env real.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from empiremuse.cogs import error_handler
from empiremuse.cogs.error_handler import ErrorHandler
from empiremuse.i18n.service import get_i18n

TOKEN_FALSO = "MTIzNDU2Nzg5MDEyMzQ1Njc4OTAx.GabcDE.abcdefghijklmnopqrstuvwxyz1"
CAMINHO = "/data/local_database/guilds.json"
LOGGER = "empiremuse.cogs.error_handler"


def _erro_de_disco() -> OSError:
    try:
        raise OSError(28, f"No space left on device: '{CAMINHO}' (token {TOKEN_FALSO})")
    except OSError as e:
        return e


def _proibidos_na_tela(texto: str) -> list[str]:
    return [
        p
        for p in ("OSError", "No space left", CAMINHO, "local_database", "Traceback", "```py", TOKEN_FALSO)
        if p in texto
    ]


@pytest.fixture
def handler(monkeypatch) -> ErrorHandler:
    h = ErrorHandler.__new__(ErrorHandler)
    h.bot = SimpleNamespace(
        config={"AUTO_ERROR_REPORT_WEBHOOK": "", "ERROR_REPORT_WEBHOOK": "", "SUPPORT_SERVER": ""},
        http=SimpleNamespace(token=TOKEN_FALSO),
        pool=SimpleNamespace(shutdown=AsyncMock(), i18n=get_i18n()),
        user=SimpleNamespace(mention="<@1>"),
    )
    h.components = []
    h.webhook_max_concurrency = SimpleNamespace(acquire=AsyncMock(), release=AsyncMock())
    h.send_webhook = AsyncMock()
    monkeypatch.setattr(error_handler.asyncio, "sleep", AsyncMock())
    return h


@pytest.fixture
def enviado(monkeypatch) -> AsyncMock:
    """send_message (caminho de interação) dublado: guarda o que seria mostrado ao usuário."""
    enviar = AsyncMock()
    monkeypatch.setattr(error_handler, "send_message", enviar)
    return enviar


@pytest.fixture
def arquivos_do_webhook(monkeypatch) -> list[str]:
    textos: list[str] = []

    def guardar(texto, nome):
        textos.append(texto)
        return f"arquivo:{nome}"

    monkeypatch.setattr(error_handler, "string_to_file", guardar)
    return textos


def _interacao() -> MagicMock:
    inter = MagicMock()
    inter.message.author.bot = False
    inter.data.name = "play"
    return inter


class _Contexto:
    """Contexto de prefixo mínimo (sem ``inter``/``store_message``: a resposta sai por ``ctx.send``)."""

    def __init__(self, embed_links: bool):
        self.bot = None
        self.author = SimpleNamespace(bot=False, id=7, mention="<@7>")
        self.prefix = "!"
        self.cog = None
        self.command = SimpleNamespace(name="play", usage=None, full_parent_name="")
        self.channel = MagicMock()
        self.channel.permissions_for.return_value.embed_links = embed_links
        self.guild = SimpleNamespace(me=object(), id=1)
        self.message = SimpleNamespace(content="!play x", delete=AsyncMock())
        self.send = AsyncMock()


def _texto_da_interacao(enviar: AsyncMock) -> str:
    kwargs = enviar.await_args.kwargs
    partes = [e.description or "" for e in kwargs.get("embeds", [])]
    if "embed" in kwargs:
        partes.append(kwargs["embed"].description or "")
    partes.append(kwargs.get("text", "") or "")
    return "\n".join(partes)


class TestInteracao:
    @pytest.mark.smoke
    @pytest.mark.asyncio
    async def test_qualquer_excecao_inesperada_tem_o_mesmo_tratamento(self, handler, enviado):
        await handler.process_interaction_error(_interacao(), KeyError(CAMINHO))

        assert not _proibidos_na_tela(_texto_da_interacao(enviado))

    @pytest.mark.asyncio
    async def test_o_detalhe_completo_vai_para_o_log_do_servidor(self, handler, enviado, caplog):
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            await handler.process_interaction_error(_interacao(), _erro_de_disco())

        registros = [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.ERROR]
        assert registros, "o erro inesperado não foi registrado no log do servidor"
        tipo, valor, _ = registros[0].exc_info
        assert tipo is OSError and CAMINHO in str(valor), "o log precisa levar o exc_info com o detalhe completo"
        assert TOKEN_FALSO not in registros[0].getMessage()


class TestComandoDePrefixo:
    @pytest.mark.asyncio
    async def test_no_report_do_webhook_so_o_tipo_e_o_codigo_saem_do_host(self, handler, arquivos_do_webhook):
        handler.bot.config["AUTO_ERROR_REPORT_WEBHOOK"] = "https://discord.com/api/webhooks/1/segredo"

        await handler.on_legacy_command_error(_Contexto(True), _erro_de_disco())

        handler.send_webhook.assert_awaited_once()
        relatorio = "\n".join(arquivos_do_webhook)
        assert "OSError" in relatorio and CAMINHO not in relatorio and TOKEN_FALSO not in relatorio
