"""As estações do servidor (item 8, R5): criar, editar e apagar com compare-and-set, nome único, teto de 10, estação em uso e isolamento por servidor."""

import asyncio
from types import SimpleNamespace

import pytest

from empiremuse.utils.radio import RadioHub
from empiremuse.utils.radio.stations import StationConflict, StationInUse, StationInvalid, StationNotFound
from empiremuse.utils.radio.store import RadioInvalid
from tests.persistence.test_radio_store import GUILD, FakeDb, make_radio, with_premium

pytestmark = pytest.mark.critical

OTHER_GUILD = GUILD + 1
ROCK = {"name": "Rock", "kind": "artists", "source": {"artists": ["Queen"]}}


@pytest.fixture
def world():
    db = FakeDb()
    hub = RadioHub(SimpleNamespace(database=db))
    return SimpleNamespace(db=db, hub=hub, stations=hub.stations, store=hub.store, premium=hub.premium)


class TestCreateAndList:
    @pytest.mark.asyncio
    async def test_a_created_station_is_listed_by_name_and_only_in_its_server(self, world):
        await world.stations.create(GUILD, {**ROCK, "name": "Zeta"}, updated_by="7")
        await world.stations.create(GUILD, {**ROCK, "name": "alfa"})

        assert [s.name for s in await world.stations.list_for_guild(GUILD)] == ["alfa", "Zeta"]
        assert await world.stations.list_for_guild(OTHER_GUILD) == []

    @pytest.mark.asyncio
    async def test_the_same_name_in_another_case_is_refused(self, world):
        await world.stations.create(GUILD, ROCK)

        with pytest.raises(StationInvalid) as refused:
            await world.stations.create(GUILD, {**ROCK, "name": "ROCK"})

        assert refused.value.error.key == "name"

    @pytest.mark.asyncio
    async def test_the_same_name_is_fine_in_another_server(self, world):
        await world.stations.create(GUILD, ROCK)

        assert (await world.stations.create(OTHER_GUILD, ROCK)).guild_id == OTHER_GUILD

    @pytest.mark.asyncio
    async def test_the_eleventh_station_is_refused(self, world):
        for number in range(10):
            await world.stations.create(GUILD, {**ROCK, "name": f"E{number}"})

        with pytest.raises(StationInvalid) as refused:
            await world.stations.create(GUILD, {**ROCK, "name": "E10"})

        assert refused.value.error.key == "limit"

    @pytest.mark.asyncio
    async def test_two_concurrent_creates_of_the_same_name_make_one_station(self, world):
        results = await asyncio.gather(
            world.stations.create(GUILD, ROCK), world.stations.create(GUILD, ROCK), return_exceptions=True
        )

        assert sum(isinstance(r, StationInvalid) for r in results) == 1
        assert len(await world.stations.list_for_guild(GUILD)) == 1

    @pytest.mark.asyncio
    async def test_a_playlist_station_needs_the_playlist_to_exist(self, world):
        raw = {"name": "Minhas", "kind": "playlists", "source": {"playlists": ["Pop"]}}

        with pytest.raises(StationInvalid):
            await world.stations.create(GUILD, raw, playlists={"Rock"})

        assert (await world.stations.create(GUILD, raw, playlists={"Pop"})).source == {"playlists": ["Pop"]}


class TestUpdateAndGet:
    @pytest.mark.asyncio
    async def test_an_update_bumps_the_rev_and_keeps_the_id(self, world):
        created = await world.stations.create(GUILD, ROCK)

        updated = await world.stations.update(
            GUILD, created.id, {**ROCK, "name": "Rock 2"}, expected_rev=created.rev, updated_by="9"
        )

        assert (updated.id, updated.rev, updated.name, updated.updated_by) == (created.id, 1, "Rock 2", "9")

    @pytest.mark.asyncio
    async def test_a_stale_rev_is_a_conflict_and_changes_nothing(self, world):
        created = await world.stations.create(GUILD, ROCK)
        await world.stations.update(GUILD, created.id, {**ROCK, "name": "A"}, expected_rev=0)

        with pytest.raises(StationConflict):
            await world.stations.update(GUILD, created.id, {**ROCK, "name": "B"}, expected_rev=0)

        assert (await world.stations.get(GUILD, created.id)).name == "A"

    @pytest.mark.asyncio
    async def test_editing_the_tenth_station_is_not_blocked_by_the_cap(self, world):
        for number in range(10):
            last = await world.stations.create(GUILD, {**ROCK, "name": f"E{number}"})

        updated = await world.stations.update(GUILD, last.id, {**ROCK, "name": "Renomeada"}, expected_rev=0)

        assert updated.name == "Renomeada"

    @pytest.mark.asyncio
    async def test_a_station_of_another_server_is_not_found_by_id(self, world):
        created = await world.stations.create(OTHER_GUILD, ROCK)

        assert await world.stations.get(GUILD, created.id) is None
        with pytest.raises(StationNotFound):
            await world.stations.update(GUILD, created.id, ROCK, expected_rev=0)
        with pytest.raises(StationNotFound):
            await world.stations.delete(GUILD, created.id, expected_rev=0)
        assert (await world.stations.get(OTHER_GUILD, created.id)).name == "Rock"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("station_id", ["", "x", "../a", 5, None, "G" * 24])
    async def test_a_malformed_id_is_just_not_found(self, world, station_id):
        assert await world.stations.get(GUILD, station_id) is None

    @pytest.mark.asyncio
    async def test_changing_the_artists_clears_the_unknown_marks(self, world):
        raw = {"name": "Meus", "kind": "artists", "source": {"artists": ["Xyzzy"]}}
        created = await world.stations.create(GUILD, raw)
        await world.stations.set_unknown(GUILD, created.id, ["Xyzzy"])
        marked = await world.stations.get(GUILD, created.id)

        same = await world.stations.update(GUILD, created.id, raw, expected_rev=marked.rev)
        changed = await world.stations.update(
            GUILD, created.id, {**raw, "source": {"artists": ["Djavan"]}}, expected_rev=same.rev
        )

        assert marked.unknown == ("Xyzzy",) and same.unknown == ("Xyzzy",) and changed.unknown == ()


class TestDelete:
    @pytest.mark.asyncio
    async def test_an_unused_station_is_deleted(self, world):
        created = await world.stations.create(GUILD, ROCK)

        await world.stations.delete(GUILD, created.id, expected_rev=created.rev)

        assert await world.stations.get(GUILD, created.id) is None

    @pytest.mark.asyncio
    async def test_a_station_in_use_is_refused_and_names_the_radio(self, world):
        created = await world.stations.create(GUILD, ROCK)
        await with_premium(world)
        radio = await world.store.create(make_radio(source={"station": created.id}))

        with pytest.raises(StationInUse) as refused:
            await world.stations.delete(GUILD, created.id, expected_rev=created.rev)

        assert refused.value.radios == [radio.id]
        assert await world.stations.get(GUILD, created.id) is not None

    @pytest.mark.asyncio
    async def test_a_stale_rev_does_not_delete(self, world):
        created = await world.stations.create(GUILD, ROCK)

        with pytest.raises(StationConflict):
            await world.stations.delete(GUILD, created.id, expected_rev=5)


class TestAssignToARadio:
    @pytest.mark.asyncio
    async def test_a_custom_radio_takes_a_station_of_its_own_server(self, world):
        await with_premium(world)
        radio = await world.store.create(make_radio())
        station = await world.stations.create(GUILD, ROCK)

        updated = await world.store.set_custom_station(radio.id, station.id, expected_rev=radio.rev, updated_by="9")

        assert updated.source == {"station": station.id} and updated.rev == radio.rev + 1

    @pytest.mark.asyncio
    async def test_a_station_of_another_server_is_refused(self, world):
        await with_premium(world)
        radio = await world.store.create(make_radio())
        foreign = await world.stations.create(OTHER_GUILD, ROCK)

        with pytest.raises(RadioInvalid):
            await world.store.set_custom_station(radio.id, foreign.id, expected_rev=radio.rev)

        assert (await world.store.get(radio.id)).source == {"playlist": "Rock"}

    @pytest.mark.asyncio
    async def test_a_live_radio_never_takes_a_server_station(self, world):
        await with_premium(world)
        main = "11111111-1111-1111-1111-111111111111"
        radio = await world.store.create(make_radio(kind="live", source={"station": main, "reserve": None}))
        station = await world.stations.create(GUILD, ROCK)

        with pytest.raises(RadioInvalid):
            await world.store.set_custom_station(radio.id, station.id, expected_rev=radio.rev)


class TestDeleteRacesAnAssignment:
    @pytest.mark.regression
    @pytest.mark.asyncio
    async def test_a_station_deleted_while_a_radio_takes_it_never_leaves_the_radio_pointing_at_nothing(self, world):
        """PI 72: o apagar confere "nenhuma rádio usa" e, antes de apagar, ``set_custom_station`` grava a rádio com a estação."""
        await with_premium(world)
        radio = await world.store.create(make_radio())
        station = await world.stations.create(GUILD, ROCK)
        checked, proceed, station_read = asyncio.Event(), asyncio.Event(), asyncio.Event()
        real_using, real_find = world.stations.radios_using, world.db.find_doc

        async def find_and_tell(id_, *, db_name, collection):
            if checked.is_set() and id_ == station.id:
                station_read.set()  # a atribuição leu a estação depois da conferência do apagar
            return await real_find(id_, db_name=db_name, collection=collection)

        world.db.find_doc = find_and_tell

        async def using_then_wait(guild_id, station_id):
            found = await real_using(guild_id, station_id)
            checked.set()
            await proceed.wait()  # o apagar para entre a conferência e a remoção
            return found

        world.stations.radios_using = using_then_wait
        deleting = asyncio.create_task(world.stations.delete(GUILD, station.id, expected_rev=station.rev))
        await checked.wait()
        assigning = asyncio.create_task(world.store.set_custom_station(radio.id, station.id, expected_rev=radio.rev))
        for _ in range(50):  # só cede a vez, sem relógio: até a atribuição ler a estação ou ficar presa na trava
            if station_read.is_set() or assigning.done():
                break
            await asyncio.sleep(0)
        proceed.set()
        results = await asyncio.gather(deleting, assigning, return_exceptions=True)

        gone = await world.stations.get(GUILD, station.id) is None
        points = (await world.store.get(radio.id)).source.get("station") == station.id
        assert not (gone and points), results


class TestEditingReloadsTheRadiosThatPlayTheStation:
    """Editar pelo chat e pelo painel web chama o mesmo ``StationStore.update``: o recarregamento mora no backend, não no cog (gate do #196, item 5)."""

    @pytest.mark.asyncio
    async def test_an_update_reloads_the_radios_that_use_the_station(self, world):
        station = await world.stations.create(GUILD, ROCK)
        radio = make_radio(source={"station": station.id})
        await world.store.create(radio)
        reloaded = []

        async def change_station(radio_):
            reloaded.append(radio_.id)
            return True

        world.hub.manager.change_station = change_station

        await world.stations.update(
            GUILD, station.id, {**ROCK, "name": "Rock 2"}, expected_rev=station.rev, updated_by="7"
        )

        assert reloaded == [radio.id]

    @pytest.mark.asyncio
    async def test_a_radio_on_another_station_is_left_alone(self, world):
        station = await world.stations.create(GUILD, ROCK)
        other = await world.stations.create(GUILD, {**ROCK, "name": "Outra"})
        await world.store.create(make_radio(source={"station": other.id}))
        reloaded = []

        async def change_station(radio_):
            reloaded.append(radio_.id)

        world.hub.manager.change_station = change_station

        await world.stations.update(GUILD, station.id, {**ROCK, "name": "Rock 2"}, expected_rev=station.rev)

        assert reloaded == []

    @pytest.mark.asyncio
    async def test_a_failing_reload_never_undoes_the_saved_edit(self, world):
        station = await world.stations.create(GUILD, ROCK)
        await world.store.create(make_radio(source={"station": station.id}))

        async def change_station(radio_):
            raise RuntimeError("boom")

        world.hub.manager.change_station = change_station

        saved = await world.stations.update(GUILD, station.id, {**ROCK, "name": "Rock 2"}, expected_rev=station.rev)

        assert saved.name == "Rock 2" and (await world.stations.get(GUILD, station.id)).name == "Rock 2"
