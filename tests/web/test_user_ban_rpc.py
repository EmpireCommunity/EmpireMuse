# ruff: noqa: F811 - os fixtures ``iniciar_servidor``, ``sessao`` e ``segredo`` são importados de test_rpc_websocket
"""E5 (R8.4): o RPC fecha para o banido (Q14): relé recusa o registro, ``_send_rpc_data`` o omite e o ban derruba a conexão aberta.

Servidor Tornado real em porta efêmera (a infraestrutura de ``test_rpc_websocket``); a lista de banidos é a do pool, com banco falso.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from empiremuse import rpc_protocol, web_app
from empiremuse.rules.user_ban import RPC_BLOCKED_REASON, UserBanRecord
from empiremuse.utils.music.models.player_state import PlayerStateMixin
from empiremuse.utils.user_ban import UserBans
from tests.web.test_rpc_websocket import (  # noqa: F401 - fixtures e utilitários do servidor real
    ASSENTAR,
    _fechamento,
    _nada_recebido,
    _registrar_bot,
    _registrar_usuario,
    _snowflake,
    _token_do_app,
    iniciar_servidor,
    segredo,
    sessao,
)

pytestmark = pytest.mark.critical

DONO = 100000000000000009


def banir(servidor, *ids):
    bans = servidor.pool.user_bans
    bans._records = {uid: UserBanRecord(uid, 1, "m", DONO) for uid in ids}
    bans._loaded = True
    return bans


async def observar(ws, espera: float = 0.5):
    """Tudo o que o servidor mandou em ``espera`` s (textos) e o código de fechamento (``None`` se a conexão seguiu aberta)."""
    textos = []
    while True:
        try:
            msg = await ws.receive(timeout=espera)
        except asyncio.TimeoutError:
            return textos, None
        if msg.type == aiohttp.WSMsgType.TEXT:
            textos.append(json.loads(msg.data))
        else:
            return textos, ws.close_code


async def mensagens_ate_fechar(ws) -> list[dict]:
    textos = []
    while True:
        msg = await ws.receive(timeout=2.0)
        if msg.type == aiohttp.WSMsgType.TEXT:
            textos.append(json.loads(msg.data))
        else:
            return textos


async def _resultado_do_vetor_a(servidor, sessao, uid):
    """Vetor A: a conexão A registra ``uid``; a conexão B registra o mesmo ``uid``. O que A vê (mensagens e fechamento) e quem ficou em ``users_ws``."""
    ws_a = await _registrar_usuario(servidor, sessao, [uid], _token_do_app())
    ws_b = await _registrar_usuario(servidor, sessao, [uid], _token_do_app())
    vistas_por_a = await observar(ws_a)
    return vistas_por_a, await observar(ws_b), web_app.users_ws.get(uid) is not None


@pytest.mark.asyncio
async def test_vector_a_a_second_registration_kicks_the_first_the_same_for_banned_and_free(iniciar_servidor, sessao):
    servidor = iniciar_servidor()
    banido, livre = _snowflake(), _snowflake()
    banir(servidor, banido)

    com_banido = await _resultado_do_vetor_a(servidor, sessao, banido)
    com_livre = await _resultado_do_vetor_a(servidor, sessao, livre)

    assert (
        com_banido == com_livre
    )  # mesma mensagem e mesmo fechamento para A, B segue aberta, ID registrado nos dois casos
    textos, codigo = com_livre[0]
    assert textos and codigo == rpc_protocol.CLOSE_REFUSED  # e o chute acontece de fato (não é um empate de "nada")


async def _resultado_do_vetor_b(servidor, sessao, proprio, novo):
    """Vetor B: a conexão registra o próprio ``proprio`` e depois reenvia a lista ``[novo]``. O que a conexão vê e quem está em ``users_ws``."""
    ws = await _registrar_usuario(servidor, sessao, [proprio], _token_do_app())
    await ws.send_json({"op": "rpc_update", "user_ids": [novo], "token": _token_do_app(), "version": "3.0.0"})
    await asyncio.sleep(ASSENTAR)
    vistas = await observar(ws)
    return vistas, proprio in web_app.users_ws, novo in web_app.users_ws


@pytest.mark.asyncio
async def test_vector_b_re_registering_replaces_the_previous_ids_the_same_for_banned_and_free(iniciar_servidor, sessao):
    servidor = iniciar_servidor()
    banido, livre = _snowflake(), _snowflake()
    banir(servidor, banido)

    com_banido = await _resultado_do_vetor_b(servidor, sessao, _snowflake(), banido)
    com_livre = await _resultado_do_vetor_b(servidor, sessao, _snowflake(), livre)

    assert com_banido == com_livre == (([], None), False, True)  # o ID antigo sai e o novo entra, igual nos dois casos


@pytest.mark.asyncio
async def test_nothing_reaches_the_bots_or_the_client_for_a_banned_id(iniciar_servidor, sessao, segredo):
    servidor = iniciar_servidor({"RPC_SERVER_SECRET": segredo})
    banido, outro_banido, livre = _snowflake(), _snowflake(), _snowflake()
    banir(servidor, banido, outro_banido)
    ws_bot, _ = await _registrar_bot(servidor, sessao, segredo)

    ws_banido = await _registrar_usuario(servidor, sessao, [outro_banido], _token_do_app())
    ws_misto = await _registrar_usuario(servidor, sessao, [banido, livre], _token_do_app())
    repassados, _ = await observar(ws_bot)
    assert [r["user_ids"] for r in repassados] == [
        [livre]
    ]  # o registro só só-banido não é repassado; no misto, só o livre

    await ws_bot.send_json({"op": "update", "user": banido, "track": "x"})
    await ws_bot.send_json({"op": "update", "user": outro_banido, "track": "z"})
    await ws_bot.send_json({"op": "update", "user": livre, "track": "y"})
    entregue_misto, _ = await observar(ws_misto)
    entregue_banido, _ = await observar(ws_banido)

    assert [m.get("track") for m in entregue_misto] == ["y"]  # a mensagem do banido não chega ao cliente
    assert entregue_banido == []


@pytest.mark.asyncio
async def test_without_a_token_a_banned_id_gets_exactly_what_a_free_id_gets(iniciar_servidor, sessao):
    servidor = iniciar_servidor()
    banido, livre = _snowflake(), _snowflake()
    banir(servidor, banido)

    resultados = []
    for uid in (banido, livre):
        ws = await _registrar_usuario(servidor, sessao, [uid], "")
        resultados.append(await observar(ws))

    assert resultados[0] == resultados[1]  # mesma mensagem e mesmo código de fechamento, sem distinção


@pytest.mark.asyncio
async def test_a_common_user_still_registers(iniciar_servidor, sessao):
    servidor = iniciar_servidor()
    banir(servidor, _snowflake())
    livre = _snowflake()

    ws = await _registrar_usuario(servidor, sessao, [livre], _token_do_app())

    assert livre in web_app.users_ws and await _fechamento(ws, ASSENTAR) is None


@pytest.mark.asyncio
async def test_the_ban_closes_an_open_connection_with_the_new_reason(iniciar_servidor, sessao):
    servidor = iniciar_servidor()
    alvo, outro = _snowflake(), _snowflake()
    ws_alvo = await _registrar_usuario(servidor, sessao, [alvo], _token_do_app())
    ws_outro = await _registrar_usuario(servidor, sessao, [outro], _token_do_app())
    banco = SimpleNamespace(query_data=AsyncMock(return_value=[]), update_data=AsyncMock(), delete_data=AsyncMock())
    bans = UserBans(
        SimpleNamespace(database=banco, panel_sessions=None), owners=frozenset({DONO})
    )  # fechador real: ``close_rpc_for``

    resultado = await bans.ban(DONO, alvo, "")

    assert resultado.status == "OK"
    assert await mensagens_ate_fechar(ws_alvo) == [{"op": "disconnect", "reason": RPC_BLOCKED_REASON}]
    assert ws_alvo.close_code == rpc_protocol.CLOSE_REFUSED
    assert alvo not in web_app.users_ws and outro in web_app.users_ws
    assert await _fechamento(ws_outro, ASSENTAR) is None


def test_the_erasure_reason_is_still_the_default_of_close_rpc_for():
    assert web_app.RPC_ERASED_REASON == "Dados apagados."
    assert web_app.close_rpc_for.__defaults__ == (web_app.RPC_ERASED_REASON,)


@pytest.mark.asyncio
async def test_send_rpc_data_skips_the_banned_user_without_even_reading_the_database():
    banido, livre = 100000000000000001, 100000000000000002
    bans = UserBans(SimpleNamespace(), owners=frozenset({DONO}))
    bans._records, bans._loaded = {banido: UserBanRecord(banido, 1, "m", DONO)}, True
    dados = {"token": "t" * 50, "lastfm": {"username": "fulano"}}
    enviados = []
    bot = SimpleNamespace(
        pool=SimpleNamespace(user_bans=bans),
        config={"ENABLE_RPC_AUTH": False},
        get_global_data=AsyncMock(return_value=dados),
        ws_client=SimpleNamespace(send=AsyncMock(side_effect=lambda stats: enviados.append(dict(stats)))),
    )
    player = SimpleNamespace(bot=bot)

    await PlayerStateMixin._send_rpc_data(player, [banido, livre], {"op": "update"})

    assert [e["user"] for e in enviados] == [livre]
    assert [c.kwargs["id_"] for c in bot.get_global_data.await_args_list] == [livre]
