"""T-407-F2: WebSocket de Lavalink usa a mesma autoridade IPv6 válida que o REST."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from empiremuse.wavelink.websocket import WebSocket

pytestmark = [pytest.mark.critical, pytest.mark.regression]


@pytest.mark.parametrize(
    "host,version,secure,expected",
    [
        ("::1", 3, False, "ws://[::1]:8090"),
        ("::1", 4, True, "wss://[::1]:8090/v4/websocket"),
        ("[::1]", 4, False, "ws://[::1]:8090/v4/websocket"),
        ("127.0.0.1", 4, False, "ws://127.0.0.1:8090/v4/websocket"),
    ],
)
def test_websocket_connects_to_valid_node_url(host, version, secure, expected):
    bot = SimpleNamespace(wait_until_ready=AsyncMock(), dispatch=Mock())
    connect = AsyncMock(return_value=SimpleNamespace(closed=False))
    node = SimpleNamespace(
        _client=SimpleNamespace(bot=bot),
        version=version,
        password="synthetic-password",
        uid=1,
        resume_key="synthetic-resume",
        session_id=None,
        heartbeat=30,
        session=SimpleNamespace(ws_connect=connect),
    )
    websocket = WebSocket(node=node, host=host, port=8090, secure=secure)
    websocket._task = object()  # não inicia o listener de rede
    headers = websocket.headers

    asyncio.run(websocket._connect())

    connect.assert_awaited_once_with(expected, headers=headers, heartbeat=30)
    assert websocket.is_connected
