"""E5 (R8.4): o RPC fecha para o banido (Q14): relé recusa o registro, ``_send_rpc_data`` o omite e o ban derruba a conexão aberta.

Servidor Tornado real em porta efêmera (a infraestrutura de ``test_rpc_websocket``); a lista de banidos é a do pool, com banco falso.
"""

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
    _registrar_usuario,
    _snowflake,
    _token_do_app,
    iniciar_servidor,
    sessao,
)

pytestmark = pytest.mark.critical

DONO = 100000000000000009


def banir(servidor, *ids):
    bans = servidor.pool.user_bans
    bans._records = {uid: UserBanRecord(uid, 1, "m", DONO) for uid in ids}
    bans._loaded = True
    return bans


async def mensagens_ate_fechar(ws) -> list[dict]:
    textos = []
    while True:
        msg = await ws.receive(timeout=2.0)
        if msg.type == aiohttp.WSMsgType.TEXT:
            textos.append(json.loads(msg.data))
        else:
            return textos


@pytest.mark.asyncio
async def test_a_list_with_a_banned_id_is_refused_whole_and_does_not_drop_another_session(iniciar_servidor, sessao):  # noqa: F811 - fixtures importadas
    servidor = iniciar_servidor()
    banido, livre = _snowflake(), _snowflake()
    banir(servidor, banido)
    ws_livre = await _registrar_usuario(servidor, sessao, [livre], _token_do_app())
    assert web_app.users_ws[livre] is not None

    ws_misto = await _registrar_usuario(servidor, sessao, [banido, livre], _token_do_app())  # lista mista

    textos = await mensagens_ate_fechar(ws_misto)
    assert textos == [{"op": "disconnect", "reason": RPC_BLOCKED_REASON}]
    assert ws_misto.close_code == rpc_protocol.CLOSE_REFUSED
    assert banido not in web_app.users_ws
    assert (
        web_app.users_ws[livre] is not None and await _fechamento(ws_livre, ASSENTAR) is None
    )  # a sessão do outro segue


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


def test_a_banned_id_is_refused_before_the_registration_changes_anything():
    """Uma conexão que JÁ registrou e reenvia a lista com um ID banido: o portão vem antes de ``_forget_users`` e ``_replace_session``."""
    chamadas = []
    handler = web_app.WebSocketHandler.__new__(web_app.WebSocketHandler)
    handler.is_blocked = lambda user_id: user_id == 200000000000000002
    handler._disconnect = lambda motivo: chamadas.append(("disconnect", motivo))
    handler._forget_users = lambda: chamadas.append("forget")
    handler._replace_session = lambda user_id: chamadas.append(("replace", user_id))
    handler._relay_registration = lambda data: chamadas.append("relay")
    handler.user_ids = [100000000000000001]  # já registrada antes

    handler._register_user(
        {"user_ids": [100000000000000001, 200000000000000002], "token": "t" * 50, "version": "99.0.0"}
    )

    assert chamadas == [("disconnect", RPC_BLOCKED_REASON)]
    assert handler.user_ids == [100000000000000001]
