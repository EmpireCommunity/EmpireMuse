"""F3-6b, etapa 5 (commit próprio de ``utils/db.py``) — ``query_data`` passa a respeitar o ``limit`` e nasce ``iter_data`` (cursor).

Hoje ``MongoDatabase.query_data`` faz ``[d async for d in colecao.find(filtro)]`` e IGNORA o ``limit`` (a coleção inteira vai para a
memória) e ``LocalDatabase.query_data`` devolve o cursor do TinyMongo, também sem ``limit``. A migração das sessões (R3, decisão
do MAESTRO) precisa de um CURSOR que entregue os documentos aos poucos: ``iter_data(db_name, collection, filter=None)``, gerador
assíncrono nos dois bancos (``async for documento in banco.iter_data(...)``).

* caracterização dos chamadores atuais (passa hoje e tem de continuar passando): sem ``limit`` EXPLÍCITO a consulta devolve tudo
  (nenhum teto escondido de 100 ou 500), o ``filter`` filtra, e ``iter_data`` percorre os documentos sem limite;
* RED: com ``limit=n`` devolve no máximo ``n`` e puxa do cursor no máximo ``n`` (+1) documentos; ``iter_data`` entrega preguiçosamente.
"""

import pytest

from empiremuse.utils.db import LocalDatabase, MongoDatabase


class CursorFalso:
    """O cursor assíncrono do PyMongo Async (``AsyncCursor``): ``limit()`` encadeável e contagem do que foi realmente puxado."""

    def __init__(self, documentos):
        self.documentos, self.teto, self.puxados = list(documentos), None, 0

    def limit(self, n):
        self.teto = n
        return self

    def __aiter__(self):
        return self._gerar()

    async def _gerar(self):
        for indice, documento in enumerate(self.documentos):
            if self.teto is not None and indice >= self.teto:
                return
            self.puxados += 1
            yield dict(documento)


class ColecaoFalsa:
    def __init__(self, documentos):
        self.documentos, self.ultimo_cursor, self.filtros = documentos, None, []

    def find(self, filtro=None):
        self.filtros.append(filtro)
        selecionados = [d for d in self.documentos if all(d.get(k) == v for k, v in (filtro or {}).items())]
        self.ultimo_cursor = CursorFalso(selecionados)
        return self.ultimo_cursor


def mongo_com(documentos) -> tuple[MongoDatabase, ColecaoFalsa]:
    colecao = ColecaoFalsa(documentos)
    banco = MongoDatabase.__new__(MongoDatabase)
    banco._connect = {"global": {"guilds": colecao}}
    return banco, colecao


def docs(quantos: int) -> list[dict]:
    return [{"_id": str(n), "par": n % 2 == 0} for n in range(quantos)]


# ============================================================================ caracterização: o que os chamadores de hoje esperam


# ============================================================================ RED: o limit passa a valer


class TestLimit:
    @pytest.mark.asyncio
    async def test_mongo_com_limit_devolve_no_maximo_o_limit_e_nao_puxa_o_resto(self):
        banco, colecao = mongo_com(docs(150))
        achados = list(await banco.query_data(db_name="guilds", collection="global", limit=7))
        assert len(achados) == 7 and colecao.ultimo_cursor.puxados <= 8

    @pytest.mark.asyncio
    async def test_local_com_limit_devolve_no_maximo_o_limit(self, tmp_path):
        banco = LocalDatabase(dir_=str(tmp_path))
        for n in range(30):
            await banco.update_data(id_=n, data={"n": n}, db_name="guilds", collection="global")
        assert len(list(await banco.query_data(db_name="guilds", collection="global", limit=4))) == 4


# ============================================================================ RED: iter_data, o cursor preguiçoso


class TestIterData:
    @pytest.mark.asyncio
    async def test_mongo_percorre_tudo_e_aplica_o_filtro(self):
        banco, _ = mongo_com(docs(10))
        todos = [d async for d in banco.iter_data(db_name="guilds", collection="global")]
        pares = [d async for d in banco.iter_data(db_name="guilds", collection="global", filter={"par": True})]
        assert len(todos) == 10 and len(pares) == 5

    @pytest.mark.asyncio
    async def test_local_tambem_tem_iter_data(self, tmp_path):
        banco = LocalDatabase(dir_=str(tmp_path))
        for n in range(12):
            await banco.update_data(id_=n, data={"n": n}, db_name="guilds", collection="global")
        assert len([d async for d in banco.iter_data(db_name="guilds", collection="global")]) == 12
