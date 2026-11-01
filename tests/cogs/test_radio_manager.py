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
    pool = SimpleNamespace(database=db, get_all_bots=lambda: [])
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


class FakeChatPorts:
    stage = False

    def __init__(self):
        self.t = 1_000_000.0
        self.sleeps = []

    def now(self):
        return self.t

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        await asyncio.sleep(0.01)

    async def fetch_recent(self, limit):
        return []

    async def bulk_delete(self, ids):
        return None


class TestChatSweeperLifecycle:
    @pytest.mark.asyncio
    async def test_the_sweeper_runs_while_the_radio_is_on_and_stops_with_it(self, world):
        chat = FakeChatPorts()
        world.manager.chat_ports_factory = lambda radio: chat
        r = await enabled_radio(world)

        await world.manager.activate(r)
        await asyncio.sleep(0.05)
        assert chat.sleeps and r.id in world.manager._sweepers

        await world.manager.deactivate(r.id)
        sleeps = len(chat.sleeps)
        await asyncio.sleep(0.05)

        assert r.id not in world.manager._sweepers and len(chat.sleeps) == sleeps
        await world.manager.close()

    @pytest.mark.asyncio
    async def test_without_chat_ports_there_is_no_sweeper(self, world):
        r = await enabled_radio(world)

        await world.manager.activate(r)

        assert world.manager._sweepers == {}

    @pytest.mark.asyncio
    async def test_new_chat_rules_from_the_web_page_reach_the_running_sweeper(self, world):
        world.manager.chat_ports_factory = lambda radio: FakeChatPorts()
        r = await enabled_radio(world)
        await world.manager.activate(r)

        updated = await world.store.update_chat(r.id, {"voice_delete_after_s": 90}, expected_rev=r.rev)
        world.manager.update_chat_rules(updated)

        assert world.manager._sweepers[r.id][0].chat["voice_delete_after_s"] == 90
        await world.manager.close()

    @pytest.mark.asyncio
    async def test_closing_cancels_every_sweeper(self, world):
        world.manager.chat_ports_factory = lambda radio: FakeChatPorts()
        await world.manager.activate(await enabled_radio(world, 1))
        await world.manager.activate(await enabled_radio(world, 2))

        await world.manager.close()

        assert world.manager._sweepers == {} and not world.manager._tasks


class TestStoppingForReal:
    @pytest.mark.asyncio
    async def test_deactivating_before_a_queued_start_runs_means_it_never_connects(self, world):
        r = await enabled_radio(world)

        await world.manager.activate(r, queued=True)
        assert world.manager.is_running(r.id)  # já conta como rodando, mesmo esperando a vez na fila
        await world.manager.deactivate(r.id)
        while world.manager._tasks:
            await asyncio.gather(*list(world.manager._tasks), return_exceptions=True)

        assert world.ports.connects == 0 and not world.manager.is_running(r.id)

    @pytest.mark.asyncio
    async def test_deactivating_in_the_middle_of_the_backoff_never_reconnects(self, world):
        r = await enabled_radio(world)
        world.ports.connect_results.extend([False, False, True])
        original = world.ports.sleep

        async def sleep_then_deactivate(seconds):
            await original(seconds)
            await world.manager.deactivate(r.id)

        world.ports.sleep = sleep_then_deactivate

        await world.manager.activate(r)

        assert world.ports.connects == 1 and world.ports.played == []

    @pytest.mark.asyncio
    async def test_activate_without_waiting_returns_before_a_slow_start_finishes(self, world):
        r = await enabled_radio(world)
        gate = asyncio.Event()
        original = world.ports.connect

        async def slow_connect():
            await gate.wait()
            return await original()

        world.ports.connect = slow_connect

        await asyncio.wait_for(world.manager.activate(r, wait=False), 1)  # não prende a resposta do comando
        assert world.ports.connects == 0
        gate.set()
        while world.manager._tasks:
            await asyncio.gather(*list(world.manager._tasks))

        assert world.ports.connects == 1

    @pytest.mark.asyncio
    async def test_a_radio_that_keeps_failing_at_boot_does_not_hold_the_queue(self, world):
        await world.hub.premium.grant(GUILD, by=1)
        radios = []
        for bot in (1, 2, 3):
            created = await world.store.create(radio(bot))
            radios.append(await world.store.set_enabled(created.id, True))
        world.ports.connect_results.extend([False] * 2)  # duas primeiras tentativas falham

        for r in radios:
            await world.manager.activate(r, queued=True)
        await asyncio.sleep(0.05)
        # as três subiram: as duas que falharam recuam fora da fila (e voltam depois do recuo)
        assert world.ports.connects >= 3
        for r in radios:
            await world.manager.deactivate(r.id)
        while world.manager._tasks:
            await asyncio.gather(*list(world.manager._tasks), return_exceptions=True)


class TestRevokeStopsWhoIsPlaying:
    @pytest.mark.asyncio
    async def test_revoking_the_premium_stops_the_runners_and_destroys_the_players(self, world):
        destroyed = []
        players = {}
        for bot in (1, 2):
            await world.manager.activate(await enabled_radio(world, bot))
            players[bot] = SimpleNamespace(destroy=lambda b=bot: _async_append(destroyed, b))
        world.hub.store.pool.get_all_bots = lambda: [
            SimpleNamespace(user=SimpleNamespace(id=b), music=SimpleNamespace(players={GUILD: players[b]}))
            for b in players
        ]

        off = await world.manager.revoke_premium(GUILD)

        assert off == 2 and sorted(destroyed) == [1, 2]
        assert world.manager.counts() == (0, 0, 0) and world.hub.service.state(radio_id(1, GUILD)) is None
        assert world.ports.stops >= 2

    @pytest.mark.asyncio
    async def test_even_when_the_database_fails_midway_nothing_keeps_playing(self, world):
        await world.manager.activate(await enabled_radio(world))

        async def broken(guild_id, updated_by="owner"):
            raise RuntimeError("banco caiu")

        world.hub.store.revoke_premium = broken

        with pytest.raises(RuntimeError):
            await world.manager.revoke_premium(GUILD)

        assert world.manager.counts() == (0, 0, 0)


async def _async_append(items, value):
    items.append(value)


class TestTicks:
    @pytest.mark.asyncio
    async def test_one_stuck_radio_does_not_hold_the_others(self, world, monkeypatch, caplog):
        from empiremuse.utils.radio import manager as manager_module

        monkeypatch.setattr(manager_module, "TICK_TIMEOUT_S", 0.05)
        await world.manager.activate(await enabled_radio(world, 1))
        await world.manager.activate(await enabled_radio(world, 2))
        stuck, fine = (world.manager.runner(radio_id(b, GUILD)) for b in (1, 2))
        calls = []

        async def hang():
            await asyncio.sleep(10)

        stuck.tick = hang
        fine.tick = lambda: _record(calls)

        with caplog.at_level(logging.WARNING):
            await asyncio.wait_for(world.manager.tick_all(), 2)

        assert calls == ["tick"]


class TestBootRetry:
    @pytest.mark.asyncio
    async def test_a_transient_failure_at_boot_is_retried_with_a_growing_wait(self, world, monkeypatch):
        from empiremuse.utils.radio import manager as manager_module

        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(manager_module.asyncio, "sleep", fake_sleep)
        attempts = {"n": 0}

        async def flaky():
            attempts["n"] += 1
            if attempts["n"] < 4:
                raise RuntimeError("banco fora")
            return 7

        world.manager.resume_all = flaky

        assert await world.manager.resume_all_with_retry() == 7
        assert sleeps == [30.0, 60.0, 120.0]


class TestBackgroundErrors:
    @pytest.mark.asyncio
    async def test_a_background_task_that_dies_leaves_the_error_type_in_the_log(self, world, caplog):
        async def dies():
            raise ValueError("segredo")

        with caplog.at_level(logging.WARNING):
            task = world.manager._spawn(dies())
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)

        assert "ValueError" in caplog.text and "segredo" not in caplog.text
