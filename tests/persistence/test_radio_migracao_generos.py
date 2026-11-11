"""J1: refs virtuais, CAS, exclusão por id e dados crus hostis (contrato §5/m6)."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.utils.erasure import ErasureTombstones

from empiremuse.rules.radio_station import CustomStation, to_document
from empiremuse.utils.radio import RadioHub, migrate_genres
from empiremuse.utils.radio.stations import STATIONS_DB, StationInvalid, StationNotFound
from empiremuse.utils.radio.store import NoPremium, RadioConflict, RadioInvalid
from tests.persistence.test_radio_store import FakeDb, GUILD, make_radio

pytestmark = pytest.mark.critical
SID = "a" * 24


@pytest.fixture
def world():
    db = FakeDb()
    hub = RadioHub(SimpleNamespace(database=db, erasure_tombstones=ErasureTombstones()))
    # Usa o escritor real: guild_id textual canônico, como na produção.
    db._table(STATIONS_DB)[SID] = to_document(CustomStation(SID, GUILD, "Rock", "genre", {"genre": "rock"}))
    return SimpleNamespace(db=db, hub=hub)


async def marker(w):
    return await w.db.find_doc(migrate_genres.MARKER_ID, db_name="migrations", collection="global")


@pytest.mark.asyncio
async def test_migrates_radio_and_same_guild_favorites_then_deletes_and_is_idempotent(world):
    radio = await world.hub.store.create(make_radio(source={"station": SID}))
    original = deepcopy(world.db._table("radios")[radio.id])
    world.db._table("radios")[radio.id]["future_field"] = "preserved"
    table = world.db._table("radio_custom_favorites")
    table[f"{GUILD}:7"] = {
        "_id": f"{GUILD}:7",
        "stations": [SID, "g:rock", *[f"{i:024x}" for i in range(30)]],
        "rev": 0,
    }
    table[f"{GUILD + 1}:7"] = {"_id": f"{GUILD + 1}:7", "stations": [SID], "rev": 0}
    report = await migrate_genres.run(world.hub)
    stored = world.db._table("radios")[radio.id]
    assert report.migrated == report.removed == 1
    assert stored == {**original, "source": {"station": "g:rock"}, "rev": 1, "future_field": "preserved"}
    ids = table[f"{GUILD}:7"]["stations"]
    assert ids[0] == "g:rock" and len(ids) == len(set(ids)) == 24
    assert table[f"{GUILD + 1}:7"]["stations"] == [SID]
    assert SID not in world.db._table(STATIONS_DB) and await marker(world)
    snapshot = deepcopy(world.db.tables)
    assert (await migrate_genres.run(world.hub)).skipped_marker
    assert world.db.tables == snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["radio", "favorites"])
async def test_cas_conflicts_keep_station_and_retry_without_marker(world, conflict):
    await world.hub.store.create(make_radio(source={"station": SID}))
    if conflict == "radio":
        original = world.hub.store.replace_station_ref
        world.hub.store.replace_station_ref = AsyncMock(side_effect=RadioConflict)
    else:
        key = f"{GUILD}:7"
        world.db._table("radio_custom_favorites")[key] = {"_id": key, "stations": [SID], "rev": 0}
        original = world.db.save_doc_cas

        async def save(*args, **kwargs):
            return False if kwargs["db_name"] == "radio_custom_favorites" else await original(*args, **kwargs)

        world.db.save_doc_cas = save
    assert (await migrate_genres.run(world.hub)).left_behind == 1
    assert SID in world.db._table(STATIONS_DB) and await marker(world) is None
    if conflict == "radio":
        world.hub.store.replace_station_ref = original
    else:
        world.db.save_doc_cas = original
    assert (await migrate_genres.run(world.hub)).removed == 1
    assert await marker(world)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "edit", [{"kind": "artists", "source": {"artists": ["Queen"]}}, {"guild_id": "600"}, {"source": {"genre": "pop"}}]
)
async def test_reread_does_not_delete_a_station_changed_during_migration(world, edit):
    original = world.hub.store.list_all

    async def list_all():
        world.db._table(STATIONS_DB)[SID].update(edit)
        return await original()

    world.hub.store.list_all = list_all
    assert (await migrate_genres.run(world.hub)).left_behind == 1
    assert SID in world.db._table(STATIONS_DB) and await marker(world) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [{"genre": "retired"}, {"genre": "../evil"}, {"genre": []}, None])
async def test_retired_or_invalid_source_never_rewrites_refs_and_removes_only_valid_identity(world, source):
    await world.hub.store.create(make_radio(source={"station": SID}))
    world.db._table(STATIONS_DB)[SID]["source"] = source
    report = await migrate_genres.run(world.hub)
    assert report.removed == 1 and report.migrated == 0
    assert (await world.hub.store.get(f"1:{GUILD}")).source == {"station": SID}


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["007", " 7", "+7", "٧", "0", 0, -1, True, 1.0, None, 2**64, str(2**64)])
async def test_invalid_guilds_are_inert_and_all_invalid_prevents_marker(world, bad, caplog):
    world.db._table(STATIONS_DB)[SID]["guild_id"] = bad
    report = await migrate_genres.run(world.hub)
    assert report.inert == report.left_behind == 1
    assert SID in world.db._table(STATIONS_DB) and await marker(world) is None
    assert any(r.levelname == "ERROR" for r in caplog.records)


@pytest.mark.asyncio
async def test_invalid_id_is_inert_and_does_not_prevent_migrating_a_valid_document(world):
    world.db._table(STATIONS_DB)["bad"] = {**world.db._table(STATIONS_DB)[SID], "_id": "bad"}
    report = await migrate_genres.run(world.hub)
    assert report.inert == 1 and report.removed == 1
    assert "bad" in world.db._table(STATIONS_DB) and await marker(world)


@pytest.mark.asyncio
async def test_integer_guild_tolerance_and_no_genres_marker(world):
    world.db._table(STATIONS_DB)[SID]["guild_id"] = GUILD
    assert (await migrate_genres.run(world.hub)).removed == 1
    await world.db.delete_data(migrate_genres.MARKER_ID, "migrations", "global")
    assert (await migrate_genres.run(world.hub)).removed == 0 and await marker(world)


@pytest.mark.asyncio
async def test_builtin_catalog_is_virtual_scoped_immutable_and_favorites_accept_refs(world):
    store = world.hub.stations
    assert len(store.list_builtin()) == 33
    for gid in (GUILD, GUILD + 1):
        station = await store.get(gid, "g:rock")
        assert (station.id, station.guild_id, station.rev, station.source) == ("g:rock", gid, 0, {"genre": "rock"})
    assert await store.get(GUILD, "g:unknown") is None
    assert await store.get(GUILD, "g:../bad") is None
    for operation in (store.update, store.delete):
        with pytest.raises(StationNotFound):
            if operation == store.update:
                await operation(GUILD, "g:rock", {}, expected_rev=0)
            else:
                await operation(GUILD, "g:rock", expected_rev=0)
    with pytest.raises(StationInvalid):
        await store.create(GUILD, {"name": "Rock", "kind": "genre", "source": {"genre": "rock"}})
    await world.hub.custom_favorites.toggle(GUILD, 7, "g:rock")
    assert await world.hub.custom_favorites.list(GUILD, 7) == ["g:rock"]
    radio = await world.hub.store.create(make_radio())
    with pytest.raises(NoPremium):
        await world.hub.store.set_custom_station(radio.id, "g:rock", expected_rev=0)
    await world.hub.premium.grant(GUILD, by=1)
    saved = await world.hub.store.set_custom_station(radio.id, "g:rock", expected_rev=0)
    assert saved.source == {"station": "g:rock"}


@pytest.mark.asyncio
async def test_interrupted_delete_retries_after_refs_were_already_migrated(world):
    await world.hub.store.create(make_radio(source={"station": SID}))
    original = world.db.delete_data
    world.db.delete_data = AsyncMock(side_effect=ConnectionError("banco indisponível"))
    with pytest.raises(ConnectionError):
        await migrate_genres.run(world.hub)
    assert SID in world.db._table(STATIONS_DB) and await marker(world) is None
    assert (await world.hub.store.get(f"1:{GUILD}")).source == {"station": "g:rock"}
    world.db.delete_data = original
    assert (await migrate_genres.run(world.hub)).removed == 1
    assert await marker(world)


@pytest.mark.asyncio
async def test_premium_loss_is_a_refusal_in_chat_and_panel(world):
    from empiremuse.utils.radio.actions import Actor, Refused, change_custom_station
    from empiremuse.panel.services.radio_stations import StationsPage

    radio = await world.hub.store.create(make_radio(source={"station": "g:rock"}))
    world.hub.store.get = AsyncMock(return_value=SimpleNamespace(**{**radio.__dict__, "enabled": True}))
    world.hub.premium.fresh = AsyncMock(side_effect=[object(), None])
    with pytest.raises(Refused) as refused:
        await change_custom_station(
            world.hub, radio_id=radio.id, station_id="g:pop", actor=Actor(user_id=7, manage_guild=True)
        )
    assert refused.value.code == "no_premium"
    world.hub.premium.fresh = AsyncMock(return_value=None)
    ctx = SimpleNamespace(pool=SimpleNamespace(radio=world.hub), user_id="7")
    outcome = await StationsPage.set_custom_station(
        ctx, radio, {"versao": ["0"], "estacao": ["g:pop"]}, lambda key: key
    )
    assert outcome[0] == 403


@pytest.mark.asyncio
async def test_silent_delete_failure_keeps_marker_absent_and_is_not_counted(world):
    world.db.delete_data = AsyncMock(return_value=None)
    report = await migrate_genres.run(world.hub)
    assert report.removed == 0 and report.left_behind == 1
    assert SID in world.db._table(STATIONS_DB) and await marker(world) is None


@pytest.mark.asyncio
async def test_replace_station_ref_rejects_a_stale_revision_without_writing(world):
    radio = await world.hub.store.create(make_radio(source={"station": SID}))
    original = deepcopy(world.db._table("radios")[radio.id])
    with pytest.raises(RadioConflict):
        await world.hub.store.replace_station_ref(radio.id, "g:rock", expected_rev=radio.rev + 1)
    assert world.db._table("radios")[radio.id] == original


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,ref", [("live", "g:rock"), ("custom", "g:unknown"), ("custom", "g:../bad")])
async def test_replace_station_ref_rejects_noncustom_and_unknown_refs(world, kind, ref):
    source = {"station": "11111111-1111-1111-1111-111111111111"} if kind == "live" else {"station": SID}
    radio = await world.hub.store.create(make_radio(kind=kind, source=source))
    original = deepcopy(world.db._table("radios")[radio.id])
    with pytest.raises(RadioInvalid):
        await world.hub.store.replace_station_ref(radio.id, ref, expected_rev=radio.rev)
    assert world.db._table("radios")[radio.id] == original


@pytest.mark.asyncio
async def test_set_custom_station_rejects_unknown_builtin_with_active_premium(world):
    radio = await world.hub.store.create(make_radio(source={"station": SID}))
    await world.hub.premium.grant(GUILD, by=1)
    original = deepcopy(world.db._table("radios")[radio.id])
    with pytest.raises(RadioInvalid):
        await world.hub.store.set_custom_station(radio.id, "g:unknown", expected_rev=radio.rev)
    assert world.db._table("radios")[radio.id] == original


@pytest.mark.asyncio
@pytest.mark.parametrize("rev", ["1", None, True, -1, 1.0, {}])
async def test_invalid_favorite_revision_keeps_station_without_marker(world, rev):
    key = f"{GUILD}:7"
    original = {"_id": key, "stations": [SID], "rev": rev}
    world.db._table("radio_custom_favorites")[key] = deepcopy(original)
    report = await migrate_genres.run(world.hub)
    assert report.left_behind == 1 and report.removed == 0
    assert SID in world.db._table(STATIONS_DB) and await marker(world) is None
    assert world.db._table("radio_custom_favorites")[key] == original
