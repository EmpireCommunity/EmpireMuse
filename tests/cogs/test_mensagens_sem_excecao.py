"""F3-6 — S-14: o texto de uma exceção não chega ao usuário, ao dono nem à página web (S-14).

Regra: onde o texto da exceção ia para a tela, entra uma mensagem genérica em pt-BR; o detalhe (com traceback) vai para o
``logger``; em erro inesperado a mensagem e o log levam o mesmo código curto (``ERR-…``). Decisões do MAESTRO:

* Lavalink: ``event.message`` (e ``exception.message`` do autoplay) CONTINUA visível, truncado e sem bloco de código; a
  ``cause`` e o ``repr`` saem da mensagem e vão para o log;
* ``start_bot``/``failed_bots``: a página pública mostra um texto genérico por bot; a instrução do PrivilegedIntentsRequired
  (e a do token inválido) continua;
* ``ll_yt_oauth``/``owner_panel``: mesmo para o dono, genérico + log; o texto do HTTP do Lavalink não vai para a mensagem;
* ``on_jukebox_message``: nunca mais aparece "None" num bloco de código.

Sem rede, sem Discord e sem ``.env``; o aleatório do código é fixado trocando ``secrets.token_hex``.
"""

import logging
import secrets
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.regression

SEGREDO = "detalhe-interno /srv/empiremuse/segredo.json"
TOKEN_FALSO = "MTIzNDU2Nzg5MDEyMzQ1Njc4OTAx.GabcDE.abcdefghijklmnopqrstuvwxyz1"
CODIGO = "ERR-7F3A2C"


def fixar_codigo(monkeypatch, valor="7f3a2c"):
    monkeypatch.setattr(secrets, "token_hex", lambda nbytes=None: valor)


def sem_vazamento(texto: str, *extras: str) -> None:
    for proibido in (SEGREDO, "segredo.json", "RuntimeError", "Traceback", "```py", "```java", "repr(", *extras):
        assert proibido not in texto, f"vazou para a tela: {proibido!r} em {texto!r}"


def registros(caplog, logger: str, nivel=logging.WARNING) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == logger and r.levelno >= nivel]


# ============================================================================ ll_yt_oauth (dono)


class FakeView:
    def __init__(self, bot=None, ctx=None):
        self.data = {"refresh_token": "refresh-falso", "access_token": "acesso-falso"}
        self.interaction = None
        self.exception_txt = ""

    async def wait(self):
        return None


class FakeAsk:
    """Confirmação aceita; a mensagem final do comando sai por ``edit_original_message`` da interação devolvida."""

    edicao = AsyncMock()

    def __init__(self, ctx=None):
        self.selected = True
        self.interaction_resp = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock(), edit_message=AsyncMock()),
            edit_original_message=self.edicao,
        )

    async def wait(self):
        return None


class Resposta:
    def __init__(self, status=200, corpo=None, texto=""):
        self.status, self._corpo, self._texto = status, corpo, texto

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self._corpo

    async def text(self):
        return self._texto

    def raise_for_status(self):
        raise AssertionError("status inesperado")


def montar_oauth(monkeypatch, tmp_path, *, mongo=None, node=None, post=None):
    from empiremuse.cogs import ll_yt_oauth

    monkeypatch.setenv("EMPIREMUSE_HOME", str(tmp_path))
    monkeypatch.setattr(ll_yt_oauth, "YtOauthView", FakeView)
    monkeypatch.setattr(ll_yt_oauth, "AskView", FakeAsk)
    monkeypatch.setattr(FakeAsk, "edicao", AsyncMock())
    sessao = SimpleNamespace(
        get=lambda *a, **k: Resposta(
            200, {"name": "Ana", "email": "ana@exemplo.invalid", "picture": "https://exemplo.invalid/a.png"}
        ),
        post=post or AsyncMock(),
    )
    bot = SimpleNamespace(
        session=sessao,
        pool=SimpleNamespace(mongo_database=mongo),
        music=SimpleNamespace(nodes={"LOCAL": node} if node else {}),
        get_color=lambda *a: 0,
    )
    mensagem = SimpleNamespace(edit=AsyncMock())
    ctx = SimpleNamespace(
        guild=SimpleNamespace(me=object()),
        author=SimpleNamespace(id=1, send=AsyncMock()),
        send=AsyncMock(return_value=mensagem),
        inter=None,
    )
    return (
        ll_yt_oauth.YtOauthLL(bot),
        ctx,
        mensagem.edit,
    )  # a nota final (com o código do erro) fica na mensagem do canal; a conta e o token vão por DM


def texto_final(edicao) -> str:
    return edicao.await_args_list[-1].kwargs["embed"].description


class TestOauthDoDono:
    @pytest.mark.asyncio
    async def test_falha_ao_salvar_no_mongo(self, monkeypatch, tmp_path, caplog):
        fixar_codigo(monkeypatch)
        mongo = SimpleNamespace(update_data=AsyncMock(side_effect=RuntimeError(SEGREDO)))
        cog, ctx, mensagem = montar_oauth(monkeypatch, tmp_path, mongo=mongo)

        with caplog.at_level(logging.WARNING, logger="empiremuse.cogs.ll_yt_oauth"):
            await cog.oauth_command(ctx)

        texto = texto_final(mensagem)
        sem_vazamento(texto)
        assert "MongoDB" in texto and CODIGO in texto
        [registro] = registros(caplog, "empiremuse.cogs.ll_yt_oauth")
        assert CODIGO in registro.getMessage() and registro.exc_info[0] is RuntimeError

    @pytest.mark.asyncio
    async def test_falha_ao_salvar_no_application_yml(self, monkeypatch, tmp_path, caplog):
        fixar_codigo(monkeypatch)
        (tmp_path / "application.yml").write_text("plugins: [sem-fechar\n", encoding="utf-8")
        cog, ctx, mensagem = montar_oauth(monkeypatch, tmp_path)

        with caplog.at_level(logging.WARNING, logger="empiremuse.cogs.ll_yt_oauth"):
            await cog.oauth_command(ctx)

        texto = texto_final(mensagem)
        assert "application.yml" in texto and CODIGO in texto
        for proibido in ("ParserError", "ScannerError", "expected", "line 1", "Error("):
            assert proibido not in texto
        assert registros(caplog, "empiremuse.cogs.ll_yt_oauth")


# ============================================================================ Music.on_jukebox_message (usuário e webhook)


def montar_pedido(monkeypatch, erro: BaseException, *, webhook=False):
    from empiremuse.cogs import music

    monkeypatch.setattr(music.asyncio, "sleep", AsyncMock())
    envios = []
    canal = SimpleNamespace(
        id=55,
        name="pedidos",
        send=AsyncMock(side_effect=lambda *a, **k: envios.append((a, k))),
        permissions_for=lambda me: SimpleNamespace(send_messages=True),
    )
    guilda = SimpleNamespace(id=9, name="Servidor", me=SimpleNamespace(voice=None), icon=None)
    autor = SimpleNamespace(
        bot=False,
        id=1,
        mention="<@1>",
        voice=None,
        display_avatar=SimpleNamespace(
            with_static_format=lambda formato: SimpleNamespace(url="https://exemplo.invalid/a.png")
        ),
    )
    mensagem = SimpleNamespace(
        author=autor, mentions=[], guild=guilda, channel=canal, content="tocar algo", attachments=[], type=None
    )
    mensagem.is_system = lambda: False
    webhook_cog = SimpleNamespace(
        webhook_max_concurrency=SimpleNamespace(acquire=AsyncMock(), release=AsyncMock()),
        send_webhook=AsyncMock(),
        mask_token=lambda texto: texto.replace(TOKEN_FALSO, "mytoken"),
    )
    bot = SimpleNamespace(
        intents=SimpleNamespace(message_content=True),
        get_data=AsyncMock(return_value={"player_controller": {"channel": "55"}}),
        music=SimpleNamespace(players={}),
        get_channel=lambda cid: canal,
        get_color=lambda *a: 0,
        http=SimpleNamespace(token=TOKEN_FALSO),
        config={"AUTO_ERROR_REPORT_WEBHOOK": "https://discord.com/api/webhooks/1/segredo" if webhook else ""},
        get_cog=lambda nome: webhook_cog,
        pool=SimpleNamespace(
            servers=SimpleNamespace(jukebox=SimpleNamespace(is_panel_bot=AsyncMock(return_value=True)))
        ),
    )
    self_ = SimpleNamespace(
        bot=bot,
        jukebox_concurrency=SimpleNamespace(acquire=AsyncMock(), release=AsyncMock()),
        delete_message=AsyncMock(),
        parse_jukebox_request=AsyncMock(side_effect=erro),
    )
    self_.jukebox_executor = AsyncMock(return_value=self_)  # o bot que toca é o próprio
    ctx = SimpleNamespace(command=None, guild=guilda, channel=canal, author=autor)
    return music.Music, self_, ctx, mensagem, envios, webhook_cog


# ============================================================================ BotPool.start_bot / failed_bots (página pública)


class TestStartBot:
    @staticmethod
    def pool():
        return SimpleNamespace(
            config={"KILL_ON_429": False},
            killing_state=False,
            failed_bots={},
            bots=[],
            shutdown=AsyncMock(),
            check_pool_ready=AsyncMock(),
        )

    @staticmethod
    def bot(erro):
        return SimpleNamespace(
            identifier="bot-1", http=SimpleNamespace(token=TOKEN_FALSO), start=AsyncMock(side_effect=erro)
        )

    @pytest.mark.asyncio
    async def test_erro_inesperado_vira_texto_generico_com_codigo_e_o_detalhe_vai_para_o_log(self, monkeypatch, caplog):
        from empiremuse.utils.client import BotPool

        fixar_codigo(monkeypatch)
        pool = self.pool()

        with caplog.at_level(logging.ERROR, logger="empiremuse.utils.client"):
            await BotPool.start_bot(pool, self.bot(RuntimeError(SEGREDO)))

        texto = pool.failed_bots["bot-1"]
        sem_vazamento(texto, TOKEN_FALSO)
        assert CODIGO in texto and "falha" in texto.lower() and "log" in texto.lower()
        [registro] = registros(caplog, "empiremuse.utils.client", logging.ERROR)
        assert CODIGO in registro.getMessage() and registro.exc_info[0] is RuntimeError


# ============================================================================ complemento do AppSec (F-3, F-4, I-11, Info)
#
# Decisões do MAESTRO depois da auditoria complementar:
# * F-3: o ``error_description`` do Spotify não chega mais ao usuário (revoga o "motivo informado" do KI-15); fica no log;
# * F-4: a mensagem do nó Lavalink aparece dentro de um code span (a crase já é trocada), nos dois usos;
# * I-11: a troca do refreshToken por ``***`` no corpo da resposta vem ANTES do corte em 500 caracteres.


# ============================================================================ lacunas apontadas pela mutação


class TestLacunasDaMutacao:
    @pytest.mark.asyncio
    async def test_resposta_do_lavalink_local_o_token_e_mascarado_e_o_corpo_vira_uma_linha_curta_no_log(
        self, monkeypatch, tmp_path, caplog
    ):
        (tmp_path / "application.yml").write_text(
            "plugins:\n  youtube:\n    oauth:\n      enabled: false\n", encoding="utf-8"
        )
        node = SimpleNamespace(
            info={"plugins": ["youtube-plugin"]},
            rest_uri="http://127.0.0.1:2333",
            _websocket=SimpleNamespace(headers={}),
        )
        corpo = "linha1\nlinha2 eco do token refresh-falso " + "x" * 600
        post = AsyncMock(return_value=Resposta(500, texto=corpo))
        cog, ctx, mensagem = montar_oauth(monkeypatch, tmp_path, node=node, post=post)

        with caplog.at_level(logging.WARNING, logger="empiremuse.cogs.ll_yt_oauth"):
            await cog.oauth_command(ctx)

        log = "\n".join(r.getMessage() for r in registros(caplog, "empiremuse.cogs.ll_yt_oauth"))
        assert "refresh-falso" not in log and "linha1 linha2 eco do token ***" in log
        assert "x" * 501 not in log and "…" in log
        assert "HTTP 500" in texto_final(mensagem)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("gera", [True, False], ids=["com_codigo", "sem_codigo"])
    async def test_song_request_webhook_nome_do_anexo_e_token_mascarado(self, monkeypatch, gera):
        if gera:
            fixar_codigo(monkeypatch)
        else:

            def quebra(nbytes=None):
                raise RuntimeError("sem entropia (simulado)")

            monkeypatch.setattr(secrets, "token_hex", quebra)
        classe, self_, ctx, mensagem, envios, cog = montar_pedido(
            monkeypatch, RuntimeError(f"falha com {TOKEN_FALSO}"), webhook=True
        )
        anexos = []
        monkeypatch.setattr(
            "empiremuse.music_cog.jukebox_messages.string_to_file", lambda texto, nome: anexos.append((texto, nome))
        )

        await classe.on_jukebox_message(self_, ctx, mensagem)

        [(texto, nome)] = anexos
        assert nome == (f"error_traceback_songrequest_{CODIGO}.txt" if gera else "error_traceback_songrequest.txt")
        assert TOKEN_FALSO not in texto and "mytoken" in texto
