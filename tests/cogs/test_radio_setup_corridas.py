"""T-417 L2: chamadas concorrentes do serviço compartilhado por chat e painel, com eventos e banco falso sem sleep."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.utils.radio import premium as premium_module
from empiremuse.utils.radio.store import RadioInvalid
from tests.cogs.radio_config import configure
from tests.cogs.test_radio_comandos import GUILD, build
from tests.cogs.test_radio_setup import ACTOR, new_radio

pytestmark = [pytest.mark.asyncio, pytest.mark.regression]


async def world(monkeypatch):
    w = build(free_bots=(2, 3, 4, 5))
    w.db._tick = AsyncMock()  # o entrelaçamento deste teste vem só dos Events, não do sleep do FakeDb
    w.hub.premium._clock = lambda: 100.0
    monkeypatch.setattr(premium_module, "time", SimpleNamespace(time=lambda: 100.0))
    await w.hub.premium.grant(GUILD, by=1)
    return w


async def started_call(started, operation):
    started.set()
    return await operation


@pytest.mark.parametrize("race", ["cap", "channel"])
async def test_concurrent_chat_and_panel_creates_keep_cap_and_unique_channel(monkeypatch, race):
    w = await world(monkeypatch)
    if race == "cap":
        for bot_id, channel_id in ((2, 800), (3, 801)):
            await configure(w.hub, bot_id=bot_id, channel_id=channel_id)
        args = ({"bot_id": 4, "channel_id": 802}, {"bot_id": 5, "channel_id": 803})
    else:
        args = ({"bot_id": 2}, {"bot_id": 3})
    entered, release, panel_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = w.hub.store.list_for_guild

    async def hold_first_snapshot(guild_id):
        snapshot = await original(guild_id)
        if not entered.is_set():
            entered.set()
            await release.wait()
        return snapshot

    monkeypatch.setattr(w.hub.store, "list_for_guild", hold_first_snapshot)
    chat = asyncio.create_task(new_radio(w, **args[0]), name="chat")
    await entered.wait()
    panel = asyncio.create_task(started_call(panel_started, new_radio(w, **args[1])), name="panel")
    await panel_started.wait()
    release.set()
    outcomes = await asyncio.gather(chat, panel, return_exceptions=True)

    refused = [result for result in outcomes if isinstance(result, RadioInvalid)]
    radios = await original(GUILD)
    assert len(radios) == (3 if race == "cap" else 1)
    assert len({radio.channel_id for radio in radios}) == len(radios)
    assert len(refused) == 1
    assert refused[0].error.code == ("max_radios" if race == "cap" else "channel_taken")


async def test_another_guild_progresses_while_the_first_guild_is_held(monkeypatch):
    w = await world(monkeypatch)
    other_guild = GUILD + 1
    await w.hub.premium.grant(other_guild, by=1)
    for bot in w.bots:
        guild = bot.get_guild(GUILD)
        bot.get_guild = lambda gid, guild=guild: guild if gid in (GUILD, other_guild) else None
    entered, release, other_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = w.hub.store.list_for_guild

    async def hold_first(guild_id):
        snapshot = await original(guild_id)
        if guild_id == GUILD:
            entered.set()
            await release.wait()
        return snapshot

    monkeypatch.setattr(w.hub.store, "list_for_guild", hold_first)
    first = asyncio.create_task(new_radio(w))
    await entered.wait()
    other = asyncio.create_task(
        started_call(
            other_started,
            w.hub.setup.configure(other_guild, ACTOR, radio_id=None, channel_id=900, bot_id=2, kind="custom", rev=None),
        )
    )
    try:
        await other_started.wait()
        assert other.done() and not first.done()
        assert (await other).guild_id == other_guild
    finally:
        release.set()
        await asyncio.gather(first, other)


async def test_reset_waits_even_while_configure_is_checking_the_actor(monkeypatch):
    w = await world(monkeypatch)
    entered, release, reset_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    check_actor = w.hub.setup.check_actor

    async def hold_actor(*args):
        entered.set()
        await release.wait()
        await check_actor(*args)

    monkeypatch.setattr(w.hub.setup, "check_actor", hold_actor)
    creating = asyncio.create_task(new_radio(w))
    await entered.wait()
    resetting = asyncio.create_task(started_call(reset_started, w.hub.setup.reset(GUILD, ACTOR, f"2:{GUILD}", 0)))
    await reset_started.wait()
    release.set()
    results = await asyncio.gather(creating, resetting, return_exceptions=True)

    assert not any(isinstance(result, BaseException) for result in results)
    assert await w.hub.store.list_for_guild(GUILD) == []


async def test_configure_waits_until_reset_has_deleted_the_old_radio(monkeypatch):
    w = await world(monkeypatch)
    radio = await new_radio(w)
    entered, release, configure_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    original_delete = w.hub.store.delete

    async def hold_delete(*args):
        entered.set()
        await release.wait()
        await original_delete(*args)

    monkeypatch.setattr(w.hub.store, "delete", hold_delete)
    resetting = asyncio.create_task(w.hub.setup.reset(GUILD, ACTOR, radio.id, radio.rev))
    await entered.wait()
    creating = asyncio.create_task(started_call(configure_started, new_radio(w, bot_id=3)))
    await configure_started.wait()
    release.set()
    results = await asyncio.gather(resetting, creating, return_exceptions=True)

    assert not any(isinstance(result, BaseException) for result in results)
    assert [radio.bot_id for radio in await w.hub.store.list_for_guild(GUILD)] == [3]
