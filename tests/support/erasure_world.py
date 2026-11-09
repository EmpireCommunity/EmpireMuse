"""Mundo de teste do apagamento de dados: dois bancos TinyMongo reais em ``tmp_path``, relógio falso e um pool mínimo.

``database`` e ``local_database`` são bancos DIFERENTES (como com MongoDB): o que é da fila de scrobbles tem de ir só ao local.
A passada 2 espera ``gate``: o teste a libera quando quer olhar o estado entre a passada 1 e a passada 2.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from empiremuse.utils.db import LocalDatabase, global_db_models, scrobble_model
from empiremuse.utils.erasure import ErasureService, ErasureTombstones
from empiremuse.utils.user_locks import UserLocks

ALVO = 12345678901234567  # 17 dígitos
PREFIXO_DO_ALVO = int(f"{ALVO}8")  # contém o alvo como prefixo
SUFIXO_DO_ALVO = int(f"9{ALVO}")  # contém o alvo como sufixo
SERVIDOR, SERVIDOR_2 = 70000000000000001, 70000000000000002
BOT = 80000000000000001


class RelogioFalso:
    def __init__(self, inicio: float = 1000.0) -> None:
        self.agora = inicio

    def __call__(self) -> float:
        return self.agora

    def avancar(self, segundos: float) -> None:
        self.agora += segundos


class Mundo(SimpleNamespace):
    pool: Any
    service: ErasureService
    relogio: RelogioFalso
    gate: asyncio.Event
    rpc_fechados: list[int]
    database: LocalDatabase
    local: LocalDatabase


def montar_mundo(tmp_path, *, passada2_livre: bool = False) -> Mundo:
    relogio = RelogioFalso()
    database = LocalDatabase(str(tmp_path / "principal"))
    local = LocalDatabase(str(tmp_path / "local"))
    pool = SimpleNamespace(
        user_locks=UserLocks(),
        erasure_tombstones=ErasureTombstones(clock=relogio),
        lastfm_sessions={},
        user_prefix_cache={},
        database=database,
        local_database=local,
        panel_sessions=None,
    )
    gate = asyncio.Event()
    if passada2_livre:
        gate.set()
    rpc_fechados: list[int] = []

    async def dormir(_segundos: float) -> None:
        await gate.wait()

    def fechar_rpc(user_id: int) -> bool:
        rpc_fechados.append(user_id)
        return True

    service = ErasureService(pool, clock=relogio, sleep=dormir, rpc_closer=fechar_rpc)
    pool.erasure = service
    return Mundo(
        pool=pool,
        service=service,
        relogio=relogio,
        gate=gate,
        rpc_fechados=rpc_fechados,
        database=database,
        local=local,
    )


def documento_do_usuario(**campos: Any) -> dict:
    doc = deepcopy(global_db_models["users"])
    for caminho, valor in campos.items():
        if caminho.startswith("lastfm_"):
            doc["lastfm"][caminho.removeprefix("lastfm_")] = valor
        else:
            doc[caminho] = valor
    return doc


async def gravar_usuario(mundo: Mundo, user_id: int, **campos: Any) -> dict:
    doc = documento_do_usuario(**campos)
    await mundo.database.update_data(user_id, deepcopy(doc), db_name="users", collection="global")
    return doc


async def gravar_scrobbles(mundo: Mundo, user_id: int, *faixas: str) -> None:
    doc = deepcopy(scrobble_model["users"])
    doc["tracks"] = [{"query": faixa, "duration": 1000} for faixa in faixas]
    await mundo.local.update_data(user_id, doc, db_name="users", collection="scrobbles")


async def gravar(mundo: Mundo, banco: str, colecao: str, id_: Any, doc: dict) -> None:
    await mundo.database.update_data(id_, deepcopy(doc), db_name=colecao, collection=banco)


async def existe(mundo: Mundo, banco: str, colecao: str, id_: Any, *, local: bool = False) -> bool:
    alvo = mundo.local if local else mundo.database
    return await alvo.find_doc(id_, db_name=colecao, collection=banco) is not None


async def ids_da_colecao(mundo: Mundo, banco: str, colecao: str) -> set[str]:
    return {doc["_id"] async for doc in mundo.database.iter_data(db_name=colecao, collection=banco)}
