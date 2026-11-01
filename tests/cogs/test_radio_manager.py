"""Quem faz as rádios rodarem (item 8, R1): ativar e desativar, a retomada na subida pela fila global (R-3), a pausa geral do dono e o
resumo. As portas são dublês; o banco é o em memória dos testes da store."""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from empiremuse.rules.radio import check_radio, radio_id
from empiremuse.utils.radio import RadioHub
from empiremuse.utils.radio.runner import Phase
from tests.cogs.test_radio_runner import FakePorts
from tests.persistence.test_radio_store import FakeDb

pytestmark = pytest.mark.critical

GUILD = 500


@pytest.fixture
def world():
    db = FakeDb()
    pool = SimpleNamespace(database=db)
    hub = RadioHub(pool)
    shared = FakePorts()
    hub.manager.ports_factory = lambda radio: shared
    return SimpleNamespace(db=db, hub=hub, manager=hub.manager, ports=shared, store=hub.store)


def radio(bot_id=1, guild_id=GUILD):
    made = check_radio(
        {"_id": radio_id(bot_id, guild_id), "channel_id": "9", "kind": "custom", "source": {"playlist": "p"}}
    )
    assert not isinstance(made, tuple)
    return made


async def enabled_radio(world, bot_id=1, guild_id=GUILD):
    await world.hub.premium.grant(guild_id, by=1)
    created = await world.store.create(radio(bot_id, guild_id))
    return await world.store.set_enabled(created.id, True)


class TestActivate:
    @pytest.mark.asyncio
    async def test_activating_starts_the_runner_and_publishes_the_state(self, world):
        r = await enabled_radio(world)

        await world.manager.activate(r)

        assert world.manager.is_running(r.id) and world.hub.service.state(r.id).playing is True
        assert world.manager.counts() == (1, 1, 0)

    @pytest.mark.asyncio
    async def test_activating_twice_is_a_no_op(self, world):
        r = await enabled_radio(world)

        await world.manager.activate(r)
        await world.manager.activate(r)

        assert world.ports.connects == 1

    @pytest.mark.asyncio
    async def test_deactivating_stops_the_stream_and_forgets_the_state(self, world):
        r = await enabled_radio(world)
        await world.manager.activate(r)

        await world.manager.deactivate(r.id)

        assert not world.manager.is_running(r.id) and world.hub.service.state(r.id) is None
        assert world.ports.stops == 1 and world.manager.counts() == (0, 0, 0)

    @pytest.mark.asyncio
    async def test_without_the_discord_adapter_nothing_starts_and_it_says_so(self, world, caplog):
        world.manager.ports_factory = None
        r = await enabled_radio(world)

        with caplog.at_level(logging.WARNING):
            await world.manager.activate(r)

        assert not world.manager.is_running(r.id) and "adaptador" in caplog.text


class TestBootResume:
    @pytest.mark.asyncio
    async def test_thirty_radios_come_back_two_at_a_time_with_a_second_between_and_one_info_line(self, world, caplog):
        for guild in range(1, 11):  # 10 servidores x 3 rádios
            await world.hub.premium.grant(guild, by=1)
            for bot in (1, 2, 3):
                created = await world.store.create(radio(bot, guild))
                await world.store.set_enabled(created.id, True)

        with caplog.at_level(logging.INFO, logger="empiremuse.utils.radio.manager"):
            queued = await world.manager.resume_all()
            while world.manager._tasks:
                await asyncio.gather(*list(world.manager._tasks))

        assert queued == 30 and world.ports.connects == 30
        assert world.ports.max_in_flight <= 2
        assert world.ports.sleeps.count(1.0) == 30
        assert len([r for r in caplog.records if "radio.toggle" in r.getMessage()]) == 1

    @pytest.mark.asyncio
    async def test_boot_first_reconciles_so_an_excess_never_starts(self, world):
        await world.hub.premium.grant(GUILD, by=1)
        for bot in range(1, 6):
            await world.store.create(radio(bot))
            world.db.tables["radios"][radio_id(bot, GUILD)]["enabled"] = True  # ligadas por fora do contador

        queued = await world.manager.resume_all()
        while world.manager._tasks:
            await asyncio.gather(*list(world.manager._tasks))

        assert queued == 3 and world.ports.connects == 3
        assert (await world.hub.premium.fresh(GUILD)).active == 3

    @pytest.mark.asyncio
    async def test_a_guild_without_premium_never_starts_on_boot(self, world):
        await world.store.create(radio())
        world.db.tables["radios"][radio_id(1, GUILD)]["enabled"] = True

        queued = await world.manager.resume_all()

        assert queued == 0 and world.ports.connects == 0
        assert not (await world.store.get(radio_id(1, GUILD))).enabled

    @pytest.mark.asyncio
    async def test_boot_loads_the_pause_all_flag_and_starts_paused_radios_silent(self, world):
        await world.hub.control.set_paused_all(True, by=1)
        fresh = RadioHub(world.hub.premium.pool)
        world.ports.paused_all = lambda: fresh.control.paused_all  # o adaptador real lê a flag do controle
        fresh.manager.ports_factory = lambda r: world.ports
        await enabled_radio(world)

        await fresh.manager.resume_all()
        while fresh.manager._tasks:
            await asyncio.gather(*list(fresh.manager._tasks))

        assert fresh.control.paused_all is True and world.ports.played == []
        assert fresh.manager.counts() == (1, 0, 1)


class TestPauseAll:
    @pytest.mark.asyncio
    async def test_pausing_everything_stops_every_running_radio_and_keeps_them_in_the_call(self, world):
        for bot in (1, 2):
            await world.manager.activate(await enabled_radio(world, bot))
        world.ports.paused_all = lambda: world.hub.control.paused_all

        await world.hub.control.set_paused_all(True, by=1)
        while world.manager._tasks:
            await asyncio.gather(*list(world.manager._tasks))

        assert all(world.manager.runner(radio_id(b, GUILD)).phase is Phase.PAUSED_OWNER for b in (1, 2))
        assert world.manager.counts() == (2, 0, 2) and world.ports.connects == 2

    @pytest.mark.asyncio
    async def test_a_broken_radio_does_not_stop_the_others_in_the_tick(self, world, caplog):
        await world.manager.activate(await enabled_radio(world, 1))
        await world.manager.activate(await enabled_radio(world, 2))
        runners = [world.manager.runner(radio_id(b, GUILD)) for b in (1, 2)]

        async def boom():
            raise RuntimeError("defeito")

        calls = []
        runners[0].tick = boom
        runners[1].tick = lambda: _record(calls)

        with caplog.at_level(logging.WARNING):
            await world.manager.tick_all()

        assert calls == ["tick"] and "RuntimeError" in caplog.text


async def _record(calls):
    calls.append("tick")


class TestClose:
    @pytest.mark.asyncio
    async def test_closing_cancels_the_background_work_and_forgets_the_runners(self, world):
        r = await enabled_radio(world)
        await world.manager.activate(r)

        await world.manager.close()

        assert world.manager.counts() == (0, 0, 0) and not world.manager._tasks
