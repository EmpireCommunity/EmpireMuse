"""A estação reserva de uma rádio ao vivo (item 8, R3): compare-and-set por ``rev``, só em rádio ao vivo, nunca a própria principal."""

from types import SimpleNamespace

import pytest

from empiremuse.rules.radio import radio_id
from empiremuse.utils.radio import RadioHub
from empiremuse.utils.radio.store import RadioConflict, RadioInvalid, RadioNotFound
from tests.persistence.test_radio_store import GUILD, FakeDb, make_radio, with_premium

pytestmark = pytest.mark.critical

MAIN, RESERVE = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def world():
    db = FakeDb()
    hub = RadioHub(SimpleNamespace(database=db))
    return SimpleNamespace(db=db, hub=hub, store=hub.store, premium=hub.premium)


async def live(world, **over):
    await with_premium(world)
    radio = make_radio(kind="live", source={"station": MAIN, "reserve": None}, **over)
    return await world.store.create(radio)


class TestSetReserve:
    @pytest.mark.asyncio
    async def test_it_sets_and_clears_the_reserve_bumping_the_rev(self, world):
        created = await live(world)

        set_ = await world.store.set_reserve(created.id, RESERVE, expected_rev=created.rev, updated_by="9")
        cleared = await world.store.set_reserve(created.id, None, expected_rev=set_.rev)

        assert set_.source == {"station": MAIN, "reserve": RESERVE} and set_.rev == created.rev + 1
        assert cleared.source == {"station": MAIN, "reserve": None}

    @pytest.mark.asyncio
    async def test_a_stale_rev_is_a_conflict_and_changes_nothing(self, world):
        created = await live(world)
        await world.store.set_reserve(created.id, RESERVE, expected_rev=created.rev)

        with pytest.raises(RadioConflict):
            await world.store.set_reserve(created.id, None, expected_rev=created.rev)

        assert (await world.store.get(created.id)).source["reserve"] == RESERVE

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reserve", [MAIN, "nao-e-uuid", "", 5, "../x"])
    async def test_the_main_station_or_a_bad_uuid_is_invalid(self, world, reserve):
        created = await live(world)

        with pytest.raises(RadioInvalid):
            await world.store.set_reserve(created.id, reserve, expected_rev=created.rev)

        assert (await world.store.get(created.id)).rev == created.rev

    @pytest.mark.asyncio
    async def test_a_custom_radio_has_no_reserve(self, world):
        await with_premium(world)
        created = await world.store.create(make_radio())

        with pytest.raises(RadioInvalid):
            await world.store.set_reserve(created.id, RESERVE, expected_rev=created.rev)

    @pytest.mark.asyncio
    async def test_a_missing_radio_is_not_found(self, world):
        with pytest.raises(RadioNotFound):
            await world.store.set_reserve(radio_id(9, GUILD), RESERVE, expected_rev=0)


THIRD = "33333333-3333-3333-3333-333333333333"


class TestSetStation:
    """A estação PRINCIPAL muda pelo seletor de local (painel da call e página Rádios): mesmas regras da reserva, com compare-and-set em ``rev``."""

    @pytest.mark.asyncio
    async def test_it_sets_the_main_station_bumping_the_rev_and_keeping_the_reserve(self, world):
        created = await live(world)
        with_reserve = await world.store.set_reserve(created.id, RESERVE, expected_rev=created.rev)

        changed = await world.store.set_station(created.id, THIRD, expected_rev=with_reserve.rev, updated_by="9")

        assert changed.source == {"station": THIRD, "reserve": RESERVE} and changed.rev == with_reserve.rev + 1
        assert (await world.store.get(created.id)).source["station"] == THIRD

    @pytest.mark.asyncio
    async def test_a_reserve_equal_to_the_new_main_station_is_dropped(self, world):
        created = await live(world)
        with_reserve = await world.store.set_reserve(created.id, RESERVE, expected_rev=created.rev)

        changed = await world.store.set_station(created.id, RESERVE, expected_rev=with_reserve.rev)

        assert changed.source == {"station": RESERVE, "reserve": None}

    @pytest.mark.asyncio
    async def test_a_stale_rev_is_a_conflict_and_changes_nothing(self, world):
        created = await live(world)
        await world.store.set_reserve(created.id, RESERVE, expected_rev=created.rev)

        with pytest.raises(RadioConflict):
            await world.store.set_station(created.id, THIRD, expected_rev=created.rev)

        assert (await world.store.get(created.id)).source["station"] == MAIN

    @pytest.mark.asyncio
    @pytest.mark.parametrize("uuid", ["nao-e-uuid", "", 5, None, "../x", MAIN + "x"])
    async def test_a_bad_uuid_is_invalid(self, world, uuid):
        created = await live(world)

        with pytest.raises(RadioInvalid):
            await world.store.set_station(created.id, uuid, expected_rev=created.rev)

        assert (await world.store.get(created.id)).rev == created.rev

    @pytest.mark.asyncio
    async def test_a_custom_radio_has_no_station(self, world):
        await with_premium(world)
        created = await world.store.create(make_radio())

        with pytest.raises(RadioInvalid):
            await world.store.set_station(created.id, THIRD, expected_rev=created.rev)

    @pytest.mark.asyncio
    async def test_a_missing_radio_is_not_found(self, world):
        with pytest.raises(RadioNotFound):
            await world.store.set_station(radio_id(9, GUILD), THIRD, expected_rev=0)


FOURTH = "44444444-4444-4444-4444-444444444444"


class TestListAndVolumeInTheSource:
    """A lista de onde a estação foi escolhida (anterior e próxima) e o volume ficam no documento da rádio ao vivo e sobrevivem às outras edições."""

    @pytest.mark.asyncio
    async def test_set_station_with_a_list_stores_it_and_without_one_keeps_it(self, world):
        created = await live(world)
        listed = await world.store.set_station(
            created.id, RESERVE, expected_rev=created.rev, station_list=[MAIN, RESERVE, THIRD]
        )

        moved = await world.store.set_station(
            created.id, THIRD, expected_rev=listed.rev
        )  # anterior/próxima: a lista fica

        assert listed.source["list"] == [MAIN, RESERVE, THIRD] and moved.source["list"] == [MAIN, RESERVE, THIRD]
        assert moved.source["station"] == THIRD

    @pytest.mark.asyncio
    async def test_set_station_with_none_or_a_one_station_list_drops_it(self, world):
        created = await live(world)
        listed = await world.store.set_station(created.id, MAIN, expected_rev=created.rev, station_list=[MAIN, RESERVE])

        cleared = await world.store.set_station(created.id, RESERVE, expected_rev=listed.rev, station_list=None)
        short = await world.store.set_station(created.id, MAIN, expected_rev=cleared.rev, station_list=[MAIN])

        assert "list" not in cleared.source and "list" not in short.source

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad", [[MAIN, MAIN], ["nao-e-uuid", MAIN], [MAIN] * 2 + [RESERVE], [str(n) for n in range(151)]]
    )
    async def test_a_bad_list_is_invalid(self, world, bad):
        created = await live(world)

        with pytest.raises(RadioInvalid):
            await world.store.set_station(created.id, MAIN, expected_rev=created.rev, station_list=bad)

    @pytest.mark.asyncio
    async def test_the_reserve_edit_from_the_web_page_keeps_the_list_and_the_volume(self, world):
        created = await live(world)
        listed = await world.store.set_station(created.id, MAIN, expected_rev=created.rev, station_list=[MAIN, RESERVE])
        loud = await world.store.set_volume(created.id, 60, expected_rev=listed.rev)

        changed = await world.store.set_reserve(created.id, THIRD, expected_rev=loud.rev)

        assert changed.source == {"station": MAIN, "reserve": THIRD, "list": [MAIN, RESERVE], "volume": 60}

    @pytest.mark.asyncio
    async def test_set_volume_stores_clamped_values_and_the_default_is_not_stored(self, world):
        created = await live(world)

        quiet = await world.store.set_volume(created.id, 40, expected_rev=created.rev, updated_by="9")
        default = await world.store.set_volume(created.id, 100, expected_rev=quiet.rev)

        assert quiet.source["volume"] == 40 and "volume" not in default.source

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [9, 151, -5, 0, True, "50", None, 50.5])
    async def test_a_volume_out_of_range_or_not_an_int_is_invalid(self, world, bad):
        created = await live(world)

        with pytest.raises(RadioInvalid):
            await world.store.set_volume(created.id, bad, expected_rev=created.rev)

    @pytest.mark.asyncio
    async def test_set_volume_conflicts_and_refuses_a_custom_radio_and_a_missing_one(self, world):
        created = await live(world)
        await world.store.set_volume(created.id, 40, expected_rev=created.rev)
        with pytest.raises(RadioConflict):
            await world.store.set_volume(created.id, 50, expected_rev=created.rev)
        with pytest.raises(RadioNotFound):
            await world.store.set_volume(radio_id(9, GUILD), 50, expected_rev=0)
        await with_premium(world)
        custom = await world.store.create(make_radio(bot_id=2))
        with pytest.raises(RadioInvalid):
            await world.store.set_volume(custom.id, 50, expected_rev=custom.rev)
