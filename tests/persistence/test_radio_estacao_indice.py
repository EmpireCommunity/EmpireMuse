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
from empiremuse.utils.db import IndexDefinitionConflict, LocalDatabase, UniqueIndexViolation, _is_id_key
from empiremuse.utils.radio import INDEX_RETRY_DELAYS_S, RadioHub
from empiremuse.utils.radio.custom_source import (
    EXPIRE_FIELD,
    FAIL_TTL_S,
    RESOLVED_DB,
    RESOLVED_TTL_S,
    ResolvedTracks,
    failure_id,
)
from empiremuse.utils.radio.stations import StationInvalid
from tests.persistence.test_radio_estacoes import ROCK
from tests.persistence.test_radio_store import GUILD, FakeDb

pytestmark = pytest.mark.critical

NOW = 1_700_000_000.0


class IndexedDb(FakeDb):
    """O ``FakeDb`` com os métodos de índice: ``index_error`` simula a criação recusada; ``write_error`` a escrita que bate no índice."""

    def __init__(self, *, index_error=None, groups=0, write_error=None):
        super().__init__()
        self.index_error, self.groups, self.write_error = index_error, groups, write_error
        self.indexes, self.ttl, self.purged = [], [], []
        self.ttl_errors, self.legacy = [], 0

    async def ensure_unique_index(self, keys, *, partial, name, db_name, collection):
        if self.index_error is not None:
            raise self.index_error
        self.indexes.append((db_name, keys, partial, name))

    async def count_duplicate_groups(self, keys, *, match, db_name, collection):
        if isinstance(self.groups, Exception):
            raise self.groups
        return self.groups

    async def ensure_ttl_index(self, field, *, db_name, collection):
        if self.ttl_errors:
            raise self.ttl_errors.pop(0)
        self.ttl.append((db_name, field))

    async def delete_matching(self, filter, *, db_name, collection):
        self.purged.append((db_name, filter))
        return self.legacy

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

        assert db.indexes == [
            (
                "radio_stations",
                [("guild_id", 1), ("name_norm", 1)],
                {"name_norm": {"$type": "string"}},
                "guild_name_norm_unique",
            )
        ]

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
    async def test_the_resolved_cache_asks_for_the_ttl_index_purges_old_entries_once_and_a_failure_only_warns(
        self, caplog
    ):
        db = IndexedDb()
        db.legacy = 7  # entradas gravadas antes do BK-4b, sem ``expire_at``
        with caplog.at_level(logging.INFO):
            assert await ResolvedTracks(db).ensure_indexes() is True
        assert db.ttl == [(RESOLVED_DB, EXPIRE_FIELD)]
        assert db.purged == [(RESOLVED_DB, {EXPIRE_FIELD: {"$exists": False}})]
        assert "7 entradas antigas" in caplog.text  # só a contagem

        broken = SimpleNamespace(ensure_ttl_index=None)  # chamar ``None`` levanta TypeError
        with caplog.at_level(logging.WARNING):
            assert await ResolvedTracks(broken).ensure_indexes() is False
        assert "TypeError" in caplog.text


class TestTheIndexesRunInTheBackgroundWithBoundedRetries:
    """Alicerce BK-4b: os índices não seguram a retomada e uma falha passageira tenta de novo, poucas vezes (sem relógio real)."""

    @staticmethod
    async def run(db):
        waits = []

        async def sleep(seconds):
            waits.append(seconds)

        await hub_with(db).ensure_indexes(sleep=sleep)
        return waits

    @pytest.mark.asyncio
    async def test_a_passing_failure_is_retried_and_the_part_already_done_is_not_redone(self):
        db = IndexedDb()
        db.ttl_errors = [ConnectionError("mongo fora")]

        waits = await self.run(db)

        assert waits == [INDEX_RETRY_DELAYS_S[0]]
        assert len(db.indexes) == 1 and db.ttl == [(RESOLVED_DB, EXPIRE_FIELD)]  # o de nome não é refeito

    @pytest.mark.asyncio
    async def test_it_gives_up_after_the_last_attempt_with_a_warning(self, caplog):
        db = IndexedDb()
        db.ttl_errors = [ConnectionError("mongo fora")] * (len(INDEX_RETRY_DELAYS_S) + 1)

        with caplog.at_level(logging.WARNING):
            waits = await self.run(db)

        assert waits == list(INDEX_RETRY_DELAYS_S)
        assert "1 parte(s) não criada(s)" in caplog.text

    @pytest.mark.asyncio
    async def test_repeated_names_are_final_and_never_retried(self):
        db = IndexedDb(index_error=UniqueIndexViolation(), groups=2)

        assert await self.run(db) == []

    @pytest.mark.regression
    @pytest.mark.asyncio
    async def test_an_index_defined_otherwise_by_hand_is_final_with_the_type_in_the_warning(self, caplog):
        """Crivo BK-4b CS2: índice com o mesmo nome e outras opções (códigos 85/86 do MongoDB) não se resolve tentando de novo."""
        db = IndexedDb(index_error=IndexDefinitionConflict())
        db.ttl_errors = [IndexDefinitionConflict()]

        with caplog.at_level(logging.WARNING):
            assert await self.run(db) == []

        assert caplog.text.count("IndexDefinitionConflict") == 2


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


class TestTheUnknownArtistsWriteNeverBreaksTheRadio:
    @pytest.mark.regression
    @pytest.mark.asyncio
    async def test_a_legacy_station_that_repeats_a_name_gives_up_silently(self):
        """Alicerce BK-4b: a estação legada sem ``name_norm`` que repete o nome de outra bate no índice ao gravar os ⚠️; o carregamento da rádio
        não pode receber ``StationInvalid`` (desiste em silêncio, como ao perder a corrida)."""
        db = IndexedDb()
        hub = hub_with(db)
        station = await hub.stations.create(GUILD, ROCK)
        db.write_error = UniqueIndexViolation()

        await hub.stations.set_unknown(GUILD, station.id, ["Xyzzy"])  # não levanta

        assert (await hub.stations.get(GUILD, station.id)).unknown == ()
