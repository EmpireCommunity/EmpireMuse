# ruff: noqa: F811
"""Item 7, P4 (Vigil, seguimento do F1 do CodeRabbit #133) — o embed do relatório de erro: ordem do saneamento, linhas, limites do Discord e falha do webhook.

R1 o redator roda ANTES de qualquer mapeamento de caracteres (``fix_characters`` troca ``_`` ``*`` ``[``: um segredo com esses caracteres escaparia);
R2 quebra de linha no nome vira ``⏎`` e o bloco de código mantém exatamente duas linhas (nome e ID); R3 o embed respeita os limites do Discord
(nome 100, campo 1024, rodapé 2048, descrição 4096, total 6000) com tudo hostil ao mesmo tempo; R4 o webhook que falha libera o limite por servidor;
R5 o webhook não menciona ninguém.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest
from disnake.ext import commands

from empiremuse.cogs import error_handler
from empiremuse.cogs.error_handler import ErrorHandler
from empiremuse.logs.redact import register_secret
from tests.cogs.test_error_handler import _interacao, handler
from tests.logs.test_errors_relatorio import autor, canal_de_voz, prefixo, servidor_do_relatorio, tratador
from tests.logs.test_errors_relatorio_nomes import autor_com_nome

pytestmark = pytest.mark.critical

SEGREDO_COM_SIMBOLOS = "".join(["pre_fixo*seg[redo]", "_com_simbolos-R1x"])
LIMITE_CAMPO, LIMITE_RODAPE, LIMITE_DESCRICAO, LIMITE_TOTAL, LIMITE_NOME = 1024, 2048, 4096, 6000, 100


def contexto_com_nome(nome: str, *, content: str = "!play x"):
    """Prefixo com o MESMO nome no servidor, no canal, nos dois canais de voz e no autor."""
    ctx = prefixo(content)
    ctx.guild = servidor_do_relatorio(nome, voz_do_bot=SimpleNamespace(channel=canal_de_voz(nome, 73)))
    ctx.channel = SimpleNamespace(name=nome, id=60)
    ctx.author = autor_com_nome(nome)
    ctx.author.voice = SimpleNamespace(channel=canal_de_voz(nome, 71))
    return ctx


def tudo_do_embed(embed: disnake.Embed) -> str:
    return "\n".join(
        [embed.title or "", embed.description or "", embed.footer.text or ""] + [f.value for f in embed.fields]
    )


def blocos_de_nome(embed: disnake.Embed) -> list[str]:
    """Os campos de servidor/canais (``\\`\\`\\`\\n<nome>\\nID: n\\`\\`\\```)."""
    return [f.value for f in embed.fields if f.name.startswith(("Servidor", "Canal"))]


class TestRedatorAntesDosMapeamentos:
    def test_a_secret_with_underscore_asterisk_and_bracket_is_absent_everywhere(self):
        register_secret(SEGREDO_COM_SIMBOLOS)

        embed = tratador().build_report_embed(contexto_com_nome(f"loja {SEGREDO_COM_SIMBOLOS} fim"))

        texto = tudo_do_embed(embed)
        # o mapeamento troca _ * [ ], então o segredo "cru" nunca aparece; o que vaza é o miolo: confere os pedaços alfanuméricos
        for pedaco in ("fixo", "redo", "simbolos", "R1x"):
            assert pedaco not in texto, f"pedaço do segredo vazou: {pedaco}"


class TestQuebrasDeLinhaNoNome:
    @pytest.mark.parametrize("quebra", ["\n", "\r", "\r\n"])
    def test_a_line_break_in_the_name_cannot_forge_an_id_line(self, quebra):
        embed = tratador().build_report_embed(contexto_com_nome(f"x{quebra}ID: 123"))

        for valor in blocos_de_nome(embed):
            interior = valor.removeprefix("```\n").removesuffix("```")
            assert len(interior.split("\n")) == 1, valor  # só o nome: sem linha de ID, e a quebra do nome virou ⏎
            assert "\r" not in valor and "⏎" in valor


class TestLimitesDoDiscord:
    def embed_hostil(self, *, content: str = "!play " + "x" * 4000) -> disnake.Embed:
        register_secret(SEGREDO_COM_SIMBOLOS)
        nome = f"{SEGREDO_COM_SIMBOLOS}\n\x1b[31m" + "n" * 30_000
        tratado = tratador()
        embed = tratado.build_report_embed(contexto_com_nome(nome, content=content))
        embed.add_field(name="Código do erro:", value="`a1b2c3`", inline=False)
        return embed

    def test_each_field_footer_description_and_total_are_within_the_limits(self):
        embed = self.embed_hostil()

        assert all(len(f.value) <= LIMITE_CAMPO for f in embed.fields)
        assert embed.footer.text is None  # sem rodapé de autor
        assert len(embed.description or "") <= LIMITE_DESCRICAO
        assert len(embed) <= LIMITE_TOTAL


class FalhaNoWebhook(Exception):
    pass


class TestFalhaNaoTravaOServidor:
    def preparar(self, h: ErrorHandler) -> None:
        h.webhook_max_concurrency = commands.MaxConcurrency(1, per=commands.BucketType.guild, wait=True)
        h.bot.config["AUTO_ERROR_REPORT_WEBHOOK"] = "https://discord.com/api/webhooks/1/x"
        h.build_report_embed = MagicMock(return_value=disnake.Embed(title="x"))

    @pytest.mark.asyncio
    async def test_a_failing_webhook_on_an_interaction_error_releases_the_guild(self, handler, monkeypatch):
        self.preparar(handler)
        monkeypatch.setattr(error_handler, "send_message", AsyncMock())
        inter = _interacao()
        handler.send_webhook = AsyncMock(side_effect=[FalhaNoWebhook("400"), None])

        await handler.process_interaction_error(inter, RuntimeError("boom"))
        await asyncio.wait_for(handler.process_interaction_error(inter, RuntimeError("boom")), timeout=2)

        assert handler.send_webhook.await_count == 2


class TestWebhookSemMencoes:
    @pytest.mark.asyncio
    async def test_the_webhook_never_mentions_anyone(self, handler, monkeypatch):
        enviados: list[dict] = []

        class Sessao:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class Webhook:
            async def send(self, **kwargs):
                enviados.append(kwargs)

        monkeypatch.setattr(error_handler, "ClientSession", Sessao)
        monkeypatch.setattr(error_handler.disnake.Webhook, "from_url", lambda *a, **k: Webhook())
        handler.bot.config["AUTO_ERROR_REPORT_WEBHOOK"] = "https://discord.com/api/webhooks/1/x"
        handler.bot.user = SimpleNamespace(
            name="Bot",
            display_avatar=SimpleNamespace(replace=lambda **k: SimpleNamespace(url="https://cdn.exemplo/a.png")),
        )

        await ErrorHandler.send_webhook(handler, content="@everyone <@&1> <@2>", embed=disnake.Embed(title="x"))

        (enviado,) = enviados
        mencoes = enviado["allowed_mentions"]
        assert mencoes.everyone is False and mencoes.roles is False and mencoes.users is False
