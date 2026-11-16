"""O encerramento do pool fecha as rádios (CodeRabbit #200): ticker, health check, relay e tarefas param antes de os bots fecharem."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.regression
@pytest.mark.asyncio
async def test_the_pool_shutdown_closes_the_radios_before_closing_the_bots():
    from empiremuse.utils.client import BotPool

    order = []
    closing_bot = SimpleNamespace(is_closed=lambda: False, close=AsyncMock(side_effect=lambda: order.append("bot")))
    pool = SimpleNamespace(
        heartbeat_task=None,
        logs_task=None,
        update_watch_task=None,
        version_check_task=None,
        playlist_icons_task=None,
        helper_watch_tasks=set(),
        jukebox_panel=None,
        i18n=SimpleNamespace(close=lambda: None),
        get_all_bots=lambda: [closing_bot],
        lavalink_instance=None,
        usage=None,
        user_bans=None,
        radio=SimpleNamespace(manager=SimpleNamespace(close=AsyncMock(side_effect=lambda: order.append("radio")))),
    )

    await BotPool.shutdown(pool)

    assert order == ["radio", "bot"]
