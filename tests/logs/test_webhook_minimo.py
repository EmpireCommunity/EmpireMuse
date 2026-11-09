# ruff: noqa: F811
"""R5: o webhook de erro leva SÓ o tipo da exceção e o código; o embed e o modal sem IDs nem autor.

O canal do Discord guarda o que recebe sem prazo; o traceback completo fica no arquivo de log (com retenção) e se acha pelo código.
"""

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from empiremuse.cogs import error_handler
from tests.cogs.test_error_handler import (  # noqa: F401
    CAMINHO,
    TOKEN_FALSO,
    _Contexto,
    _erro_de_disco,
    _interacao,
    arquivos_do_webhook,
    enviado,
    handler,
)
from tests.logs.test_errors_relatorio import avatar, canal_de_voz, prefixo, servidor_do_relatorio, tratador
from tests.logs.test_errors_relatorio_limites import tudo_do_embed

pytestmark = pytest.mark.critical

ID_AUTOR, ID_SERVIDOR, ID_CANAL, ID_VOZ_AUTOR, ID_VOZ_BOT = (
    7700000000000001,
    7700000000000002,
    7700000000000003,
    7700000000000004,
    7700000000000005,
)
TODOS_OS_IDS = [str(i) for i in (ID_AUTOR, ID_SERVIDOR, ID_CANAL, ID_VOZ_AUTOR, ID_VOZ_BOT)]
CODIGO = re.compile(r"ERR-[0-9A-F]{6}")
CONFIG = "https://discord.com/api/webhooks/1/segredo"


def contexto_cheio() -> SimpleNamespace:
    ctx = prefixo("!play x")
    ctx.guild = servidor_do_relatorio(
        "Servidor", voz_do_bot=SimpleNamespace(channel=canal_de_voz("Sala bot", ID_VOZ_BOT))
    )
    ctx.guild.id = ID_SERVIDOR
    ctx.channel = SimpleNamespace(name="geral", id=ID_CANAL)
    ctx.author.id = ID_AUTOR
    ctx.author.voice = SimpleNamespace(channel=canal_de_voz("Sala do autor", ID_VOZ_AUTOR))
    ctx.author.display_avatar = avatar()
    return ctx


class TestEmbed:
    def test_nenhum_id_de_usuario_servidor_ou_canal_nem_o_autor(self):
        embed = tratador().build_report_embed(contexto_cheio())

        texto = tudo_do_embed(embed)
        assert [i for i in TODOS_OS_IDS if i in texto] == []
        assert embed.footer.text is None and embed.author.name is None
        assert "Servidor" in texto and "geral" in texto  # continua útil: nomes e comando

    def test_fora_de_servidor_nao_leva_o_id_do_servidor(self):
        ctx = contexto_cheio()
        ctx.guild = None
        ctx.guild_id = ID_SERVIDOR

        embed = tratador().build_report_embed(ctx)

        assert str(ID_SERVIDOR) not in tudo_do_embed(embed)


class TestAnexo:
    @pytest.mark.asyncio
    async def test_interacao_so_com_tipo_e_codigo(self, handler, enviado, arquivos_do_webhook):
        handler.bot.config["AUTO_ERROR_REPORT_WEBHOOK"] = CONFIG

        await handler.process_interaction_error(_interacao(), _erro_de_disco())

        handler.send_webhook.assert_awaited_once()
        (relatorio,) = arquivos_do_webhook
        assert relatorio.startswith("Tipo da exceção: OSError\nCódigo do erro: ERR-")
        assert len(CODIGO.findall(relatorio)) == 1 and len(relatorio.splitlines()) == 2
        assert CAMINHO not in relatorio and TOKEN_FALSO not in relatorio and "Traceback" not in relatorio
        assert "No space left" not in relatorio

    @pytest.mark.asyncio
    async def test_prefixo_so_com_tipo_e_codigo(self, handler, arquivos_do_webhook):
        handler.bot.config["AUTO_ERROR_REPORT_WEBHOOK"] = CONFIG

        await handler.on_legacy_command_error(_Contexto(True), _erro_de_disco())

        (relatorio,) = arquivos_do_webhook
        assert relatorio.startswith("Tipo da exceção: OSError\nCódigo do erro: ERR-")
        assert CAMINHO not in relatorio and TOKEN_FALSO not in relatorio and "Traceback" not in relatorio

    def test_excecao_embrulhada_mostra_o_tipo_original_e_sem_codigo_sai_um_traco(self):
        embrulho = SimpleNamespace(original=KeyError("segredo-na-chave"))

        texto = error_handler.ErrorHandler.minimal_webhook_report(embrulho, "")

        assert texto == "Tipo da exceção: KeyError\nCódigo do erro: -\n"


class TestModal:
    @pytest.mark.asyncio
    async def test_o_report_manual_nao_leva_nome_id_nem_avatar_do_autor_nem_id_do_servidor(self, handler):
        handler.bot.get_color = lambda *a: 0
        autor = SimpleNamespace(
            id=ID_AUTOR, avatar=SimpleNamespace(url="https://cdn.exemplo/a.png"), __str__=lambda s: "Fulano"
        )
        inter = MagicMock()
        inter.custom_id = "error_report_submit_1"
        inter.author = autor
        inter.guild = SimpleNamespace(id=ID_SERVIDOR, name="Servidor Teste", icon=None, me=SimpleNamespace())
        inter.message.embeds = [disnake.Embed(description="Ocorreu um erro. Código: ERR-ABCDEF")]
        inter.text_values = {"error_details": "o bot travou quando pedi uma música", "image_url": ""}
        inter.response.edit_message = AsyncMock()

        await handler.on_report_submit(inter)

        handler.send_webhook.assert_awaited_once()
        embed = handler.send_webhook.await_args.kwargs["embed"]
        texto = tudo_do_embed(embed)
        assert embed.author.name is None and embed.author.icon_url is None
        assert [i for i in TODOS_OS_IDS if i in texto] == [] and "Fulano" not in texto
        assert "o bot travou" in texto and "Servidor Teste" in embed.footer.text


class TestOutrosRemetentes:
    """Todo remetente ao webhook segue a mesma regra: só tipo e código (jukebox no teste de mensagens; aqui reprodução e fila)."""

    @pytest.mark.asyncio
    async def test_report_error_do_player_nao_leva_faixa_servidor_nem_canal(self):
        from empiremuse.utils.music.models.player import LavalinkPlayer

        fila = AsyncMock()
        jogador = SimpleNamespace(bot=SimpleNamespace(get_cog=lambda nome: SimpleNamespace(error_report_queue=fila)))
        embed = disnake.Embed(description=f"[Faixa privada](https://exemplo.invalido/x) {ID_SERVIDOR} {ID_CANAL}")

        await LavalinkPlayer.report_error(
            jogador, embed, SimpleNamespace(title="Faixa privada"), KeyError("segredo"), "ERR-ABC123"
        )

        enviado = fila.put.await_args.args[0]
        corpo = tudo_do_embed(enviado["embed"])
        assert "ERR-ABC123" in corpo and "Faixa privada" not in corpo and str(ID_SERVIDOR) not in corpo
        assert enviado["embed"].thumbnail.url is None

    @pytest.mark.asyncio
    async def test_report_error_sem_fila_nao_faz_nada(self):
        from empiremuse.utils.music.models.player import LavalinkPlayer

        jogador = SimpleNamespace(bot=SimpleNamespace(get_cog=lambda nome: SimpleNamespace(error_report_queue=None)))

        await LavalinkPlayer.report_error(jogador, disnake.Embed(), None)

    def test_texto_minimo_do_modulo_compartilhado(self):
        from empiremuse.logs.webhook import minimal_embed, minimal_report_text

        assert minimal_report_text(None, "") == "Tipo da exceção: NoneType\nCódigo do erro: -\n"
        assert minimal_embed("T", "").fields == [] and minimal_embed("T", "ERR-1").fields[0].value == "`ERR-1`"
