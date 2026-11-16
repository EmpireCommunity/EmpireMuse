"""Compare-and-set nos backends reais (BK-4, PR 257): ``delete_doc_cas`` e ``save_doc_cas`` do ``LocalDatabase`` (TinyMongo real em
``tmp_path``) e o cache do ``MongoDatabase`` (cliente falso; o mongod real fica em ``test_mongo_integracao.py``).

Documento gravado antes do compare-and-set não tem ``rev``: as regras o leem como 0, então ``expected_rev=0`` casa com ele.
"""

import asyncio
from types import SimpleNamespace

import pytest
from cachetools import TTLCache

from empiremuse.utils.db import LocalDatabase, MongoDatabase, rev_filter

pytestmark = pytest.mark.critical

BANCO, COLECAO = "global", "radios"


def _local_com(tmp_path, doc: dict) -> LocalDatabase:
    banco = LocalDatabase(str(tmp_path / "db"))
    banco._connect[BANCO][COLECAO].insert_one({**doc, "_id": "r"})
    return banco


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("guardado", "esperado", "apaga"),
    [
        ({"rev": 3}, 3, True),
        ({"rev": 3}, 2, False),
        ({}, 0, True),  # legado sem rev = rev 0
        ({}, 1, False),
    ],
    ids=["rev-bate", "rev-velho", "legado-rev-0", "legado-outro-rev"],
)
async def test_local_delete_doc_cas_so_apaga_com_o_rev_guardado(tmp_path, guardado, esperado, apaga):
    banco = _local_com(tmp_path, guardado)
    banco.cache[f"{BANCO}:{COLECAO}:r"] = {"velho": True}

    resultado = await banco.delete_doc_cas("r", expected_rev=esperado, db_name=COLECAO, collection=BANCO)

    assert resultado is apaga
    restante = banco._connect[BANCO][COLECAO].find_one({"_id": "r"})
    assert (restante is None) is apaga
    assert (f"{BANCO}:{COLECAO}:r" in banco.cache) is not apaga


@pytest.mark.asyncio
async def test_local_delete_doc_cas_sem_documento_devolve_false(tmp_path):
    banco = LocalDatabase(str(tmp_path / "db"))

    assert await banco.delete_doc_cas("r", expected_rev=0, db_name=COLECAO, collection=BANCO) is False


@pytest.mark.asyncio
async def test_local_save_doc_cas_troca_o_documento_legado_com_rev_0(tmp_path):
    banco = _local_com(tmp_path, {"enabled": True})

    assert await banco.save_doc_cas(
        "r", {"rev": 1, "enabled": False}, expected_rev=0, db_name=COLECAO, collection=BANCO
    )
    assert banco._connect[BANCO][COLECAO].find_one({"_id": "r"})["rev"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("guardado", "esperado", "grava"),
    [
        ({"rev": 3, "enabled": True}, 3, True),
        ({"rev": 3, "enabled": True}, 2, False),  # outro processo já avançou o rev
        ({"rev": 3, "enabled": True}, None, False),  # "ainda não existe", mas existe
        ({"enabled": True}, 0, True),  # sem o campo rev = rev 0
        ({"enabled": True}, 1, False),
    ],
    ids=["rev-bate", "rev-velho", "esperava-novo-mas-existe", "legado-rev-0", "legado-outro-rev"],
)
async def test_local_save_doc_cas_so_grava_com_o_rev_guardado(tmp_path, guardado, esperado, grava):
    banco = _local_com(tmp_path, guardado)

    resultado = await banco.save_doc_cas(
        "r", {"rev": 9, "enabled": False}, expected_rev=esperado, db_name=COLECAO, collection=BANCO
    )

    assert resultado is grava
    restante = banco._connect[BANCO][COLECAO].find_one({"_id": "r"})
    assert (restante.get("rev"), restante["enabled"]) == ((9, False) if grava else (guardado.get("rev"), True))


@pytest.mark.asyncio
@pytest.mark.parametrize(("esperado", "grava"), [(None, True), (0, False), (3, False)], ids=["novo", "rev-0", "rev-3"])
async def test_local_save_doc_cas_sem_documento_so_cria_quando_se_espera_novo(tmp_path, esperado, grava):
    banco = LocalDatabase(str(tmp_path / "db"))

    resultado = await banco.save_doc_cas(
        "r", {"rev": 1, "enabled": True}, expected_rev=esperado, db_name=COLECAO, collection=BANCO
    )

    assert resultado is grava
    assert (banco._connect[BANCO][COLECAO].find_one({"_id": "r"}) is not None) is grava


def test_filtro_do_mongo_casa_rev_ausente_so_no_rev_0():
    assert rev_filter(0) == {"$or": [{"rev": 0}, {"rev": {"$exists": False}}]}
    assert rev_filter(4) == {"rev": 4}


def _mongo_falso(tabela) -> MongoDatabase:
    banco = MongoDatabase.__new__(MongoDatabase)
    banco.cache = TTLCache(maxsize=10, ttl=300)
    banco._connect = {BANCO: {COLECAO: tabela}}
    return banco


class _TabelaQueRele:
    """``delete_one`` que, durante o ``await``, deixa uma leitura concorrente repor o documento no cache."""

    def __init__(self, deleted_count: int) -> None:
        self.banco: MongoDatabase | None = None
        self.deleted_count = deleted_count
        self.filtros: list[dict] = []

    async def delete_one(self, filtro: dict):
        self.filtros.append(filtro)
        await asyncio.sleep(0)  # só cede o laço (sem relógio): a leitura concorrente roda aqui
        assert self.banco is not None
        self.banco.cache[f"{BANCO}:{COLECAO}:r"] = {"rev": 3}
        return SimpleNamespace(deleted_count=self.deleted_count)


def _mongo_que_rele(deleted_count: int) -> tuple[MongoDatabase, _TabelaQueRele]:
    tabela = _TabelaQueRele(deleted_count)
    banco = _mongo_falso(tabela)
    tabela.banco = banco
    return banco, tabela


@pytest.mark.asyncio
async def test_mongo_delete_doc_cas_esvazia_o_cache_de_novo_depois_do_await():
    banco, tabela = _mongo_que_rele(deleted_count=1)

    assert await banco.delete_doc_cas("r", expected_rev=3, db_name=COLECAO, collection=BANCO) is True

    assert f"{BANCO}:{COLECAO}:r" not in banco.cache
    assert tabela.filtros == [{"_id": "r", "rev": 3}]


@pytest.mark.asyncio
async def test_mongo_delete_doc_cas_que_perde_mantem_o_cache_relido_e_usa_o_filtro_legado_no_rev_0():
    banco, tabela = _mongo_que_rele(deleted_count=0)

    assert await banco.delete_doc_cas("r", expected_rev=0, db_name=COLECAO, collection=BANCO) is False

    assert banco.cache[f"{BANCO}:{COLECAO}:r"] == {"rev": 3}  # o documento segue no banco: o cache relido vale
    assert tabela.filtros == [{"_id": "r", **rev_filter(0)}]


@pytest.mark.asyncio
async def test_mongo_delete_data_esvazia_o_cache_de_novo_depois_do_await():
    banco, _ = _mongo_que_rele(deleted_count=1)

    await banco.delete_data("r", COLECAO, BANCO)

    assert f"{BANCO}:{COLECAO}:r" not in banco.cache
