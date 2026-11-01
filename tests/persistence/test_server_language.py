"""``settings.language`` no store (item 21, I-12): a migração de boot só mexe nesse campo, sob a trava e com CAS, e roda uma vez."""

import logging
from copy import deepcopy
from types import SimpleNamespace

import pytest

from empiremuse.rules.servers import new_doc
from empiremuse.utils.servers import ServerStore
from tests.persistence.test_servers_store import GID, MAIN_ID, RESERVE_ID, SERVERS, Banco, make_bot, run

pytestmark = pytest.mark.critical

OTHER = 222
THIRD = 333


def stored(**settings):
    doc = {**new_doc(), "_id": str(GID)}
    doc["migrated"] = {section: True for section in doc["migrated"]}
    doc["settings"] = {key: value for key, value in doc["settings"].items() if key != "language"} | settings
    doc["rev"] = 4
    return doc


@pytest.fixture
def world():
    banco = Banco()
    bots = [make_bot(MAIN_ID, "TOKEN", guilds=(GID, OTHER, THIRD)), make_bot(RESERVE_ID, "TOKEN_2", guilds=(GID,))]
    pool = SimpleNamespace(database=banco, get_all_bots=lambda: bots)
    return SimpleNamespace(banco=banco, store=ServerStore(pool))


def test_a_document_without_the_key_becomes_pt_br_and_only_that_field_changes(world):
    document = stored(autoplay=True, djroles=["10"])
    world.banco.put(*SERVERS, GID, deepcopy(document))

    assert run(world.store.migrate_language()) == (1, 0)

    saved = world.banco.servers_doc()
    assert saved["settings"]["language"] == "pt-BR" and saved["rev"] == 5
    assert {k: v for k, v in saved["settings"].items() if k != "language"} == document["settings"]
    assert {k: v for k, v in saved.items() if k not in ("settings", "rev")} == {
        k: v for k, v in document.items() if k not in ("settings", "rev")
    }


@pytest.mark.parametrize("value", [None, "de", "en-US", "fr"])
def test_a_document_with_the_key_is_left_alone_even_when_it_is_none(world, value):
    world.banco.put(*SERVERS, GID, stored(language=value))

    assert run(world.store.migrate_language()) == (0, 0)

    assert world.banco.saves == [] and world.banco.servers_doc()["settings"]["language"] == value


def test_the_second_run_changes_nothing(world, caplog):
    world.banco.put(*SERVERS, GID, stored())

    with caplog.at_level(logging.INFO):
        assert run(world.store.migrate_language()) == (1, 0)
    assert run(world.store.migrate_language()) == (0, 0)

    assert len(world.banco.saves) == 1 and world.banco.servers_doc()["rev"] == 5
    assert "pt-BR" in caplog.text and str(GID) not in caplog.text  # só contagens


def test_a_server_that_fails_stays_for_the_next_boot_and_the_others_go_on(world):
    world.banco.put(*SERVERS, GID, stored())
    world.banco.put(*SERVERS, OTHER, {**stored(), "_id": str(OTHER)})
    original = world.banco.save_doc_cas

    async def flaky(gid, doc, **kwargs):
        if gid == str(GID):
            raise RuntimeError("banco fora")
        return await original(gid, doc, **kwargs)

    world.banco.save_doc_cas = flaky

    assert run(world.store.migrate_language()) == (1, 1)

    assert "language" not in world.banco.servers_doc()["settings"]
    assert world.banco.servers_doc(OTHER)["settings"]["language"] == "pt-BR"


def test_a_document_without_a_settings_section_is_skipped(world):
    world.banco.put(*SERVERS, GID, {"_id": str(GID), "ver": 1, "rev": 1, "migrated": {}})

    assert run(world.store.migrate_language()) == (0, 0)


def test_a_new_document_is_automatic_and_one_built_from_the_old_format_is_pt_br(world):
    world.banco.put(
        "guilds", str(MAIN_ID), GID, {"_id": str(GID), "player_controller": {"channel": "5", "message_id": "6"}}
    )

    assert run(world.store.settings(GID))["language"] == "pt-BR"
    assert run(world.store.settings(OTHER))["language"] is None
