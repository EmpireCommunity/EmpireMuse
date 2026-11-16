"""A16 — ``MongoDatabase`` contra um ``mongo:8`` real, local e descartável.

Só roda com ``MONGO_TEST_URI`` apontando para localhost; sem a variável é pulado. Nunca lê ``MONGO`` nem o ``.env``: o banco
``global`` de produção guarda refresh tokens. O mongod sobe e fica pronto antes do pytest (gate); aqui não há espera por tempo.
Tudo roda num banco com nome único, apagado no fim.
"""

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from empiremuse.utils import db
from empiremuse.utils.db import DBModel, MongoDatabase
from tests.support.mongo_local import uri_so_local

pytestmark = pytest.mark.integration

URI = os.environ.get("MONGO_TEST_URI", "")

if not URI:
    pytest.skip("MONGO_TEST_URI ausente: sem mongod descartável", allow_module_level=True)

if not uri_so_local(URI):
    pytest.fail(
        "MONGO_TEST_URI precisa ser mongodb:// e todos os hosts locais (localhost, 127.0.0.1, ::1)", pytrace=False
    )


async def _ciclo():
    banco = MongoDatabase(URI, timeout=5)
    nome = f"litmus_{uuid.uuid4().hex}"
    versao = db.db_models[DBModel.guilds]["ver"]
    try:
        # sem documento: o padrão, sem inserir
        assert (await banco.get_data(1, db_name=DBModel.guilds, collection=nome))["ver"] == versao
        assert await banco.query_data(DBModel.guilds, nome) == []

        # push
        await banco.push_data({"_id": "2", "ver": versao, "autoplay": True}, db_name=DBModel.guilds, collection=nome)
        banco.cache.clear()
        assert (await banco.get_data(2, db_name=DBModel.guilds, collection=nome))["autoplay"] is True

        # migração por versão: salva de volta já na versão nova, preservando o valor gravado
        await banco.push_data({"_id": "3", "ver": 0.5, "djroles": ["4"]}, db_name=DBModel.guilds, collection=nome)
        migrado = await banco.get_data(3, db_name=DBModel.guilds, collection=nome)
        assert migrado["ver"] == versao and migrado["djroles"] == ["4"]
        banco.cache.clear()
        assert (await banco._connect[nome][DBModel.guilds].find_one({"_id": "3"}))["ver"] == versao

        # update com upsert (documento novo) e sobre o existente
        await banco.update_data(5, {"x": 1, "ver": versao}, db_name=DBModel.guilds, collection=nome)
        await banco.update_data(5, {"x": 2}, db_name=DBModel.guilds, collection=nome)
        assert (await banco._connect[nome][DBModel.guilds].find_one({"_id": "5"}))["x"] == 2

        # query com e sem limit, filtro e iter
        todos = await banco.query_data(DBModel.guilds, nome)
        assert sorted(d["_id"] for d in todos) == ["2", "3", "5"]
        assert len(await banco.query_data(DBModel.guilds, nome, limit=2)) == 2
        assert [d["_id"] for d in await banco.query_data(DBModel.guilds, nome, {"x": 2})] == ["5"]
        assert sorted([d["_id"] async for d in banco.iter_data(DBModel.guilds, nome)]) == ["2", "3", "5"]

        # delete invalida o cache
        banco.cache[f"{nome}:{DBModel.guilds}:2"] = {"velho": True}
        await banco.delete_data(2, DBModel.guilds, nome)
        assert f"{nome}:{DBModel.guilds}:2" not in banco.cache
        assert await banco._connect[nome][DBModel.guilds].find_one({"_id": "2"}) is None
    finally:
        await banco._connect.drop_database(nome)
        await banco._connect.close()


def test_mongo_database_round_trip_against_a_real_mongod():
    asyncio.run(_ciclo())


async def _ciclo_apagar_dados():
    """``delete_many`` e ``save_doc_cas`` (apagar meus dados, E1) contra o mongod real (o CI roda este arquivo com o serviço mongo, sem skip)."""
    banco = MongoDatabase(URI, timeout=5)
    nome = f"litmus_{uuid.uuid4().hex}"
    colecao = "radio_custom_favorites"
    try:
        for id_ in ("1:45", "2:45", "1:145"):
            await banco.update_data(id_, {"rev": 1}, db_name=colecao, collection=nome)
        banco.cache[f"{nome}:{colecao}:1:45"] = {"velho": True}

        apagados = await banco.delete_many(["1:45", "2:45", "9:45", "1:45"], db_name=colecao, collection=nome)

        assert apagados == 2  # o inexistente e o repetido não contam
        assert f"{nome}:{colecao}:1:45" not in banco.cache
        restantes = [d["_id"] async for d in banco.iter_data(db_name=colecao, collection=nome)]
        assert restantes == ["1:145"]
        assert await banco.delete_many([], db_name=colecao, collection=nome) == 0

        # iter_data com filtro (anonimização do updated_by): só os documentos do autor, sem varrer o resto
        for id_, autor in (("a", "45"), ("b", "145"), ("c", "45")):
            await banco.update_data(id_, {"rev": 1, "updated_by": autor}, db_name="radios", collection=nome)
        filtrados = [
            d["_id"] async for d in banco.iter_data(db_name="radios", collection=nome, filter={"updated_by": "45"})
        ]
        assert sorted(filtrados) == ["a", "c"]

        # compare-and-set: documento novo só se não existe; troca só se o rev bate
        assert await banco.save_doc_cas("r", {"rev": 1, "x": 1}, expected_rev=None, db_name=colecao, collection=nome)
        assert not await banco.save_doc_cas("r", {"rev": 9}, expected_rev=None, db_name=colecao, collection=nome)
        assert not await banco.save_doc_cas("r", {"rev": 3, "x": 2}, expected_rev=7, db_name=colecao, collection=nome)
        assert await banco.save_doc_cas("r", {"rev": 2, "x": 2}, expected_rev=1, db_name=colecao, collection=nome)
        guardado = await banco.find_doc("r", db_name=colecao, collection=nome)
        assert (guardado["rev"], guardado["x"]) == (2, 2)

        # delete_doc_cas: rev velho mantém o documento; o rev guardado apaga e tira do cache
        assert not await banco.delete_doc_cas("r", expected_rev=1, db_name=colecao, collection=nome)
        assert await banco.find_doc("r", db_name=colecao, collection=nome) is not None
        assert await banco.delete_doc_cas("r", expected_rev=2, db_name=colecao, collection=nome)
        assert await banco._connect[nome][colecao].find_one({"_id": "r"}) is None
        assert f"{nome}:{colecao}:r" not in banco.cache

        # documento legado sem ``rev`` (lido como 0): o rev 0 casa no save e no delete; outro rev não
        await banco._connect[nome][colecao].insert_one({"_id": "legado", "x": 1})
        await banco._connect[nome][colecao].insert_one({"_id": "legado2", "x": 1})
        assert not await banco.delete_doc_cas("legado", expected_rev=1, db_name=colecao, collection=nome)
        assert await banco.delete_doc_cas("legado", expected_rev=0, db_name=colecao, collection=nome)
        assert await banco._connect[nome][colecao].find_one({"_id": "legado"}) is None
        assert await banco.save_doc_cas("legado2", {"rev": 1, "x": 2}, expected_rev=0, db_name=colecao, collection=nome)
        assert (await banco._connect[nome][colecao].find_one({"_id": "legado2"}))["rev"] == 1
    finally:
        await banco._connect.drop_database(nome)
        await banco._connect.close()


def test_mongo_delete_many_and_save_doc_cas_against_a_real_mongod():
    asyncio.run(_ciclo_apagar_dados())


async def _ciclo_lista_de_banidos():
    """Lista de banidos (``banned_users``/``banned_guilds``): grava, relê sem cache, e o delete tira da lista."""
    banco = MongoDatabase(URI, timeout=5)
    nome = f"litmus_{uuid.uuid4().hex}"
    try:
        for modelo, id_ in ((DBModel.banned_users, 45), (DBModel.banned_guilds, 77)):
            await banco.update_data(id_, {"banned_at": 1, "reason": "teste"}, db_name=modelo, collection=nome)
            banco.cache.clear()
            lista = await banco.query_data(db_name=modelo, collection=nome)
            assert [(d["_id"], d["reason"]) for d in lista] == [(str(id_), "teste")]

            await banco.delete_data(id_, modelo, nome)
            assert await banco.query_data(db_name=modelo, collection=nome) == []
    finally:
        await banco._connect.drop_database(nome)
        await banco._connect.close()


def test_mongo_ban_list_round_trip_against_a_real_mongod():
    asyncio.run(_ciclo_lista_de_banidos())


async def _ciclo_registro_de_uso():
    """Registro de uso (``usage_events``): índices, insert em lote com TTL, patch e consulta por servidor, do mais novo ao mais velho."""
    banco = MongoDatabase(URI, timeout=5)
    nome = f"litmus_{uuid.uuid4().hex}"
    modelo = DBModel.usage_events
    try:
        await banco.ensure_event_indexes(db_name=modelo, collection=nome)
        await banco.ensure_event_indexes(db_name=modelo, collection=nome)  # idempotente
        indices = await banco._connect[nome][modelo].index_information()
        assert any(i.get("expireAfterSeconds") == 0 for i in indices.values())

        docs = [
            {"_id": "e1", "guild": 9, "ts": 100},
            {"_id": "e2", "guild": 9, "ts": 200},
            {"_id": "e3", "guild": 8, "ts": 300},
        ]
        await banco.append_events(docs, db_name=modelo, collection=nome, expire_after=timedelta(days=1))
        await banco.patch_event("e1", {"ok": True}, db_name=modelo, collection=nome)

        achados = await banco.find_events({"guild": 9, "ts": {"$gte": 0}}, db_name=modelo, collection=nome, limit=10)
        assert [d["_id"] for d in achados] == ["e2", "e1"]
        assert achados[1]["ok"] is True and "expire_at" in achados[0]
        assert len(await banco.find_events({}, db_name=modelo, collection=nome, limit=1)) == 1
    finally:
        await banco._connect.drop_database(nome)
        await banco._connect.close()


def test_mongo_usage_events_round_trip_against_a_real_mongod():
    asyncio.run(_ciclo_registro_de_uso())


async def _ciclo_indice_de_estacoes():
    """BK-4b: o índice único parcial (servidor, nome) recusa o nome repetido na inserção e na troca, deixa o legado sem ``name_norm`` de fora,
    não nasce sobre dados repetidos (só a contagem dos grupos) e o TTL é criado."""
    from empiremuse.utils.db import UniqueIndexViolation
    from empiremuse.utils.radio.stations import NAME_INDEX, NAME_INDEX_KEYS, NAME_INDEX_PARTIAL

    banco = MongoDatabase(URI, timeout=5)
    nome = f"litmus_{uuid.uuid4().hex}"
    colecao, repetida = "radio_stations", "radio_stations_dup"
    indice = {"partial": NAME_INDEX_PARTIAL, "name": NAME_INDEX, "collection": nome}
    try:
        await banco.ensure_unique_index(NAME_INDEX_KEYS, db_name=colecao, **indice)
        await banco.ensure_unique_index(NAME_INDEX_KEYS, db_name=colecao, **indice)  # idempotente
        rock = {"guild_id": "1", "name_norm": "rock", "rev": 0}
        assert await banco.save_doc_cas("a", rock, expected_rev=None, db_name=colecao, collection=nome)
        assert not await banco.save_doc_cas(
            "a", rock, expected_rev=None, db_name=colecao, collection=nome
        )  # o _id: CAS
        with pytest.raises(UniqueIndexViolation):
            await banco.save_doc_cas("b", rock, expected_rev=None, db_name=colecao, collection=nome)
        assert await banco.save_doc_cas(
            "c", {**rock, "guild_id": "2"}, expected_rev=None, db_name=colecao, collection=nome
        )
        assert await banco.save_doc_cas(
            "d", {**rock, "name_norm": "jazz"}, expected_rev=None, db_name=colecao, collection=nome
        )
        with pytest.raises(UniqueIndexViolation):
            await banco.save_doc_cas("d", {**rock, "rev": 1}, expected_rev=0, db_name=colecao, collection=nome)
        await banco._connect[nome][colecao].insert_many([{"_id": "e", "guild_id": "1"}, {"_id": "f", "guild_id": "1"}])

        await banco._connect[nome][repetida].insert_many(
            [{"_id": str(i), "guild_id": "1", "name_norm": "rock"} for i in range(3)]
            + [{"_id": "x", "guild_id": "1", "name_norm": "pop"}]
        )
        with pytest.raises(UniqueIndexViolation):
            await banco.ensure_unique_index(NAME_INDEX_KEYS, db_name=repetida, **indice)
        grupos = await banco.count_duplicate_groups(
            [key for key, _ in NAME_INDEX_KEYS], match=NAME_INDEX_PARTIAL, db_name=repetida, collection=nome
        )
        assert grupos == 1

        await banco.ensure_ttl_index("expire_at", db_name="radio_resolved", collection=nome)
        info = await banco._connect[nome]["radio_resolved"].index_information()
        assert any(spec.get("expireAfterSeconds") == 0 and spec["key"] == [("expire_at", 1)] for spec in info.values())

        # a limpeza única das entradas antigas sem ``expire_at``: só elas saem
        await banco._connect[nome]["radio_resolved"].insert_many(
            [
                {"_id": "velha1", "at": 1.0},
                {"_id": "velha2", "at": 2.0},
                {"_id": "nova", "at": 3.0, "expire_at": datetime.now(timezone.utc)},
            ]
        )
        apagadas = await banco.delete_matching(
            {"expire_at": {"$exists": False}}, db_name="radio_resolved", collection=nome
        )
        assert apagadas == 2
        assert [d["_id"] async for d in banco.iter_data("radio_resolved", nome)] == ["nova"]
    finally:
        await banco._connect.drop_database(nome)
        await banco._connect.close()


def test_mongo_station_name_index_and_resolved_ttl_against_a_real_mongod():
    asyncio.run(_ciclo_indice_de_estacoes())
