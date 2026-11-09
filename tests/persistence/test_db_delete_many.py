"""Apagar meus dados (E1), contrato §8 e §10: ``delete_many`` do ``LocalDatabase`` (TinyMongo real em ``tmp_path``) e do ``MongoDatabase``
(cliente falso: o CI não tem Mongo; só a chamada e o esvaziamento do cache são conferidos) e a fiação do ``BotPool`` (T-481).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cachetools import TTLCache

from empiremuse.utils.client import BotPool
from empiremuse.utils.db import LocalDatabase, MongoDatabase
from empiremuse.utils.erasure import ErasureService, ErasureTombstones
from empiremuse.utils.user_locks import UserLocks

pytestmark = pytest.mark.critical

COLECAO, BANCO = "radio_custom_favorites", "global"


@pytest.mark.asyncio
async def test_local_apaga_so_os_ids_pedidos_conta_os_que_existiam_e_esvazia_o_cache(tmp_path):
    banco = LocalDatabase(str(tmp_path / "db"))
    for id_ in ("1:45", "2:45", "1:145"):
        await banco.update_data(id_, {"rev": 1}, db_name=COLECAO, collection=BANCO)
    assert f"{BANCO}:{COLECAO}:1:45" in banco.cache  # update_data enche o cache

    apagados = await banco.delete_many(["1:45", "2:45", "9:45", "1:45"], db_name=COLECAO, collection=BANCO)

    assert apagados == 2  # o inexistente e o repetido não contam
    assert [d["_id"] async for d in banco.iter_data(db_name=COLECAO, collection=BANCO)] == ["1:145"]
    assert not [
        chave
        for chave in banco.cache
        if chave.startswith(f"{BANCO}:{COLECAO}:") and chave != f"{BANCO}:{COLECAO}:1:145"
    ]


@pytest.mark.asyncio
async def test_local_sem_ids_ou_colecao_inexistente_devolve_zero(tmp_path):
    banco = LocalDatabase(str(tmp_path / "db"))

    assert await banco.delete_many([], db_name=COLECAO, collection=BANCO) == 0
    assert await banco.delete_many(["x"], db_name="nao_existe", collection=BANCO) == 0


def _mongo_falso(tabela) -> MongoDatabase:
    banco = MongoDatabase.__new__(MongoDatabase)
    banco.cache = TTLCache(maxsize=10, ttl=300)
    banco._connect = {BANCO: {COLECAO: tabela}}
    return banco


@pytest.mark.asyncio
async def test_mongo_chama_delete_many_com_in_nos_ids_unicos_e_esvazia_as_chaves_do_cache():
    tabela = SimpleNamespace(delete_many=AsyncMock(return_value=SimpleNamespace(deleted_count=2)))
    banco = _mongo_falso(tabela)
    banco.cache.update({f"{BANCO}:{COLECAO}:1:45": {}, f"{BANCO}:{COLECAO}:2:45": {}, f"{BANCO}:{COLECAO}:1:145": {}})

    apagados = await banco.delete_many(["1:45", "2:45", "1:45"], db_name=COLECAO, collection=BANCO)

    assert apagados == 2
    tabela.delete_many.assert_awaited_once_with({"_id": {"$in": ["1:45", "2:45"]}})
    assert list(banco.cache) == [f"{BANCO}:{COLECAO}:1:145"]


@pytest.mark.asyncio
async def test_mongo_sem_ids_nao_chama_o_banco():
    tabela = SimpleNamespace(delete_many=AsyncMock())

    assert await _mongo_falso(tabela).delete_many([], db_name=COLECAO, collection=BANCO) == 0
    tabela.delete_many.assert_not_called()


@pytest.fixture
def pool_real(tmp_path, monkeypatch):
    for nome in ("home", "cwd"):
        (tmp_path / nome).mkdir()
    monkeypatch.setenv("EMPIREMUSE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path / "cwd")
    return BotPool()


def test_botpool_traz_lock_de_usuario_sepultura_e_servico_ligados_e_sem_loja_de_sessoes_ate_o_painel(pool_real):
    assert isinstance(pool_real.user_locks, UserLocks)
    assert isinstance(pool_real.erasure_tombstones, ErasureTombstones)
    assert isinstance(pool_real.erasure, ErasureService) and pool_real.erasure.pool is pool_real
    assert pool_real.erasure.tombstones is pool_real.erasure_tombstones
    assert pool_real.panel_sessions is None
    assert pool_real.erasure_tombstones._ttl == max(600, 2 * pool_real.config["DBCACHE_TTL"])
