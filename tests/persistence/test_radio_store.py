"""Store de rádios e premium (item 8, R1): o contador de 3 por servidor só muda em transição confirmada (R-2), a revogação desliga tudo
em todos os bots (R-1), o painel web só consegue mudar o chat (R-6) e a subida recalcula o contador."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from empiremuse.rules.errors import RuleError
from empiremuse.rules.radio import Radio, check_radio, radio_id
from empiremuse.utils.radio import RadioHub
from empiremuse.utils.radio.premium import CACHE_SECONDS, PremiumStore, RadioUnavailable
from empiremuse.utils.radio.store import NoPremium, NoRoom, RadioConflict, RadioInvalid, RadioNotFound

pytestmark = pytest.mark.critical

GUILD, OTHER_GUILD = 500, 600
CHANNEL = 900


class FakeDb:
    """Banco em memória com a mesma interface de ``find_doc``/``save_doc_cas``/``delete_data``/``iter_data``; cada acesso cede o laço
    (``sleep(0)``) para que ``gather`` entrelace de verdade as leituras e as gravações."""

    def __init__(self):
        self.tables: dict[str, dict[str, dict]] = {}
        self.fail = False

    def _table(self, name):
        return self.tables.setdefault(name, {})

    async def _tick(self):
        await asyncio.sleep(0)
        if self.fail:
            raise ConnectionError("mongo fora")

    async def find_doc(self, id_, *, db_name, collection):
        await self._tick()
        doc = self._table(db_name).get(str(id_))
        return deepcopy(doc) if doc else None

    async def save_doc_cas(self, id_, doc, *, expected_rev, db_name, collection):
        await self._tick()
        table, key = self._table(db_name), str(id_)
        stored = table.get(key)
        if expected_rev is None:
            if stored:
                return False
            table[key] = {**deepcopy(doc), "_id": key}
            return True
        if not stored or stored.get("rev") != expected_rev:
            return False
        stored.update({k: deepcopy(v) for k, v in doc.items() if k != "_id"})
        return True

    async def delete_data(self, id_, db_name, collection):
        await self._tick()
        self._table(db_name).pop(str(id_), None)

    async def iter_data(self, db_name, collection, filter=None):
        await self._tick()
        for doc in list(self._table(db_name).values()):
            if all(doc.get(k) == v for k, v in (filter or {}).items()):
                yield deepcopy(doc)


@pytest.fixture
def world():
    db = FakeDb()
    pool = SimpleNamespace(database=db)
    hub = RadioHub(pool)
    return SimpleNamespace(db=db, pool=pool, hub=hub, store=hub.store, premium=hub.premium)


def make_radio(bot_id=1, guild_id=GUILD, **over) -> Radio:
    doc = {
        "_id": radio_id(bot_id, guild_id),
        "channel_id": str(CHANNEL),
        "kind": "custom",
        "source": {"playlist": "Rock"},
        "chat": {},
        "enabled": False,
        "rev": 0,
        **over,
    }
    radio = check_radio(doc)
    assert not isinstance(radio, RuleError)
    return radio


async def with_premium(world, guild_id=GUILD):
    return await world.premium.grant(guild_id, by=1)


async def active(world, guild_id=GUILD):
    return (await world.premium.fresh(guild_id)).active


class TestPremium:
    @pytest.mark.asyncio
    async def test_a_guild_without_a_document_is_not_premium(self, world):
        assert await world.premium.get(GUILD) is None

    @pytest.mark.asyncio
    async def test_granting_twice_keeps_the_counter(self, world):
        await with_premium(world)
        await world.store.create(make_radio())
        await world.store.set_enabled(radio_id(1, GUILD), True)

        again = await world.premium.grant(GUILD, by=2)

        assert again.active == 1

    @pytest.mark.asyncio
    async def test_the_read_is_cached_for_thirty_seconds_and_fresh_skips_the_cache(self, world):
        now = [100.0]
        world.premium._clock = lambda: now[0]
        await world.premium.grant(GUILD, by=1)
        assert (await world.premium.get(GUILD)).active == 0
        await world.db.delete_data(GUILD, db_name="premium", collection="global")  # revogado por fora

        assert await world.premium.get(GUILD) is not None  # ainda no cache
        now[0] += CACHE_SECONDS + 1
        assert await world.premium.get(GUILD) is None

    @pytest.mark.asyncio
    async def test_a_database_failure_is_radio_unavailable_not_a_traceback(self, world):
        world.db.fail = True

        with pytest.raises(RadioUnavailable):
            await world.premium.get(GUILD)

    @pytest.mark.asyncio
    async def test_releasing_at_zero_stays_at_zero_and_reserving_stops_at_the_cap(self, world):
        await with_premium(world)

        await world.premium.release(GUILD)
        assert await active(world) == 0
        assert [await world.premium.reserve(GUILD) for _ in range(5)] == [True, True, True, False, False]
        assert await active(world) == 3

    @pytest.mark.asyncio
    async def test_releasing_or_reserving_without_a_premium_document_does_nothing(self, world):
        await world.premium.release(GUILD)

        assert await world.premium.reserve(GUILD) is False and await world.premium.get(GUILD) is None

    def test_premium_store_takes_the_pool_and_an_injectable_clock(self):
        assert PremiumStore(SimpleNamespace(database=None), clock=lambda: 1.0)._clock() == 1.0


class TestTheCapOfThreeIsAtomic:
    """R-2: o contador só muda na TRANSIÇÃO confirmada, em qualquer caminho."""

    @pytest.mark.asyncio
    async def test_enabling_the_same_radio_twice_counts_once(self, world):
        await with_premium(world)
        await world.store.create(make_radio())

        await asyncio.gather(*(world.store.set_enabled(radio_id(1, GUILD), True) for _ in range(2)))
        await world.store.set_enabled(radio_id(1, GUILD), True)

        assert await active(world) == 1

    @pytest.mark.asyncio
    async def test_two_simultaneous_disables_free_a_single_slot(self, world):
        await with_premium(world)
        for bot in (1, 2):
            await world.store.create(make_radio(bot))
            await world.store.set_enabled(radio_id(bot, GUILD), True)
        assert await active(world) == 2

        await asyncio.gather(*(world.store.set_enabled(radio_id(1, GUILD), False) for _ in range(2)))

        assert await active(world) == 1

    @pytest.mark.asyncio
    async def test_ten_simultaneous_enables_with_two_active_let_exactly_one_through(self, world):
        await with_premium(world)
        for bot in range(1, 13):
            await world.store.create(make_radio(bot))
        for bot in (1, 2):
            await world.store.set_enabled(radio_id(bot, GUILD), True)

        results = await asyncio.gather(
            *(world.store.set_enabled(radio_id(bot, GUILD), True) for bot in range(3, 13)), return_exceptions=True
        )

        assert sum(isinstance(r, Radio) for r in results) == 1
        assert sum(isinstance(r, NoRoom) for r in results) == 9
        assert await active(world) == 3
        enabled = [r for r in await world.store.list_for_guild(GUILD) if r.enabled]
        assert len(enabled) == 3  # as 9 recusadas voltaram a desligadas

    @pytest.mark.asyncio
    async def test_a_refused_enable_leaves_the_radio_disabled_and_the_counter_untouched(self, world):
        await with_premium(world)
        for bot in range(1, 5):
            await world.store.create(make_radio(bot))
        for bot in (1, 2, 3):
            await world.store.set_enabled(radio_id(bot, GUILD), True)

        with pytest.raises(NoRoom):
            await world.store.set_enabled(radio_id(4, GUILD), True)

        assert not (await world.store.get(radio_id(4, GUILD))).enabled and await active(world) == 3

    @pytest.mark.asyncio
    async def test_without_premium_enabling_is_refused_and_leaves_no_trace(self, world):
        await world.store.create(make_radio())

        with pytest.raises(NoPremium):
            await world.store.set_enabled(radio_id(1, GUILD), True)

        assert not (await world.store.get(radio_id(1, GUILD))).enabled

    @pytest.mark.asyncio
    async def test_disabling_a_radio_that_was_never_enabled_does_not_go_below_zero(self, world):
        await with_premium(world)
        await world.store.create(make_radio())

        await world.store.set_enabled(radio_id(1, GUILD), False)

        assert await active(world) == 0

    @pytest.mark.asyncio
    async def test_a_missing_radio_is_not_found(self, world):
        with pytest.raises(RadioNotFound):
            await world.store.set_enabled(radio_id(9, GUILD), True)

    @pytest.mark.asyncio
    async def test_deleting_an_enabled_radio_gives_the_slot_back(self, world):
        await with_premium(world)
        await world.store.create(make_radio())
        await world.store.set_enabled(radio_id(1, GUILD), True)

        await world.store.delete(radio_id(1, GUILD))

        assert await active(world) == 0 and await world.store.get(radio_id(1, GUILD)) is None


class TestRevocation:
    """R-1: apagar o premium e desligar TODAS as rádios do servidor, em todos os bots; o contador de outro servidor não se mexe."""

    @pytest.mark.asyncio
    async def test_revoking_turns_off_every_radio_of_the_guild_in_every_bot(self, world):
        await with_premium(world)
        await with_premium(world, OTHER_GUILD)
        for bot in (1, 2, 3):
            await world.store.create(make_radio(bot))
            await world.store.set_enabled(radio_id(bot, GUILD), True)
        await world.store.create(make_radio(1, OTHER_GUILD))
        await world.store.set_enabled(radio_id(1, OTHER_GUILD), True)

        off = await world.store.revoke_premium(GUILD)

        assert len(off) == 3 and {r.enabled for r in off} == {False}
        assert await world.premium.fresh(GUILD) is None
        assert not any(r.enabled for r in await world.store.list_for_guild(GUILD))
        assert (await world.store.get(radio_id(1, OTHER_GUILD))).enabled and await active(world, OTHER_GUILD) == 1

    @pytest.mark.asyncio
    async def test_revoking_in_the_middle_of_an_enable_leaves_no_radio_on(self, world):
        await with_premium(world)
        await world.store.create(make_radio())

        results = await asyncio.gather(
            world.store.set_enabled(radio_id(1, GUILD), True), world.store.revoke_premium(GUILD), return_exceptions=True
        )

        assert await world.premium.fresh(GUILD) is None
        assert not (await world.store.get(radio_id(1, GUILD))).enabled, results

    @pytest.mark.asyncio
    async def test_a_revoked_guild_cannot_enable_again(self, world):
        await with_premium(world)
        await world.store.create(make_radio())
        await world.store.revoke_premium(GUILD)

        with pytest.raises(NoPremium):
            await world.store.set_enabled(radio_id(1, GUILD), True)


class TestChatRules:
    @pytest.mark.asyncio
    async def test_only_the_chat_changes_and_the_rev_moves(self, world):
        await world.store.create(make_radio())

        updated = await world.store.update_chat(
            radio_id(1, GUILD), {"voice_delete_after_s": 90}, expected_rev=0, updated_by="42"
        )

        assert updated.chat["voice_delete_after_s"] == 90 and updated.rev == 1
        assert (updated.channel_id, updated.source, updated.enabled) == (CHANNEL, {"playlist": "Rock"}, False)
        assert not hasattr(updated, "updated_by")
        assert world.db.tables["radios"][radio_id(1, GUILD)]["updated_by"] == "42"

    @pytest.mark.asyncio
    async def test_reconfiguring_keeps_the_chat_rules_the_panel_saved(self, world):  # CodeRabbit #180
        await world.store.create(make_radio())
        await world.store.update_chat(radio_id(1, GUILD), {"voice_delete_after_s": 90}, expected_rev=0)

        updated = await world.store.reconfigure(make_radio(channel_id="777", source={"playlist": "Jazz"}))

        assert updated.chat["voice_delete_after_s"] == 90  # as regras não voltam aos padrões
        assert (updated.channel_id, updated.source) == (777, {"playlist": "Jazz"})

    @pytest.mark.asyncio
    async def test_a_stale_rev_is_a_conflict(self, world):
        await world.store.create(make_radio())
        await world.store.update_chat(radio_id(1, GUILD), {"stage_max_messages": 5}, expected_rev=0)

        with pytest.raises(RadioConflict):
            await world.store.update_chat(radio_id(1, GUILD), {"stage_max_messages": 6}, expected_rev=0)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "chat",
        [{"voice_delete_after_s": 4}, {"stage_max_age_s": 9999}, {"enabled": True}, {"channel_id": "1"}, {"x": 1}],
    )
    async def test_out_of_range_or_foreign_fields_are_refused_and_nothing_is_written(self, world, chat):
        await world.store.create(make_radio())

        with pytest.raises(RadioInvalid) as refusal:
            await world.store.update_chat(radio_id(1, GUILD), chat, expected_rev=0)

        assert isinstance(refusal.value.error, RuleError)
        assert (await world.store.get(radio_id(1, GUILD))).rev == 0

    @pytest.mark.asyncio
    async def test_a_missing_radio_is_not_found(self, world):
        with pytest.raises(RadioNotFound):
            await world.store.update_chat(radio_id(7, GUILD), {}, expected_rev=0)


class TestCreateAndList:
    @pytest.mark.asyncio
    async def test_create_always_starts_disabled_at_rev_zero_and_never_twice(self, world):
        created = await world.store.create(make_radio(enabled=True, rev=9))

        assert created.enabled is False and created.rev == 0
        with pytest.raises(RadioConflict):
            await world.store.create(make_radio())

    @pytest.mark.asyncio
    async def test_the_list_is_per_guild_and_sorted(self, world):
        for bot, guild in ((2, GUILD), (1, GUILD), (1, OTHER_GUILD)):
            await world.store.create(make_radio(bot, guild))

        assert [r.id for r in await world.store.list_for_guild(GUILD)] == [radio_id(1, GUILD), radio_id(2, GUILD)]

    @pytest.mark.asyncio
    async def test_a_broken_document_is_skipped_not_fatal(self, world):
        await world.store.create(make_radio())
        world.db.tables["radios"]["9:500"] = {"_id": "9:500", "guild_id": "500", "kind": "nonsense"}

        assert [r.id for r in await world.store.list_for_guild(GUILD)] == [radio_id(1, GUILD)]

    @pytest.mark.asyncio
    async def test_database_trouble_is_radio_unavailable(self, world):
        world.db.fail = True

        with pytest.raises(RadioUnavailable):
            await world.store.list_for_guild(GUILD)


class TestBootReconcile:
    @pytest.mark.asyncio
    async def test_the_counter_is_recomputed_from_the_radios_that_are_really_on(self, world):
        await with_premium(world)
        for bot in (1, 2):
            await world.store.create(make_radio(bot))
            await world.store.set_enabled(radio_id(bot, GUILD), True)
        await world.premium.set_active(GUILD, 0)  # contador corrompido

        report = await world.store.reconcile()

        assert await active(world) == 2 and report.counters_fixed == 1

    @pytest.mark.asyncio
    async def test_a_stale_counter_without_any_radio_on_goes_back_to_zero(self, world):
        await with_premium(world)
        await world.premium.set_active(GUILD, 2)

        report = await world.store.reconcile()

        assert await active(world) == 0 and report.counters_fixed == 1

    @pytest.mark.asyncio
    async def test_radios_over_the_cap_or_without_premium_are_turned_off_on_boot(self, world):
        await with_premium(world)
        for bot in range(1, 6):
            await world.store.create(make_radio(bot))
            doc = world.db.tables["radios"][radio_id(bot, GUILD)]
            doc["enabled"] = True  # ligadas por fora do contador (banco restaurado, versão antiga...)
        await world.store.create(make_radio(1, OTHER_GUILD))
        world.db.tables["radios"][radio_id(1, OTHER_GUILD)]["enabled"] = True  # esse servidor não tem premium

        report = await world.store.reconcile()

        assert report.over_cap == 2 and report.without_premium == 1
        assert sum(r.enabled for r in await world.store.list_for_guild(GUILD)) == 3 and await active(world) == 3
        assert not (await world.store.get(radio_id(1, OTHER_GUILD))).enabled


class TestReconfigure:
    @pytest.mark.asyncio
    async def test_a_disabled_radio_takes_the_new_channel_and_source_and_loses_the_old_panel(self, world):
        await world.store.create(make_radio())
        await world.store.set_panel_message(radio_id(1, GUILD), 4242)

        updated = await world.store.reconfigure(
            make_radio(source={"playlist": "Jazz"}, channel_id="777"),
            updated_by="9",
        )

        assert (updated.channel_id, updated.source, updated.panel_message_id) == (777, {"playlist": "Jazz"}, None)

    @pytest.mark.asyncio
    async def test_an_enabled_radio_cannot_be_reconfigured(self, world):
        await with_premium(world)
        await world.store.create(make_radio())
        await world.store.set_enabled(radio_id(1, GUILD), True)

        with pytest.raises(RadioConflict):
            await world.store.reconfigure(make_radio(channel_id="777"))

    @pytest.mark.asyncio
    async def test_a_missing_radio_is_not_found(self, world):
        with pytest.raises(RadioNotFound):
            await world.store.reconfigure(make_radio(7))


class TestReserveFirst:
    """Revisão: reservar a vaga ANTES de gravar ``enabled`` mantém o contador igual ao número de rádios ligadas em qualquer ordem."""

    @pytest.mark.asyncio
    async def test_a_refused_enable_racing_a_disable_never_gives_back_a_slot_that_was_not_reserved(self, world):
        await with_premium(world)
        for bot in range(1, 5):
            await world.store.create(make_radio(bot))
        for bot in (1, 2, 3):
            await world.store.set_enabled(radio_id(bot, GUILD), True)

        await asyncio.gather(
            world.store.set_enabled(radio_id(4, GUILD), True),
            world.store.set_enabled(radio_id(4, GUILD), False),
            return_exceptions=True,
        )

        enabled = sum(r.enabled for r in await world.store.list_for_guild(GUILD))
        assert enabled == 3 and await active(world) == 3

    @pytest.mark.asyncio
    async def test_any_mix_of_enables_and_disables_ends_with_the_counter_equal_to_the_radios_on(self, world):
        await with_premium(world)
        for bot in range(1, 7):
            await world.store.create(make_radio(bot))
        jobs = []
        for round_ in range(4):
            for bot in range(1, 7):
                jobs.append(world.store.set_enabled(radio_id(bot, GUILD), round_ % 2 == 0))

        await asyncio.gather(*jobs, return_exceptions=True)

        enabled = sum(r.enabled for r in await world.store.list_for_guild(GUILD))
        assert await active(world) == enabled and enabled <= 3

    @pytest.mark.asyncio
    async def test_a_reservation_that_raises_leaves_the_radio_off_and_the_counter_untouched(
        self, world
    ):  # CodeRabbit #180
        await with_premium(world)
        await world.store.create(make_radio())

        async def broken(guild_id):
            raise ConnectionError("mongo fora")

        world.premium.reserve = broken

        with pytest.raises(ConnectionError):
            await world.store.set_enabled(radio_id(1, GUILD), True)

        assert not (await world.store.get(radio_id(1, GUILD))).enabled and await active(world) == 0

    @pytest.mark.asyncio
    async def test_a_failed_write_after_the_reservation_gives_the_slot_back(self, world):
        await with_premium(world)
        await world.store.create(make_radio())
        real = world.db.save_doc_cas

        async def flaky(id_, doc, **kwargs):
            if kwargs["db_name"] == "radios" and doc.get("enabled") is True:
                raise ConnectionError("mongo fora")
            return await real(id_, doc, **kwargs)

        world.db.save_doc_cas = flaky

        with pytest.raises(RadioUnavailable):
            await world.store.set_enabled(radio_id(1, GUILD), True)
        world.db.save_doc_cas = real

        assert await active(world) == 0 and not (await world.store.get(radio_id(1, GUILD))).enabled

    @pytest.mark.asyncio
    async def test_a_premium_revoked_between_the_reservation_and_the_write_leaves_the_radio_off(self, world):
        await with_premium(world)
        await world.store.create(make_radio())
        real = world.db.save_doc_cas

        async def revoke_then_save(id_, doc, **kwargs):
            if kwargs["db_name"] == "radios" and doc.get("enabled") is True:
                await world.premium.revoke(GUILD)  # a revogação passa exatamente aqui
            return await real(id_, doc, **kwargs)

        world.db.save_doc_cas = revoke_then_save

        with pytest.raises(NoPremium):
            await world.store.set_enabled(radio_id(1, GUILD), True)
        world.db.save_doc_cas = real

        assert not (await world.store.get(radio_id(1, GUILD))).enabled


class TestPremiumCacheAgainstRevoke:
    @pytest.mark.asyncio
    async def test_a_read_that_started_before_a_revoke_does_not_repopulate_the_cache(self, world):
        await with_premium(world)
        real = world.db.find_doc
        calls = {"n": 0}

        async def slow_first(id_, **kwargs):
            calls["n"] += 1
            doc = await real(id_, **kwargs)  # lê o documento ANTES da revogação...
            if calls["n"] == 1:
                for _ in range(8):
                    await asyncio.sleep(0)  # ...e demora a devolver, enquanto a revogação termina
            return doc

        world.db.find_doc = slow_first
        await asyncio.gather(world.premium.get(GUILD), world.premium.revoke(GUILD))
        world.db.find_doc = real

        assert await world.premium.get(GUILD) is None  # sem o cache velho de 30 s


class TestClockPersistence:
    @pytest.mark.asyncio
    async def test_enabling_stamps_the_virtual_clock_and_disabling_then_enabling_restarts_it(self, world, monkeypatch):
        from empiremuse.utils.radio import store as store_module

        await with_premium(world)
        await world.store.create(make_radio())
        times = iter([1000.0, 5000.0])
        monkeypatch.setattr(store_module.time, "time", lambda: next(times))

        on = await world.store.set_enabled(radio_id(1, GUILD), True)
        await world.store.set_enabled(radio_id(1, GUILD), False)
        again = await world.store.set_enabled(radio_id(1, GUILD), True)

        assert on.clock_started_at == 1000.0 and again.clock_started_at == 5000.0
        assert (await world.store.get(radio_id(1, GUILD))).clock_started_at == 5000.0

    @pytest.mark.asyncio
    async def test_a_restart_reads_the_same_origin_back(self, world):
        await with_premium(world)
        await world.store.create(make_radio())
        on = await world.store.set_enabled(radio_id(1, GUILD), True)

        from empiremuse.utils.radio import RadioHub

        reborn = RadioHub(world.pool)

        assert (await reborn.store.get(radio_id(1, GUILD))).clock_started_at == on.clock_started_at

    @pytest.mark.asyncio
    async def test_an_old_document_without_the_field_is_stamped_once(self, world):
        await with_premium(world)
        await world.store.create(make_radio())
        await world.store.set_enabled(radio_id(1, GUILD), True)
        del world.db.tables["radios"][radio_id(1, GUILD)]["clock_started_at"]
        legacy = await world.store.get(radio_id(1, GUILD))
        assert legacy.clock_started_at is None

        stamped = await world.store.ensure_clock(legacy)
        again = await world.store.ensure_clock(stamped)

        assert stamped.clock_started_at is not None and again.clock_started_at == stamped.clock_started_at

    @pytest.mark.asyncio
    async def test_reconfiguring_clears_the_clock(self, world):
        await world.store.create(make_radio())
        world.db.tables["radios"][radio_id(1, GUILD)]["clock_started_at"] = 123.0

        updated = await world.store.reconfigure(make_radio(source={"playlist": "Jazz"}))

        assert updated.clock_started_at is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [0, -5, True, "x", float("nan"), 5e12])
    async def test_a_garbage_clock_makes_the_document_invalid(self, world, bad):
        from empiremuse.rules.radio import check_radio as check

        doc = {
            "_id": radio_id(1, GUILD),
            "channel_id": "9",
            "kind": "custom",
            "source": {"playlist": "p"},
            "clock_started_at": bad,
        }

        assert isinstance(check(doc), RuleError)
