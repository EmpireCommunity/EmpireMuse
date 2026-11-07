"""T-407: exercita os pontos de entrega com HTTP e Discord dublados."""

import asyncio
import secrets
import string
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.cogs.music_settings import RPCCog
from empiremuse.utils import client
from tests.cogs.test_ytoauth_dm import Cenario

pytestmark = [pytest.mark.critical, pytest.mark.regression]

HOSTS = [
    ("127.0.0.1", True),
    ("::1", True),
    ("localhost", True),
    ("lavalink", False),
    ("192.168.1.2", False),
    ("203.0.113.1", False),
    ("localhost.example.invalid", False),
    ("127.0.0.1.example.invalid", False),
]


@pytest.mark.parametrize("host,allowed", HOSTS)
@pytest.mark.parametrize("identifier", ["LOCAL", "REMOTE"])
def test_startup_delivers_only_to_loopback_local(monkeypatch, host, allowed, identifier):
    monkeypatch.setattr(client.asyncio, "sleep", AsyncMock())

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self):
            return {"plugins": [{"name": "youtube-plugin"}]}

    post = AsyncMock(return_value=SimpleNamespace(status=204))

    class Session(Response):
        def get(self, *args, **kwargs):
            return Response()

    Session.post = post
    monkeypatch.setattr(client.aiohttp, "ClientSession", Session)
    monkeypatch.setattr(client, "ClientSession", Session)
    collection = SimpleNamespace(find_one=AsyncMock(return_value={"refresh_tokens": {"owner": "synthetic-test-value"}}))
    pool = client.BotPool.__new__(client.BotPool)
    pool.mongo_database = SimpleNamespace(_connect={"global": {"global": collection}})
    pool.get_all_bots = lambda: []

    asyncio.run(
        pool.check_node({"host": host, "port": 8090, "password": "test", "identifier": identifier, "retries": 1})
    )

    assert post.await_count == int(allowed and identifier == "LOCAL")
    if post.await_count:
        assert post.call_args.args[0].endswith("/youtube")


@pytest.mark.parametrize("host,allowed", HOSTS)
def test_oauth_command_delivers_only_to_loopback_local(monkeypatch, tmp_path, host, allowed):
    scenario = Cenario(monkeypatch, tmp_path, com_efeitos=True)
    scenario.no_local.host = host
    scenario.no_local.rest_uri = f"http://{host}:8090"
    scenario.sessao.post.return_value = SimpleNamespace(status=204)

    scenario.rodar()

    assert scenario.erro is None
    assert scenario.sessao.post.await_count == int(allowed)
    if allowed:
        assert scenario.sessao.post.call_args.args[0].endswith("/youtube")


def test_rpc_generation_uses_secrets_choice(monkeypatch):
    calls = []

    def choose(alphabet):
        calls.append(alphabet)
        return "Z"

    monkeypatch.setattr(secrets, "choice", choose)
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

    assert calls == [string.ascii_letters + string.digits] * 50
    assert data["token"] == "Z" * 50
    bot.update_global_data.assert_awaited_once()
