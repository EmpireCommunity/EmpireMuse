"""D10: o registro do que foi desenhado só avança depois de uma edição ou envio que saiu, nos passos REAIS do ``invoke_np``.

Sem Discord real: o player é um dublê e a mensagem, um ``AsyncMock``; as funções de ``np_steps`` e o ``invoke_np`` são as de produção.
"""

import asyncio
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from empiremuse.utils.music.models import np_steps, player_message
from empiremuse.utils.music.models.step_state import NpState

pytestmark = pytest.mark.regression

DATA = {"content": "np", "components": [], "embeds": []}
PRIVADA = SimpleNamespace(view_channel=False, connect=False)
PUBLICA = SimpleNamespace(view_channel=True, connect=True)


class Jogador(SimpleNamespace):
    __hash__ = object.__hash__  # o estado de render é um WeakKeyDictionary


def erro_http():
    return disnake.HTTPException(SimpleNamespace(status=503, reason="x"), "falha")


def canal_com_permissao():
    canal = MagicMock()
    canal.id = 77
    canal.permissions_for.return_value = SimpleNamespace(send_messages=True, read_messages=True)
    return canal


def jogador(*, edit=None, send=None, privada=True, **campos):
    canal = canal_com_permissao()
    canal.send = send or AsyncMock(return_value=SimpleNamespace(id=2))
    voz = SimpleNamespace(
        id=1, permissions_for=lambda _r: PRIVADA if privada else PUBLICA, guild=SimpleNamespace(default_role=None)
    )
    skin = SimpleNamespace(load=lambda player: dict(DATA))
    base = dict(
        is_closing=False,
        text_channel=canal,
        current=SimpleNamespace(title="faixa"),
        updating=False,
        has_lyrics=False,
        retry_setup_hints=False,
        static=False,
        skin="padrao",
        bot=SimpleNamespace(
            pool=SimpleNamespace(player_skins={"padrao": skin}),
            get_channel=lambda _id: canal,
            user=MagicMock(id=9),
        ),
        last_data={},
        controller_mode=True,
        temp_embed=None,
        message=SimpleNamespace(edit=edit or AsyncMock(), id=1),
        guild=SimpleNamespace(me=SimpleNamespace(voice=SimpleNamespace(channel=voz))),
        allowed_mentions=None,
        ignore_np_once=True,
        has_thread=False,
        paused=False,
        keep_connected=True,
        details_hidden_rendered=False,
        start_message_updater_task=lambda: None,
        destroy_message=AsyncMock(),
        is_last_message=lambda: True,
        guild_id=42,
        set_command_log=lambda *a, **k: None,
        tr=lambda key, **k: key,
        update=False,
    )
    base.update(campos)
    return Jogador(**base)


@pytest.fixture(autouse=True)
def sem_espera(monkeypatch):
    monkeypatch.setattr(np_steps.asyncio, "sleep", AsyncMock())  # a pausa de 0,5 s depois da edição


def requisicao(**campos):
    req = NpState(force=False, interaction=campos.pop("interaction", None), rpc_update=False)
    req.data = dict(DATA)
    return req


# ---------------------------------------------------------------------------------------------- cada passo real


@pytest.mark.asyncio
@pytest.mark.parametrize("falha", [False, True], ids=["ok", "edicao-falha"])
async def test_message_edit_marca_published_so_com_edicao_que_saiu(falha):
    edit = AsyncMock(side_effect=erro_http() if falha else None)
    player, req = jogador(edit=edit), requisicao()
    await np_steps._np_message_edit(player, req)
    edit.assert_awaited_once()
    assert req.published is (not falha)


@pytest.mark.asyncio
@pytest.mark.parametrize("falha", [False, True], ids=["ok", "envio-falha"])
async def test_send_new_texto_marca_published_so_com_envio_que_saiu(falha):
    send = AsyncMock(side_effect=erro_http() if falha else None, return_value=SimpleNamespace(id=2))
    player, req = jogador(send=send, message=None), requisicao()
    await np_steps._np_send_new(player, req)
    assert req.published is (not falha)


@pytest.mark.asyncio
@pytest.mark.parametrize("falha", [False, True], ids=["ok", "edicao-do-painel-falha"])
async def test_send_new_painel_marca_published_so_com_a_edicao_do_painel(falha):
    nova = SimpleNamespace(edit=AsyncMock(side_effect=erro_http() if falha else None))
    painel = SimpleNamespace(resend=AsyncMock(return_value=nova))
    player = jogador(message=None, static=True, channel_cleanup=AsyncMock())
    player.bot.pool.jukebox_panel = painel
    req = requisicao()
    await np_steps._np_send_new(player, req)
    assert req.published is (not falha)


@pytest.mark.asyncio
@pytest.mark.parametrize("falha", [False, True], ids=["ok", "interacao-falha"])
async def test_interaction_edit_marca_published_so_com_a_edicao_que_saiu(falha):
    resposta = SimpleNamespace(
        is_done=lambda: False, edit_message=AsyncMock(side_effect=erro_http() if falha else None)
    )
    interacao = SimpleNamespace(response=resposta, message=None)
    player, req = jogador(), requisicao(interaction=interacao)
    await np_steps._np_interaction_edit(player, req)
    assert req.published is (not falha)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("caso", "esperado"),
    [
        ("edita", True),
        ("edicao-falha-e-reenvia", True),
        ("edicao-proibida", False),
        ("sem-permissao-de-envio", False),
        ("nao-e-a-ultima-mensagem", False),
        ("sem-conexao-fixa-envia", True),
        ("sem-conexao-fixa-envio-falha", False),
    ],
)
async def test_plain_marca_published_so_quando_algo_saiu(caso, esperado):
    edit, send = AsyncMock(), AsyncMock(return_value=SimpleNamespace(id=3))
    campos = {}
    if caso == "edicao-falha-e-reenvia":
        edit.side_effect = erro_http()
    elif caso == "edicao-proibida":
        edit.side_effect = disnake.Forbidden(SimpleNamespace(status=403, reason="x"), "sem")
    elif caso == "sem-permissao-de-envio":
        campos["text_channel"] = MagicMock()
        campos["text_channel"].permissions_for.return_value = SimpleNamespace(send_messages=False)
    elif caso == "nao-e-a-ultima-mensagem":
        campos["is_last_message"] = lambda: False
    elif caso.startswith("sem-conexao-fixa"):
        campos["keep_connected"] = False
        campos["message_updater_task"] = MagicMock()
        if caso.endswith("falha"):
            send.side_effect = erro_http()
    player = jogador(edit=edit, send=send, **campos)
    req = requisicao()
    try:
        await np_steps._np_plain(player, req)
    except disnake.HTTPException:
        pass  # o envio que levanta é o caso: nada saiu
    assert req.published is esperado


# ---------------------------------------------------------------------------------- o invoke_np real, duas tentativas


@pytest.mark.asyncio
async def test_edicao_que_falha_so_confirma_na_tentativa_que_edita():
    """A 1ª edição levanta 5xx: nada é confirmado e o ``last_data`` é zerado; a 2ª refaz o desenho, edita e só então confirma."""
    edit = AsyncMock(side_effect=[erro_http(), None])
    player = jogador(edit=edit, privada=False, details_hidden_rendered=True)  # a call virou pública: passa a mostrar
    await player_message.PlayerMessageMixin.invoke_np(player)
    assert player.details_hidden_rendered is True and player.last_data == {}
    await player_message.PlayerMessageMixin.invoke_np(player)
    assert edit.await_count == 2
    assert player.details_hidden_rendered is False


@pytest.mark.asyncio
async def test_pedido_superado_nao_confirma_o_registro(monkeypatch):
    async def superado(self, req):
        req.superseded = True  # o cartão antigo foi superado por um pedido mais novo
        return True

    monkeypatch.setattr(player_message, "_np_data", superado)
    player = jogador(details_hidden_rendered=False)
    await player_message.PlayerMessageMixin.invoke_np(player)
    assert player.details_hidden_rendered is False


@pytest.mark.asyncio
async def test_painel_assumindo_o_player_confirma_depois_de_refazer_o_painel():
    refresh = AsyncMock()
    painel = SimpleNamespace(takes_over=lambda _g: True, refresh=refresh)
    player = jogador(static=True, details_hidden_rendered=False)  # privada: o resultado vira para "esconde"
    player.bot.pool.jukebox_panel = painel
    player._panel_takes_over = partial(player_message.PlayerMessageMixin._panel_takes_over, player)
    await player_message.PlayerMessageMixin.invoke_np(player)
    refresh.assert_awaited_once_with(42)
    assert player.details_hidden_rendered is True
