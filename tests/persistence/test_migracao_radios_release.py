"""A migração de subida ``radios_release`` (contrato §3, risco ALTO: grava e apaga dados dos servidores): campos novos, cópia, ``$unset``, CAS e retomada.

Roda no banco local de verdade (TinyMongo), não num dublê: o que prova o ``$unset`` é o motor, não um dicionário.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from empiremuse.utils.db import LocalDatabase
from empiremuse.utils.migrations import radios_release as migration

pytestmark = pytest.mark.critical

G = "global"


def legacy_doc(guild="111", *, strict=True, channel="77", message="88", rev=3):
    return {
        "_id": guild,
        "ver": 1,
        "rev": rev,
        "settings": {"request_channel_strict": strict, "autoplay": True},
        "request": {"channel": channel, "message_id": message, "panel_bot_id": "9"},
        "migrated": {"settings": True, "request": True, "playlists": True},
    }


@pytest.fixture
def world(tmp_path):
    database = LocalDatabase(str(tmp_path))
    pool = SimpleNamespace(database=database)
    return SimpleNamespace(db=database, pool=pool, table=database._connect[G]["servers"])


def put(world, doc):
    world.table.insert_one(dict(doc))


def stored(world, guild="111"):
    return world.db._connect[G]["servers"].find_one({"_id": guild})


def run(world):
    return asyncio.run(migration.run(world.pool))


class TestHappyPath:
    def test_the_new_fields_are_written_the_old_ones_are_unset_and_the_rev_advances(self, world):
        put(world, legacy_doc())

        report = run(world)

        doc = stored(world)
        assert (report.migrated, report.left_behind) == (1, 0)
        assert doc["settings"] == {"autoplay": True, "jukebox_auto_delete": True, "jukebox_text_requests": True}
        assert doc["jukebox"] == {"channel": "77", "message_id": "88", "panel_bot_id": "9"}
        assert doc["migrated"] == {"settings": True, "playlists": True, "jukebox": True}
        assert "request" not in doc and doc["rev"] == 5  # dois degraus: gravar o novo e apagar o antigo

    def test_the_strict_flag_off_becomes_auto_delete_off_and_text_requests_default_on(self, world):
        put(world, legacy_doc(strict=False))

        run(world)

        settings = stored(world)["settings"]
        assert settings["jukebox_auto_delete"] is False and settings["jukebox_text_requests"] is True

    def test_a_document_without_a_channel_keeps_an_empty_jukebox_section(self, world):
        put(world, legacy_doc(channel=None, message=None))

        run(world)

        assert (
            stored(world)["jukebox"] == {"channel": None, "message_id": None, "panel_bot_id": "9"}
            or stored(world)["jukebox"]["channel"] is None
        )

    def test_other_servers_and_unrelated_fields_are_left_alone(self, world):
        put(world, legacy_doc("111"))
        other = {
            "_id": "222",
            "ver": 1,
            "rev": 0,
            "settings": {"jukebox_auto_delete": False, "jukebox_text_requests": True},
            "jukebox": {"channel": None, "message_id": None, "panel_bot_id": None},
            "migrated": {"jukebox": True},
        }
        put(world, other)

        report = run(world)

        assert (report.migrated, report.already) == (1, 1) and stored(world, "222") == other


class TestBackup:
    def test_the_original_document_is_saved_before_the_old_fields_go_with_a_90_day_ttl(self, world):
        original = legacy_doc()
        put(world, original)

        run(world)

        backup = world.db._connect[G]["migration_backup"].find_one({"_id": "radios_release:servers:111"})
        assert (
            backup["old"]["request"] == original["request"]
            and backup["old"]["settings"]["request_channel_strict"] is True
        )
        assert "_id" not in backup["old"]
        expires = (
            backup["expire_at"]
            if isinstance(backup["expire_at"], datetime)
            else datetime.fromisoformat(str(backup["expire_at"]))
        )
        stamp = backup["at"] if isinstance(backup["at"], datetime) else datetime.fromisoformat(str(backup["at"]))
        assert timedelta(days=89) < expires - stamp <= timedelta(days=90, minutes=1)

    def test_the_ttl_index_is_requested_on_the_backup_collection(self, world):
        asked = []

        async def ensure(*, db_name, collection):
            asked.append((db_name, collection))

        world.db.ensure_event_indexes = ensure
        put(world, legacy_doc())

        run(world)

        assert asked == [("migration_backup", "global")]

    def test_a_failed_backup_leaves_the_old_fields_and_no_marker_and_the_next_boot_finishes(self, world, monkeypatch):
        put(world, legacy_doc())
        real = world.db.save_doc_cas

        async def flaky(id_, doc, *, expected_rev, db_name, collection):
            if db_name == "migration_backup":
                raise ConnectionError("mongo fora")
            return await real(id_, doc, expected_rev=expected_rev, db_name=db_name, collection=collection)

        monkeypatch.setattr(world.db, "save_doc_cas", flaky)
        first = run(world)

        assert first.left_behind == 1 and "request" in stored(world)  # nada foi apagado sem a cópia
        assert world.db._connect[G]["migrations"].find_one({"_id": "radios_release"}) is None

        monkeypatch.setattr(world.db, "save_doc_cas", real)
        second = run(world)

        assert second.left_behind == 0 and "request" not in stored(world)
        assert world.db._connect[G]["migrations"].find_one({"_id": "radios_release"}) is not None

    def test_a_second_run_never_overwrites_the_first_backup(self, world):
        original = legacy_doc()
        put(world, original)
        run(world)
        world.db._connect[G]["migrations"].delete_one({"_id": "radios_release"})
        world.table.update_one({"_id": "111"}, {"$set": {"request": {"channel": "NOVO"}}})

        run(world)

        backup = world.db._connect[G]["migration_backup"].find_one({"_id": "radios_release:servers:111"})
        assert backup["old"]["request"]["channel"] == "77"


class TestCasAndMarker:
    def test_the_marker_makes_the_next_boot_a_no_op(self, world):
        put(world, legacy_doc())
        run(world)

        again = run(world)

        assert again.skipped_marker is True and again.migrated == 0

    def test_a_concurrent_writer_that_bumps_the_rev_leaves_the_document_for_the_next_boot(self, world, monkeypatch):
        put(world, legacy_doc())
        real = world.db.save_doc_cas
        bumped = []

        async def racing(id_, doc, *, expected_rev, db_name, collection):
            if db_name == "servers" and not bumped:
                bumped.append(True)
                world.table.update_one({"_id": "111"}, {"$set": {"rev": 99}})  # outro escritor passou na frente
            return await real(id_, doc, expected_rev=expected_rev, db_name=db_name, collection=collection)

        monkeypatch.setattr(world.db, "save_doc_cas", racing)

        report = run(world)

        assert report.left_behind == 1 and "request" in stored(world) and stored(world)["rev"] == 99
        assert world.db._connect[G]["migrations"].find_one({"_id": "radios_release"}) is None

    def test_an_empty_database_still_writes_the_marker(self, world):
        report = run(world)

        assert (report.migrated, report.left_behind) == (0, 0)
        assert world.db._connect[G]["migrations"].find_one({"_id": "radios_release"}) is not None


class TestUnsetCas:
    def test_unset_removes_dotted_paths_and_advances_the_rev_only_when_the_rev_matches(self, world):
        put(world, {"_id": "1", "rev": 4, "a": {"b": 1, "c": 2}, "d": 3})

        assert (
            asyncio.run(world.db.unset_fields_cas("1", ["a.b", "d"], expected_rev=9, db_name="servers", collection=G))
            is False
        )
        assert (
            asyncio.run(world.db.unset_fields_cas("1", ["a.b", "d"], expected_rev=4, db_name="servers", collection=G))
            is True
        )

        assert stored(world, "1") == {"_id": "1", "rev": 5, "a": {"c": 2}}

    def test_an_empty_field_list_only_advances_the_rev(self, world):
        put(world, {"_id": "1", "rev": 0, "x": 1})

        assert asyncio.run(world.db.unset_fields_cas("1", [], expected_rev=0, db_name="servers", collection=G)) is True

        assert stored(world, "1") == {"_id": "1", "rev": 1, "x": 1}
