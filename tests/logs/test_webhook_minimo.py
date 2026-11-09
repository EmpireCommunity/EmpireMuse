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


def canal_de_voz(nome: str, id_: int) -> SimpleNamespace:
    return SimpleNamespace(name=nome, id=id_, user_limit=0, voice_states={})


CONFIG = "https://discord.com/api/webhooks/1/segredo"


def contexto_cheio() -> SimpleNamespace:
    """Contexto de prefixo com tudo o que o relatório antigo copiava: nomes, IDs, ícone real, avatar, menção e opção de membro."""
    ctx = _Contexto(True)
    ctx.guild = SimpleNamespace(
        id=ID_SERVIDOR,
        name="Servidor Privado",
        icon=SimpleNamespace(
            with_static_format=lambda f: SimpleNamespace(url=f"https://cdn.exemplo/icons/{ID_SERVIDOR}/a.png")
        ),
        me=SimpleNamespace(voice=SimpleNamespace(channel=canal_de_voz("Sala bot", ID_VOZ_BOT))),
    )
    ctx.channel = SimpleNamespace(
        name="canal-privado", id=ID_CANAL, permissions_for=lambda me: SimpleNamespace(embed_links=True)
    )
    ctx.author = SimpleNamespace(
        id=ID_AUTOR,
        bot=False,
        mention=f"<@{ID_AUTOR}>",
        voice=SimpleNamespace(channel=canal_de_voz("Sala do autor", ID_VOZ_AUTOR)),
    )
    ctx.message = SimpleNamespace(content=f"!play {MENCAO} texto livre privado")
    return ctx


MENCAO = "<@123456789012345678>"


def dump(embed: disnake.Embed) -> str:
    return "\n".join(
        [embed.title or "", embed.description or "", embed.footer.text or "", embed.footer.icon_url or ""]
        + [f.value for f in embed.fields]
    )


def confere_minimo(embed: disnake.Embed) -> None:
    texto = dump(embed)
    assert [i for i in TODOS_OS_IDS if i in texto] == []
    for proibido in (
        "Servidor Privado",
        "canal-privado",
        "Sala ",
        "texto livre privado",
        "123456789012345678",
        "cdn.exemplo",
        "Member(",
    ):
        assert proibido not in texto, proibido
    assert embed.footer.text is None and embed.footer.icon_url is None and embed.thumbnail.url is None
    assert embed.author.name is None and embed.image.url is None
    assert [f.name for f in embed.fields] == ["Código do erro:"]


class TestEmbed:
    @pytest.mark.asyncio
    async def test_prefixo_com_icone_real_mencao_e_conteudo_so_leva_o_codigo(self, handler):
        handler.bot.config["AUTO_ERROR_REPORT_WEBHOOK"] = CONFIG

        await handler.on_legacy_command_error(contexto_cheio(), _erro_de_disco())

        confere_minimo(handler.send_webhook.await_args.kwargs["embed"])

    @pytest.mark.asyncio
    async def test_interacao_com_opcao_de_membro_so_leva_o_codigo(self, handler, enviado):
        handler.bot.config["AUTO_ERROR_REPORT_WEBHOOK"] = CONFIG
        inter = _interacao()
        inter.guild = contexto_cheio().guild
        inter.author = contexto_cheio().author
        inter.filled_options = {"alvo": f"Member(id={ID_AUTOR})", "busca": "texto livre privado"}

        await handler.process_interaction_error(inter, _erro_de_disco())

        confere_minimo(handler.send_webhook.await_args.kwargs["embed"])

    def test_o_embed_compartilhado_sem_codigo_nao_tem_campo(self):
        assert error_handler.ErrorHandler.report_embed_with_code("").fields == []


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
    async def test_o_report_manual_leva_so_o_texto_do_membro_sem_autor_icone_nem_servidor(self, handler):
        handler.bot.get_color = lambda *a: 0
        autor = SimpleNamespace(id=ID_AUTOR, avatar=SimpleNamespace(url="https://cdn.exemplo/avatar.png"))
        inter = MagicMock()
        inter.custom_id = "error_report_submit_1"
        inter.author = autor
        inter.guild = contexto_cheio().guild
        inter.message.embeds = [disnake.Embed(description="Ocorreu um erro. Código: ERR-ABCDEF")]
        inter.text_values = {"error_details": "o bot travou quando pedi uma música", "image_url": ""}
        inter.response.edit_message = AsyncMock()

        await handler.on_report_submit(inter)

        handler.send_webhook.assert_awaited_once()
        embed = handler.send_webhook.await_args.kwargs["embed"]
        texto = dump(embed)
        assert "o bot travou" in texto and "ERR-ABCDEF" in texto
        assert (
            [i for i in TODOS_OS_IDS if i in texto] == []
            and "Servidor Privado" not in texto
            and "cdn.exemplo" not in texto
        )
        assert embed.footer.text is None and embed.footer.icon_url is None
        assert embed.author.name is None and embed.author.icon_url is None and embed.thumbnail.url is None

    @pytest.mark.asyncio
    async def test_a_imagem_opcional_do_membro_segue(self, handler):
        handler.bot.get_color = lambda *a: 0
        inter = MagicMock()
        inter.custom_id = "error_report_submit_1"
        inter.guild = contexto_cheio().guild
        inter.message.embeds = [disnake.Embed(description="x")]
        inter.text_values = {"error_details": "d", "image_url": "https://exemplo.invalido/print.png"}
        inter.response.edit_message = AsyncMock()

        await handler.on_report_submit(inter)

        assert handler.send_webhook.await_args.kwargs["embed"].image.url == "https://exemplo.invalido/print.png"


class TestOutrosRemetentes:
    """Todo remetente ao webhook segue a mesma regra: só tipo e código (jukebox no teste de mensagens; aqui reprodução e fila)."""

    @pytest.mark.asyncio
    async def test_report_error_do_player_so_leva_tipo_e_codigo(self):
        from empiremuse.utils.music.models.player import LavalinkPlayer

        fila = AsyncMock()
        jogador = SimpleNamespace(bot=SimpleNamespace(get_cog=lambda nome: SimpleNamespace(error_report_queue=fila)))

        await LavalinkPlayer.report_error(jogador, KeyError("segredo-no-texto"), "ERR-ABC123")

        enviado = fila.put.await_args.args[0]
        assert [f.value for f in enviado["embed"].fields] == ["`ERR-ABC123`"] and enviado["embed"].thumbnail.url is None
        corpo = enviado["file"].fp.read().decode()
        assert corpo == "Tipo da exceção: KeyError\nCódigo do erro: ERR-ABC123\n"

    @pytest.mark.asyncio
    async def test_report_error_sem_fila_nao_faz_nada(self):
        from empiremuse.utils.music.models.player import LavalinkPlayer

        jogador = SimpleNamespace(bot=SimpleNamespace(get_cog=lambda nome: SimpleNamespace(error_report_queue=None)))

        await LavalinkPlayer.report_error(jogador, None)

    def test_texto_minimo_do_modulo_compartilhado(self):
        from empiremuse.logs.webhook import minimal_embed, minimal_report_text

        assert minimal_report_text(None, "") == "Tipo da exceção: NoneType\nCódigo do erro: -\n"
        assert minimal_embed("T", "").fields == [] and minimal_embed("T", "ERR-1").fields[0].value == "`ERR-1`"


class TestRemetentesDeReproducao:
    """``_process_next`` (falha ao tocar), ``_he_report_exception`` (falha da faixa) e ``_rt_report_failure`` (PartialTrack)."""

    @pytest.mark.asyncio
    async def test_process_next_envia_so_tipo_e_codigo_sem_traceback_nem_id_do_servidor(self, monkeypatch):
        from empiremuse.utils.music.models import player_playback
        from tests.audio.test_playback_retry_youtube import get_i18n

        webhook = SimpleNamespace(send_webhook=AsyncMock())
        jogador = SimpleNamespace(
            tr=get_i18n().for_locale("pt-BR"),
            current=None,
            text_channel=None,
            locked=False,
            guild=SimpleNamespace(id=ID_SERVIDOR, name="Servidor Privado"),
            bot=SimpleNamespace(get_cog=lambda nome: webhook),
            process_next=AsyncMock(),
        )
        monkeypatch.setattr(player_playback.asyncio, "sleep", AsyncMock())
        monkeypatch.setattr(
            player_playback,
            "_pn_pre_checks",
            AsyncMock(side_effect=KeyError(f"consulta {CAMINHO} do servidor {ID_SERVIDOR}")),
        )

        await player_playback.PlayerPlaybackMixin._process_next(jogador)

        enviado = webhook.send_webhook.await_args.kwargs
        anexo = enviado["file"]
        corpo = anexo.fp.read().decode()
        assert corpo.startswith("Tipo da exceção: KeyError\nCódigo do erro: ERR-") and len(corpo.splitlines()) == 2
        assert CAMINHO not in corpo and str(ID_SERVIDOR) not in corpo and "Traceback" not in corpo
        assert str(ID_SERVIDOR) not in dump(enviado["embed"]) and "Servidor Privado" not in dump(enviado["embed"])
        assert anexo.filename == "error_traceback_ytdl.txt"
        jogador.process_next.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_falha_da_faixa_passa_o_evento_e_o_codigo_ao_report_error(self):
        from empiremuse.utils.music.models.event_steps import _he_report_exception

        jogador = SimpleNamespace(node=SimpleNamespace(identifier="N1"), report_error=AsyncMock())
        evento = SimpleNamespace(name="evento")
        faixa = SimpleNamespace(uri="https://exemplo.invalido/x", search_uri=None)

        await _he_report_exception(jogador, evento, faixa, "formato", "ERR-ABC123")

        jogador.report_error.assert_awaited_once_with(evento, "ERR-ABC123")

    @pytest.mark.asyncio
    async def test_falha_de_partial_track_passa_a_excecao_e_o_codigo(self):
        from empiremuse.utils.music.models.autoqueue_steps import _rt_report_failure

        jogador = SimpleNamespace(report_error=AsyncMock())
        faixa = SimpleNamespace(uri="https://exemplo.invalido/x", search_uri=None, title="Faixa privada")
        erro = ValueError("segredo")

        await _rt_report_failure(jogador, faixa, erro)

        (passado, codigo), _ = jogador.report_error.await_args
        assert passado is erro and CODIGO.fullmatch(codigo or "ERR-000000")
