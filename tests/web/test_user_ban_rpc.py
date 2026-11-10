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


@pytest.mark.asyncio
async def test_a_banned_id_is_ignored_silently_and_the_free_ids_of_the_list_register(iniciar_servidor, sessao, segredo):  # noqa: F811 - fixtures importadas
    servidor = iniciar_servidor({"RPC_SERVER_SECRET": segredo})
    banido, livre = _snowflake(), _snowflake()
    banir(servidor, banido)
    ws_bot, _ = await _registrar_bot(servidor, sessao, segredo)
    ws_outro = await _registrar_usuario(servidor, sessao, [_snowflake()], _token_do_app())

    ws_misto = await _registrar_usuario(servidor, sessao, [banido, livre], _token_do_app())  # lista mista

    assert banido not in web_app.users_ws and web_app.users_ws[livre] is not None  # só o ID livre foi registrado
    assert await _nada_recebido(ws_misto) and await _fechamento(ws_misto, ASSENTAR) is None  # nada diferente do sucesso
    assert await _fechamento(ws_outro, ASSENTAR) is None  # a sessão de outro ID segue
    repassados, _ = await observar(ws_bot)
    ids_repassados = [uid for r in repassados for uid in r["user_ids"]]
    assert livre in ids_repassados and banido not in ids_repassados  # o banido não é repassado aos bots


@pytest.mark.asyncio
async def test_a_list_with_only_banned_ids_changes_nothing_and_says_nothing(iniciar_servidor, sessao, segredo):  # noqa: F811 - fixtures importadas
    servidor = iniciar_servidor({"RPC_SERVER_SECRET": segredo})
    banido = _snowflake()
    banir(servidor, banido)
    ws_bot, _ = await _registrar_bot(servidor, sessao, segredo)

    ws = await _registrar_usuario(servidor, sessao, [banido], _token_do_app())

    assert banido not in web_app.users_ws
    assert (
        await _nada_recebido(ws) and await _fechamento(ws, ASSENTAR) is None
    )  # igual a um registro livre: sem mensagem e sem fechar
    assert await _nada_recebido(ws_bot)  # nada foi repassado aos bots


@pytest.mark.asyncio
async def test_without_a_token_a_banned_id_gets_exactly_what_a_free_id_gets(iniciar_servidor, sessao):  # noqa: F811 - fixtures importadas
    servidor = iniciar_servidor()
    banido, livre = _snowflake(), _snowflake()
    banir(servidor, banido)

    resultados = []
    for uid in (banido, livre):
        ws = await _registrar_usuario(servidor, sessao, [uid], "")
        resultados.append(await observar(ws))

    assert resultados[0] == resultados[1]  # mesma mensagem e mesmo código de fechamento, sem distinção


@pytest.mark.asyncio
async def test_a_common_user_still_registers(iniciar_servidor, sessao):  # noqa: F811 - fixtures importadas
    servidor = iniciar_servidor()
    banir(servidor, _snowflake())
    livre = _snowflake()

    ws = await _registrar_usuario(servidor, sessao, [livre], _token_do_app())

    assert livre in web_app.users_ws and await _fechamento(ws, ASSENTAR) is None


@pytest.mark.asyncio
async def test_the_ban_closes_an_open_connection_with_the_new_reason(iniciar_servidor, sessao):  # noqa: F811 - fixtures importadas
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


def test_a_banned_id_never_reaches_forget_replace_or_the_relay():
    """Uma conexão que JÁ registrou e reenvia a lista com um ID banido: só o ID livre passa por ``_forget_users``, ``_replace_session`` e o repasse."""
    chamadas = []
    handler = web_app.WebSocketHandler.__new__(web_app.WebSocketHandler)
    banido, livre = 200000000000000002, 100000000000000001
    handler.is_blocked = lambda user_id: user_id == banido
    handler._disconnect = lambda motivo: chamadas.append(("disconnect", motivo))
    handler._reject = lambda *a: chamadas.append(("reject", a))
    handler._forget_users = lambda: chamadas.append("forget")
    handler._replace_session = lambda user_id: chamadas.append(("replace", user_id))
    handler._relay_registration = lambda data: chamadas.append(("relay", list(handler.user_ids)))

    handler._register_user({"user_ids": [livre, banido], "token": "t" * 50, "version": "99.0.0"})
    handler._register_user({"user_ids": [banido], "token": "t" * 50, "version": "99.0.0"})

    assert chamadas == [
        "forget",
        ("replace", livre),
        ("relay", [livre]),
    ]  # a lista só com banido não faz nada, nem fecha
