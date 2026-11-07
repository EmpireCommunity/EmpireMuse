"""T-407: pontos de entrega com HTTP e Discord dublados; a matriz de hosts fica na regra pura."""

import asyncio
import logging
import secrets
import string
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.cogs.music_settings import RPCCog
from empiremuse.utils import client
from tests.cogs.test_ytoauth_dm import Cenario, REFRESH

pytestmark = [pytest.mark.critical, pytest.mark.regression]

TOKEN = "synthetic-test-value"
OTHER_TOKEN = "synthetic-other-value"
HEADERS = {"Authorization": "test-password"}


@pytest.mark.parametrize(
    "host,identifier,allowed,status",
    [
        ("::1", "LOCAL", True, 204),
        ("203.0.113.1", "LOCAL", False, 204),
        ("::1", "REMOTE", False, 204),
        ("::1", "LOCAL", True, 302),
    ],
)
def test_startup_delivery_registers_tokens_before_gate(monkeypatch, caplog, host, identifier, allowed, status):
    from empiremuse.rules.lavalink import is_loopback_host

    monkeypatch.setattr(client.asyncio, "sleep", AsyncMock())
    events = []
    monkeypatch.setattr(client, "register_secret", lambda token: events.append(("registered", token)))

    def gate(host):
        events.append(("gate", host))
        return is_loopback_host(host)

    monkeypatch.setattr(client, "is_loopback_host", gate)

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self):
            return {"plugins": [{"name": "youtube-plugin"}]}

    post = AsyncMock(return_value=SimpleNamespace(status=status))

    class Session(Response):
        def get(self, *args, **kwargs):
            assert args[0] == ("http://[::1]:8090/v4/info" if host == "::1" else f"http://{host}:8090/v4/info")
            return Response()

    Session.post = post
    monkeypatch.setattr(client.aiohttp, "ClientSession", Session)
    monkeypatch.setattr(client, "ClientSession", Session)
    collection = SimpleNamespace(
        find_one=AsyncMock(return_value={"refresh_tokens": {"owner": TOKEN, "other": OTHER_TOKEN}})
    )
    pool = client.BotPool.__new__(client.BotPool)
    pool.mongo_database = SimpleNamespace(_connect={"global": {"global": collection}})
    pool.get_all_bots = lambda: []

    with caplog.at_level(logging.WARNING):
        asyncio.run(
            pool.check_node(
                {
                    "host": host,
                    "port": 8090,
                    "password": HEADERS["Authorization"],
                    "identifier": identifier,
                    "retries": 1,
                }
            )
        )

    assert events == (
        [("registered", TOKEN), ("registered", OTHER_TOKEN), ("gate", host)] if identifier == "LOCAL" else []
    )
    if allowed:
        post.assert_awaited_once_with(
            "http://[::1]:8090/youtube", headers=HEADERS, json={"refreshToken": TOKEN}, allow_redirects=False
        )
        if status == 302:
            assert "HTTP 302" in caplog.text
            assert "aplicado no servidor" not in caplog.text
    else:
        post.assert_not_awaited()
        if identifier == "LOCAL":
            assert "LOCAL" in caplog.text and host not in caplog.text
        assert TOKEN not in caplog.text and OTHER_TOKEN not in caplog.text


@pytest.mark.parametrize(
    "nodes,status", [("allowed", 204), ("refused", 204), ("remote", 204), ("empty", 204), ("allowed", 302)]
)
def test_oauth_command_delivery(monkeypatch, tmp_path, caplog, nodes, status):
    scenario = Cenario(monkeypatch, tmp_path, com_efeitos=True)
    scenario.no_local.host = "203.0.113.1" if nodes == "refused" else "::1"
    scenario.no_local.identifier = "LOCAL"
    scenario.no_local.rest_uri = "http://[::1]:8090"
    scenario.no_local._websocket.headers = HEADERS
    if nodes == "remote":
        scenario.no_local.identifier = "REMOTE"
        scenario.cog.bot.music.nodes = {"REMOTE": scenario.no_local}
    elif nodes == "empty":
        scenario.cog.bot.music.nodes = {}
    scenario.sessao.post.return_value = SimpleNamespace(status=status, text=AsyncMock(return_value=REFRESH))

    with caplog.at_level(logging.WARNING):
        scenario.rodar()

    assert scenario.erro is None
    if nodes == "allowed":
        scenario.sessao.post.assert_awaited_once_with(
            "http://[::1]:8090/youtube", headers=HEADERS, json={"refreshToken": REFRESH}, allow_redirects=False
        )
    else:
        scenario.sessao.post.assert_not_awaited()
        assert "não foi aplicado" in scenario.dm_texto()
        assert "configurado automaticamente" not in scenario.dm_texto()
    if status == 302:
        assert "HTTP 302" in caplog.text and REFRESH not in caplog.text
        assert "configurado automaticamente" not in scenario.dm_texto()
    if nodes == "refused":
        assert "LOCAL" in caplog.text and scenario.no_local.host not in caplog.text and REFRESH not in caplog.text


@pytest.mark.parametrize("entrypoint", ["command", "panel"])
def test_rpc_generation_uses_secrets_choice(monkeypatch, entrypoint):
    calls = []

    def choose(alphabet):
        calls.append(alphabet)
        return "Z"

    monkeypatch.setattr(secrets, "choice", choose)
    if entrypoint == "panel":
        from empiremuse.panel.services.account_misc import RpcTokenSection

        parsed = asyncio.run(RpcTokenSection().parse(None, {}, "rpc_token_gerar", {}))
        assert parsed.new["token"] == "Z" * 50
    else:
        data = {"token": ""}
        bot = SimpleNamespace(get_global_data=AsyncMock(return_value=data), update_global_data=AsyncMock())
        cog = RPCCog.__new__(RPCCog)
        cog.bot = bot
        interaction = SimpleNamespace(
            data=SimpleNamespace(custom_id="rpc_gen.42"),
            author=SimpleNamespace(id=42),
            response=SimpleNamespace(defer=AsyncMock()),
            message=SimpleNamespace(flags=SimpleNamespace(ephemeral=True)),
            edit_original_message=AsyncMock(),
        )

        asyncio.run(cog.rpc_button_event(interaction))

        assert data["token"] == "Z" * 50
        bot.update_global_data.assert_awaited_once()
    assert calls == [string.ascii_letters + string.digits] * 50


def test_connected_node_gets_a_bracketed_ipv6_rest_url():
    from empiremuse.music_cog.nodes import NodesMixin

    node = SimpleNamespace(connect=AsyncMock())
    initiate = AsyncMock(return_value=node)
    cog = SimpleNamespace(bot=SimpleNamespace(music=SimpleNamespace(nodes={}, initiate_node=initiate)))

    asyncio.run(NodesMixin.connect_node(cog, {"identifier": "LOCAL", "host": "::1", "port": 8090, "retries": 1}))

    assert initiate.call_args.kwargs["rest_uri"] == "http://[::1]:8090"
