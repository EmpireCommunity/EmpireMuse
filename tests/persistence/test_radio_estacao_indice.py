"""BK-4b: o índice único (servidor, nome) das estações no MongoDB e o TTL do cache de faixas resolvidas.

O índice nunca derruba a subida (instalação própria com nomes repetidos só ganha um aviso com a CONTAGEM), e a escrita que bate nele
recebe a mesma recusa de "nome já usado" da regra. O cache grava a data de expiração ao lado do ``at`` para o TTL do MongoDB.
"""

import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pymongo.errors import DuplicateKeyError

from empiremuse.rules.radio_shuffle import Track
from empiremuse.rules.radio_station import STATION_NAME_TAKEN
from empiremuse.utils.db import LocalDatabase, UniqueIndexViolation, _is_id_key
from empiremuse.utils.radio import RadioHub
from empiremuse.utils.radio.custom_source import (
    EXPIRE_FIELD,
    FAIL_TTL_S,
    RESOLVED_DB,
    RESOLVED_TTL_S,
    ResolvedTracks,
    failure_id,
)
from empiremuse.utils.radio.stations import NAME_INDEX_KEYS, NAME_INDEX_PARTIAL, StationInvalid
from tests.persistence.test_radio_estacoes import ROCK
from tests.persistence.test_radio_store import GUILD, FakeDb

pytestmark = pytest.mark.critical

NOW = 1_700_000_000.0


class IndexedDb(FakeDb):
    """O ``FakeDb`` com os métodos de índice: ``index_error`` simula a criação recusada; ``write_error`` a escrita que bate no índice."""

    def __init__(self, *, index_error=None, groups=0, write_error=None):
        super().__init__()
        self.index_error, self.groups, self.write_error = index_error, groups, write_error
        self.indexes, self.ttl = [], []

    async def ensure_unique_index(self, keys, *, partial, name, db_name, collection):
        if self.index_error is not None:
            raise self.index_error
        self.indexes.append((db_name, keys, partial, name))

    async def count_duplicate_groups(self, keys, *, match, db_name, collection):
        if isinstance(self.groups, Exception):
            raise self.groups
        return self.groups

    async def ensure_ttl_index(self, field, *, db_name, collection):
        self.ttl.append((db_name, field))

    async def save_doc_cas(self, id_, doc, *, expected_rev, db_name, collection):
        if self.write_error is not None and db_name == "radio_stations":
            raise self.write_error
        return await super().save_doc_cas(id_, doc, expected_rev=expected_rev, db_name=db_name, collection=collection)


def hub_with(db):
    return RadioHub(SimpleNamespace(database=db))


class TestTheIndexAtBoot:
    @pytest.mark.asyncio
    async def test_the_partial_unique_index_is_asked_for_on_the_station_collection(self):
        db = IndexedDb()

        await hub_with(db).stations.ensure_indexes()

        assert db.indexes == [("radio_stations", NAME_INDEX_KEYS, NAME_INDEX_PARTIAL, "guild_name_norm_unique")]
        assert NAME_INDEX_KEYS == [("guild_id", 1), ("name_norm", 1)]
        assert NAME_INDEX_PARTIAL == {"name_norm": {"$type": "string"}}

    @pytest.mark.regression
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("index_error", "groups", "expected"),
        [
            (UniqueIndexViolation(), 3, "3 grupos"),  # nomes já repetidos (instalação própria): só a contagem
            (UniqueIndexViolation(), RuntimeError("fora"), "? (RuntimeError) grupos"),  # nem a contagem: segue
            (ConnectionError("mongo fora"), 0, "(ConnectionError)"),  # qualquer outra falha: segue
        ],
    )
    async def test_a_refused_index_only_warns_and_the_boot_goes_on(self, caplog, index_error, groups, expected):
        db = IndexedDb(index_error=index_error, groups=groups)

        with caplog.at_level(logging.WARNING):
            await hub_with(db).stations.ensure_indexes()  # não levanta

        assert any(expected in record.getMessage() for record in caplog.records), caplog.text

    @pytest.mark.asyncio
    async def test_the_resolved_cache_asks_for_the_ttl_index_and_a_failure_only_warns(self, caplog):
        db = IndexedDb()
        await ResolvedTracks(db).ensure_indexes()
        assert db.ttl == [(RESOLVED_DB, EXPIRE_FIELD)]

        broken = SimpleNamespace(ensure_ttl_index=None)  # chamar ``None`` levanta TypeError
        with caplog.at_level(logging.WARNING):
            await ResolvedTracks(broken).ensure_indexes()
        assert "TypeError" in caplog.text


class TestAWriteThatHitsTheIndex:
    @pytest.mark.regression
    @pytest.mark.asyncio
    async def test_create_and_update_get_the_same_name_taken_refusal_as_the_rule(self):
        db = IndexedDb()
        hub = hub_with(db)
        station = await hub.stations.create(GUILD, ROCK)
        db.write_error = UniqueIndexViolation()  # outro processo gravou o mesmo nome entre a leitura e a escrita

        with pytest.raises(StationInvalid) as created:
            await hub.stations.create(GUILD, {**ROCK, "name": "Jazz"})
        with pytest.raises(StationInvalid) as updated:
            await hub.stations.update(GUILD, station.id, {**ROCK, "name": "Jazz"}, expected_rev=station.rev)

        for refused in (created, updated):
            assert (refused.value.error.code, refused.value.error.key) == (STATION_NAME_TAKEN, "name")


class TestTheMongoDuplicateKey:
    @pytest.mark.parametrize(
        ("details", "is_id"),
        [
            ({"keyPattern": {"_id": 1}}, True),  # o documento já existe: o CAS devolve False
            ({"keyPattern": {"guild_id": 1, "name_norm": 1}}, False),  # o índice de nome: UniqueIndexViolation
            (None, True),  # sem detalhe nenhum: o comportamento de antes
            # Sentinela BK-4b: sem ``keyPattern`` (servidor antigo), o ``keyValue`` e o nome do índice na mensagem ainda dizem qual foi
            ({"keyValue": {"guild_id": "1", "name_norm": "rock"}}, False),
            ({"keyValue": {"_id": "a"}}, True),
            (
                {
                    "errmsg": "E11000 duplicate key error collection: g.radio_stations index: guild_name_norm_unique dup key"
                },
                False,
            ),
            (
                {"errmsg": 'E11000 duplicate key error collection: g.radio_stations index: _id_ dup key: { _id: "a" }'},
                True,
            ),
        ],
    )
    def test_only_the_id_key_means_the_document_already_exists(self, details, is_id):
        assert _is_id_key(DuplicateKeyError("E11000", 11000, details)) is is_id


class TestTheExpiryDateIsWrittenWithTheEntry:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("kind", "ttl"), [("fail", FAIL_TTL_S), ("resolved", RESOLVED_TTL_S)])
    async def test_expire_at_is_the_same_deadline_the_code_checks(self, kind, ttl):
        db = FakeDb()
        cache = ResolvedTracks(db, clock=lambda: NOW)
        track = Track("Djavan", "Oceano")

        if kind == "fail":
            await cache.mark(track)
        else:
            await cache.store(track, SimpleNamespace(id="QAAAx", info={"uri": "https://example.test/x", "length": 1}))

        stored = await db.find_doc(failure_id(track), db_name=RESOLVED_DB, collection="global")
        assert stored["at"] == NOW
        assert stored[EXPIRE_FIELD] == datetime.fromtimestamp(NOW + ttl, tz=timezone.utc)

    @pytest.mark.asyncio
    async def test_the_local_database_keeps_the_date_and_an_old_entry_without_it_still_counts(self, tmp_path):
        database = LocalDatabase(str(tmp_path))
        cache = ResolvedTracks(database, clock=lambda: NOW)
        track, old = Track("Djavan", "Oceano"), Track("Gal", "Baby")

        await cache.mark(track)
        await database.save_doc_cas(
            failure_id(old),
            {"fail": True, "at": NOW, "rev": 1},
            expected_rev=None,
            db_name=RESOLVED_DB,
            collection="global",
        )  # gravada antes do BK-4b: sem ``expire_at``

        stored = await database.find_doc(failure_id(track), db_name=RESOLVED_DB, collection="global")
        assert isinstance(stored[EXPIRE_FIELD], datetime)
        fresh = ResolvedTracks(database, clock=lambda: NOW)
        assert await fresh.failed(track) and await fresh.failed(old)
