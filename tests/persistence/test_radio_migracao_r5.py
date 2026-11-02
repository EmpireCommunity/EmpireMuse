"""A migração da R5: a rádio personalizada de playlist vira rádio de estação do servidor, sem perder nada e sem repetir."""

from types import SimpleNamespace

import pytest

from empiremuse.rules.radio_station import MAX_STATIONS_PER_GUILD
from empiremuse.utils.radio import RadioHub, migrate_r5
from tests.persistence.test_radio_store import GUILD, FakeDb, make_radio, with_premium

pytestmark = pytest.mark.critical


@pytest.fixture
def world():
    db = FakeDb()
    hub = RadioHub(SimpleNamespace(database=db))
    return SimpleNamespace(db=db, hub=hub, store=hub.store, stations=hub.stations, premium=hub.premium)


async def with_radio(w, playlist="Rock", **over):
    await with_premium(w)
    return await w.store.create(make_radio(source={"playlist": playlist}, **over))


class TestRun:
    @pytest.mark.asyncio
    async def test_a_playlist_radio_gets_a_playlist_station_and_keeps_everything_else(self, world):
        radio = await with_radio(world)
        enabled = await world.store.set_enabled(radio.id, True)

        report = await migrate_r5.run(world.hub)

        migrated = await world.store.get(radio.id)
        station = (await world.stations.list_for_guild(GUILD))[0]
        assert report.migrated == 1 and migrated.source == {"station": station.id}
        assert (station.kind, station.source, station.name) == ("playlists", {"playlists": ["Rock"]}, "Rock")
        assert migrated.enabled and migrated.channel_id == enabled.channel_id and migrated.rev == enabled.rev + 1

    @pytest.mark.asyncio
    async def test_two_runs_make_one_station_and_the_second_does_nothing(self, world):
        await with_radio(world)
        await migrate_r5.run(world.hub)

        second = await migrate_r5.run(world.hub)

        assert second.skipped_marker is True and len(await world.stations.list_for_guild(GUILD)) == 1

    @pytest.mark.asyncio
    async def test_without_the_marker_a_rerun_reuses_the_station_and_leaves_a_station_radio_alone(self, world):
        await with_radio(world)
        await migrate_r5.run(world.hub)
        await world.db.delete_data(migrate_r5.MARKER_ID, migrate_r5.MIGRATIONS_DB, "global")

        report = await migrate_r5.run(world.hub)

        assert report.already == 1 and report.migrated == 0 and len(await world.stations.list_for_guild(GUILD)) == 1

    @pytest.mark.asyncio
    async def test_a_playlist_that_no_longer_exists_still_gets_its_station(self, world):
        await with_radio(world, playlist="Sumiu")

        await migrate_r5.run(world.hub)

        assert (await world.stations.list_for_guild(GUILD))[0].source == {"playlists": ["Sumiu"]}

    @pytest.mark.asyncio
    async def test_a_long_playlist_name_is_cut_to_the_station_limit(self, world):
        name = "P" * 90
        await with_radio(world, playlist=name)

        await migrate_r5.run(world.hub)

        station = (await world.stations.list_for_guild(GUILD))[0]
        assert len(station.name) == 32 and station.source == {"playlists": [name]}

    @pytest.mark.asyncio
    async def test_a_server_with_ten_stations_keeps_the_old_radio_and_no_marker_is_written(self, world):
        await with_radio(world)
        for number in range(MAX_STATIONS_PER_GUILD):
            await world.stations.create(GUILD, {"name": f"E{number}", "kind": "genre", "source": {"genre": "rock"}})

        report = await migrate_r5.run(world.hub)

        assert report.left_behind == 1 and (await world.store.get("1:" + str(GUILD))).source == {"playlist": "Rock"}
        assert (
            await world.db.find_doc(migrate_r5.MARKER_ID, db_name=migrate_r5.MIGRATIONS_DB, collection="global") is None
        )

    @pytest.mark.asyncio
    async def test_a_live_radio_is_untouched(self, world):
        await with_premium(world)
        main = "11111111-1111-1111-1111-111111111111"
        live = await world.store.create(make_radio(kind="live", source={"station": main, "reserve": None}))

        report = await migrate_r5.run(world.hub)

        assert report.migrated == 0 and (await world.store.get(live.id)).rev == live.rev
