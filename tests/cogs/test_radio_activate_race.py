"""Ativação concorrente da rádio e fallback do relógio em memória."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from tests.cogs.radio_config import configure
from tests.cogs.test_radio_pr_b import world, GUILD, CHANNEL
from tests.cogs.test_radio_runner import FakePorts

pytestmark = [pytest.mark.critical, pytest.mark.asyncio]


@pytest.mark.regression
async def test_concurrent_activations_register_one_runner_and_deactivate_stops_every_runner(monkeypatch):
    w = await world()
    radio = await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2, source={"playlist": "p"})
    radio = await w.hub.store.set_enabled(radio.id, True)
    assert radio.clock_started_at is not None
    manager = w.hub.manager
    original_get, original_factory = w.hub.store.get, manager._runner_for
    both_read = asyncio.Event()
    reads, runners = [], []

    async def get(rid):
        result = await original_get(rid)
        reads.append(rid)
        if len(reads) == 2:
            both_read.set()
        await both_read.wait()
        return result

    def factory(value):
        runner = original_factory(value)
        runners.append(runner)
        return runner

    manager.ports_factory = lambda value: FakePorts()
    monkeypatch.setattr(w.hub.store, "get", get)
    monkeypatch.setattr(manager, "_runner_for", factory)
    try:
        await asyncio.gather(manager.activate(radio), manager.activate(radio))
        assert len(runners) == 1
        assert manager.is_running(radio.id)
        await manager.deactivate(radio.id)
        assert not manager.is_running(radio.id)
        assert all(runner.stopped and runner.ports.stops == 1 for runner in runners)
    finally:
        for runner in runners:
            if not runner.stopped:
                await runner.stop()
        await manager.close()


async def test_missing_persisted_clock_uses_the_in_memory_origin_after_a_successful_read(monkeypatch, caplog):
    w = await world()
    radio = await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2, source={"playlist": "p"})
    w.db.tables["radios"][radio.id]["enabled"] = True  # documento legado ligado, sem época persistida
    radio = await w.hub.store.get(radio.id)
    assert radio.enabled and radio.clock_started_at is None
    monkeypatch.setattr(w.hub.store, "ensure_clock", AsyncMock(side_effect=ConnectionError("synthetic detail")))
    try:
        await w.hub.manager.activate(radio)
        runner = w.hub.manager._runners[radio.id]
        assert runner.virtual_origin == w.ports.wall()
        assert w.ports.played == [(0, 0)]
        assert (await w.hub.store.get(radio.id)).clock_started_at is None
        assert "ConnectionError" in caplog.text and "synthetic detail" not in caplog.text
    finally:
        await w.hub.manager.deactivate(radio.id)
        await w.hub.manager.close()
