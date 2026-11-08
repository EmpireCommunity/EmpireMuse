"""T-417 Keystone: entrada de reserva não cria documentos por bot nem mexe no documento único."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from empiremuse.cogs import misc
from empiremuse.utils.db import DBModel, db_models

pytestmark = [pytest.mark.asyncio, pytest.mark.regression]


async def test_guild_join_never_reads_or_writes_settings_and_keeps_notifications(monkeypatch):
    guild = SimpleNamespace(id=500)
    docs = {("servers", "global", "500"): {"jukebox": {"channel": 900}, "settings": {"prefix": "?"}}}
    before = deepcopy(docs)

    async def update(gid, data, *, db_name, collection):
        docs[(db_name, collection, str(gid))] = deepcopy(data)

    database = SimpleNamespace(
        get_data=AsyncMock(return_value=deepcopy(db_models[DBModel.guilds])),
        update_data=AsyncMock(side_effect=update),
    )
    bot = SimpleNamespace(
        identifier="TOKEN_2",
        user=SimpleNamespace(id=2, name="Reserva"),
        pool=SimpleNamespace(database=database),
        update_appinfo=AsyncMock(),
    )
    cog = misc.GuildLog.__new__(misc.GuildLog)
    cog.bot, cog.hook_url = bot, "https://example.invalid/hook"
    cog.roster_changed = Mock()
    cog.send_hook = AsyncMock()
    recorder = SimpleNamespace(record_guild=Mock())
    monkeypatch.setattr(misc, "usage_of", lambda owner: recorder)

    await cog.on_guild_join(guild)

    assert docs == before
    database.get_data.assert_not_awaited()
    database.update_data.assert_not_awaited()
    recorder.record_guild.assert_called_once_with("join", guild, bot=bot)
    cog.roster_changed.assert_called_once_with(500)
    cog.send_hook.assert_awaited_once()
    bot.update_appinfo.assert_awaited_once()
