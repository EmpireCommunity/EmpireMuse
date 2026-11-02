"""``ServerStore.peek_section`` e ``ServerJukebox.peek_message_id`` (item 8, J1): leitura do cache SEM cópia.

Quem apaga mensagem em qualquer canal de qualquer servidor dispara um evento por bot; decidir "não é o painel" não pode custar um
``deepcopy`` do documento inteiro (com as playlists).
"""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from empiremuse.rules.servers import new_doc
from empiremuse.utils.server_jukebox import ServerJukebox
from empiremuse.utils.servers import ServerStore
from tests.persistence.test_servers_store import GID, MAIN_ID, RESERVE_ID, SERVERS, Banco, make_bot, run

pytestmark = pytest.mark.critical


@pytest.fixture
def world():
    banco = Banco()
    bots = [make_bot(MAIN_ID, "TOKEN", guilds=(GID,)), make_bot(RESERVE_ID, "TOKEN_2", guilds=(GID,))]
    store = ServerStore(SimpleNamespace(database=banco, get_all_bots=lambda: bots))
    return SimpleNamespace(banco=banco, store=store, request=ServerJukebox(store))


def stored(message_id):
    doc = {**new_doc(), "_id": str(GID)}
    doc["migrated"] = {section: True for section in doc["migrated"]}
    doc["request"] = {"channel": "7", "message_id": message_id, "panel_bot_id": None}
    return doc


def test_a_document_that_is_not_in_the_cache_is_unknown(world):
    world.banco.put(*SERVERS, GID, stored("500"))

    assert world.request.peek_message_id(GID) is None


def test_after_one_real_read_the_panel_message_id_comes_from_the_cache_without_copying(world):
    world.banco.put(*SERVERS, GID, stored("500"))
    run(world.request.get(GID))

    assert world.request.peek_message_id(GID) == 500
    assert (
        world.store.peek_section(GID, "request") is world.store._docs[str(GID)]["request"]
    )  # a mesma seção, não uma cópia


def test_a_panel_without_a_message_peeks_as_zero_not_as_unknown(world):
    world.banco.put(*SERVERS, GID, stored(None))
    run(world.request.get(GID))

    assert world.request.peek_message_id(GID) == 0


def test_a_section_that_was_not_migrated_yet_is_unknown(world):
    doc = deepcopy(stored("500"))
    doc["migrated"]["request"] = False
    world.store._docs[str(GID)] = doc

    assert world.store.peek_section(GID, "request") is None
