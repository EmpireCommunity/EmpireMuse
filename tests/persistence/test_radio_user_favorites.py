"""J3: isolamento pessoal, CAS determinístico, limites e entrada não confiável."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.utils.radio import RadioHub
from empiremuse.utils.radio.custom_favorites import Toggled
from empiremuse.utils.radio.directory import DirectoryError
from empiremuse.utils.radio.favorites import FavoriteForbidden, FavoriteNotFound
from empiremuse.utils.radio.perms import can_manage_guild
from empiremuse.utils.radio.premium import RadioUnavailable
from empiremuse.utils.radio.user_favorites import USER_FAVORITES_DB
from tests.persistence.test_radio_favoritas import station, uuid
from tests.persistence.test_radio_store import FakeDb

pytestmark = [pytest.mark.critical, pytest.mark.asyncio]


class MemoryDb(FakeDb):
    async def _tick(self):
        if self.fail:
            raise ConnectionError("indisponível")


@pytest.fixture
def world():
    db = MemoryDb()
    hub = RadioHub(SimpleNamespace(database=db))
    hub.directory = SimpleNamespace(
        by_uuid=AsyncMock(side_effect=lambda value: station(int(value[-12:]), "@everyone https://evil.example Rádio"))
    )
    hub.locations = SimpleNamespace(node=lambda value: object() if value.startswith("node:") else None)
    return SimpleNamespace(db=db, hub=hub, store=hub.user_favorites)


async def test_personal_round_trip_isolation_and_erasure(world):
    store, db = world.store, world.db
    assert await store.toggle_station(7, uuid(1)) == Toggled.ADDED
    assert await store.toggle_filter(7, "node:city") == Toggled.ADDED
    doc = db.tables[USER_FAVORITES_DB]["7"]
    assert set(doc) == {"_id", "rev", "stations", "filters"}
    assert set(doc["stations"][0]) == {"uuid", "name"}
    assert "https://" not in str(doc) and "@everyone" not in str(doc)
    assert doc["filters"] == [{"node": "node:city"}]
    assert await store.stations(17) == [] and await store.filters(17) == []
    world.hub.directory.by_uuid.reset_mock()
    assert await store.toggle_station(7, uuid(1)) == Toggled.REMOVED
    world.hub.directory.by_uuid.assert_not_awaited()
    assert await store.toggle_filter(7, "node:city") == Toggled.REMOVED
    await store.toggle_station(17, uuid(2))
    await db.delete_data("7", db_name=USER_FAVORITES_DB, collection="global")
    assert await store.stations(7) == [] and len(await store.stations(17)) == 1


@pytest.mark.parametrize("operation,item,limit", [("station", uuid(99), 24), ("filter", "node:99", 10)])
async def test_capacity_full_before_directory_and_idempotent_removal(world, operation, item, limit):
    store = world.store
    toggle = getattr(store, "toggle_" + operation)
    for n in range(limit):
        assert await toggle(7, uuid(n) if operation == "station" else f"node:{n}") == Toggled.ADDED
    world.hub.directory.by_uuid.reset_mock()
    assert await toggle(7, item) == Toggled.FULL
    world.hub.directory.by_uuid.assert_not_awaited()
    remove = getattr(store, "remove_" + operation)
    assert not await remove(7, "invalid")
    assert await remove(7, uuid(0) if operation == "station" else "node:0")
    clear = getattr(store, "clear_" + ("stations" if operation == "station" else "filters"))
    assert await clear(7) and not await clear(7)
    assert not await clear(99)


@pytest.mark.parametrize(
    "operation,value", [("station", "bad"), ("station", uuid(4)), ("filter", "bad!"), ("filter", "missing")]
)
async def test_invalid_or_missing_never_writes(world, operation, value):
    world.hub.directory.by_uuid = AsyncMock(return_value=None)
    world.db.save_doc_cas = AsyncMock()
    with pytest.raises(FavoriteNotFound):
        await getattr(world.store, "toggle_" + operation)(7, value)
    world.db.save_doc_cas.assert_not_awaited()


async def test_hidden_and_obsolete_filters_remain_removable(world):
    await world.store.toggle_filter(7, "node:1")
    world.hub.locations = None
    assert await world.store.filters(7) == []
    # Alterar estações com a árvore desligada não apaga os filtros guardados.
    await world.store.toggle_station(7, uuid(1))
    assert world.db.tables[USER_FAVORITES_DB]["7"]["filters"] == [{"node": "node:1"}]
    with pytest.raises(FavoriteNotFound):
        await world.store.toggle_filter(7, "node:2")
    assert await world.store.toggle_filter(7, "node:1") == Toggled.REMOVED
    world.hub.locations = SimpleNamespace(node=lambda value: None)
    world.db.tables[USER_FAVORITES_DB]["7"]["filters"] = [{"node": "node:old"}, None, {"node": "bad!"}]
    assert await world.store.filters(7) == []
    assert await world.store.remove_filter(7, "node:old")


@pytest.mark.parametrize("failure", ["read", "write", "cas", "directory"])
async def test_failures_are_unavailable_without_personal_logs(world, failure, caplog):
    if failure == "read":
        world.db.fail = True
    elif failure == "write":
        world.db.save_doc_cas = AsyncMock(side_effect=ConnectionError("private"))
    elif failure == "cas":
        world.db.save_doc_cas = AsyncMock(return_value=False)
    else:
        world.hub.directory.by_uuid = AsyncMock(side_effect=DirectoryError("unavailable"))
    with pytest.raises(RadioUnavailable):
        await world.store.toggle_station(7, uuid(1))
    assert uuid(1) not in caplog.text and "private" not in caplog.text


async def test_two_additions_for_last_slot_and_cross_field_updates(world):
    for n in range(23):
        await world.store.toggle_station(7, uuid(n))
    find = world.db.find_doc
    both_read = asyncio.Event()
    reads = 0

    async def overlap(*args, **kwargs):
        nonlocal reads
        doc = await find(*args, **kwargs)
        reads += 1
        if reads == 2:
            both_read.set()
        if reads <= 2:
            await both_read.wait()
        return doc

    world.db.find_doc = overlap
    results = await asyncio.gather(world.store.toggle_station(7, uuid(90)), world.store.toggle_station(7, uuid(91)))
    assert sorted(results) == sorted([Toggled.ADDED, Toggled.FULL])
    assert len(await world.store.stations(7)) == 24
    # As duas colunas do documento compartilham o mesmo CAS.
    await asyncio.gather(world.store.clear_stations(7), world.store.toggle_filter(7, "node:1"))
    assert await world.store.stations(7) == [] and await world.store.filters(7) == ["node:1"]


async def test_malformed_documents_and_identity_type(world):
    world.db.tables[USER_FAVORITES_DB] = {"7": {"rev": 0, "stations": [None, {"uuid": "bad"}], "filters": [None]}}
    assert await world.store.stations(7) == [] and await world.store.filters(7) == []
    for value in ("7", True, None):
        with pytest.raises(TypeError):
            await world.store.toggle_station(value, uuid(1))


@pytest.mark.parametrize(
    "permission", ["manage_guild", "administrator", "owner", "member", "absent", "fetch_error", "fetch"]
)
async def test_server_gate_cache_fetch_and_fail_closed(permission):
    member = SimpleNamespace(
        guild_permissions=SimpleNamespace(
            manage_guild=permission in {"manage_guild", "fetch"}, administrator=permission == "administrator"
        )
    )
    guild = SimpleNamespace(
        owner_id=7 if permission == "owner" else 8,
        get_member=lambda uid: member if permission not in {"fetch", "absent", "fetch_error"} else None,
        fetch_member=AsyncMock(return_value=member if permission == "fetch" else None),
    )
    if permission == "fetch_error":
        guild.fetch_member.side_effect = ConnectionError
    pool = SimpleNamespace(
        database=MemoryDb(), get_all_bots=lambda: [SimpleNamespace(get_guild=lambda gid: guild if gid == 500 else None)]
    )
    from empiremuse.utils.radio.favorites import RadioFavorites

    store = RadioFavorites(pool)
    allowed = permission in {"manage_guild", "administrator", "owner", "fetch"}
    assert await can_manage_guild(pool, 500, 7) is allowed
    assert not await can_manage_guild(pool, 501, 7)
    if allowed:
        await store.add(500, 7, station(1))
        assert await store.remove(500, 7, uuid(1))
        assert not await store.remove(500, 7, uuid(1))
        await store.add(500, 7, station(2))
        assert await store.clear(500, 7) and not await store.clear(500, 7)
    else:
        for method, args in ((store.add, (station(1),)), (store.remove, (uuid(1),)), (store.clear, ())):
            with pytest.raises(FavoriteForbidden):
                await method(500, 7, *args)
        assert pool.database.tables == {}


async def test_invalid_uuid_never_reads_and_invalid_removes_do_not_create(world):
    find = world.db.find_doc
    world.db.find_doc = AsyncMock(side_effect=find)
    with pytest.raises(FavoriteNotFound):
        await world.store.toggle_station(7, "bad")
    world.db.find_doc.assert_not_awaited()
    assert not await world.store.remove_station(7, "bad")
    assert not await world.store.remove_filter(7, "bad!")
    assert world.db.tables[USER_FAVORITES_DB] == {}


async def test_obsolete_filter_toggles_off_and_bad_filter_column_is_ignored(world):
    world.db.tables[USER_FAVORITES_DB] = {"7": {"rev": 0, "stations": None, "filters": [{"node": "node:old"}]}}
    world.hub.locations = SimpleNamespace(node=lambda value: None)
    assert await world.store.filters(7) == []
    assert await world.store.toggle_filter(7, "node:old") == Toggled.REMOVED
    world.db.tables[USER_FAVORITES_DB]["7"]["filters"] = "bad"
    assert await world.store.filters(7) == []
