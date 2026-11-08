"""Campos legados de request ficam inertes: sem migração/cópia e com padrões da Jukebox."""

from copy import deepcopy

import pytest

from empiremuse.rules.servers import jukebox_auto_delete, jukebox_text_requests
from tests.persistence.test_servers_store import world, GID, SERVERS  # noqa: F401

pytestmark = pytest.mark.critical


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["get", "get_sections"])
async def test_old_request_fields_are_tolerated_and_jukebox_defaults_are_available(world, api):  # noqa: F811
    doc = {
        "_id": str(GID),
        "ver": 1,
        "rev": 0,
        "request": {"channel": "22", "message_id": "23"},
        "settings": {"request_channel_strict": True},
        "migrated": {"settings": True, "request": True},
    }
    world.banco.put(*SERVERS, GID, doc)

    async def find(id_, *, db_name, collection):
        return deepcopy(world.banco.docs.get((db_name, collection, str(id_))))

    async def save(id_, doc, *, expected_rev, db_name, collection):
        key = (db_name, collection, str(id_))
        current = world.banco.docs.get(key)
        assert current["rev"] == expected_rev
        world.banco.docs[key] = deepcopy(doc)
        return True

    world.banco.find_doc, world.banco.save_doc_cas = find, save
    loaded = await world.store.get(GID) if api == "get" else await world.store.get_sections(GID, "settings", "jukebox")
    assert jukebox_auto_delete(loaded["settings"]) is False
    assert jukebox_text_requests(loaded["settings"]) is True
    assert loaded["request"] == doc["request"]
