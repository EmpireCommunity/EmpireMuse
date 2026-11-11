# -*- coding: utf-8 -*-
"""Prova de posse do ID no registro do relé RPC: o token enviado precisa ser o de ``global.users.token`` do mesmo ID."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Optional

from empiremuse.rpc_protocol import PROOF_MAX_IN_FLIGHT, PROOF_TIMEOUT, authenticates
from empiremuse.utils.db import DBModel

if TYPE_CHECKING:
    from empiremuse.utils.db import LocalDatabase, MongoDatabase

    type Database = LocalDatabase | MongoDatabase


GLOBAL_COLLECTION = "global"


class ProofUnavailable(Exception):
    """A prova não pôde ser feita (erro do banco, tempo esgotado, teto de provas em voo ou banco ausente).

    ``args`` leva só o NOME do tipo da causa: a mensagem dela pode carregar a URI do Mongo, com a senha.
    """


class TokenProof:
    """Verifica, sem cache, quais dos IDs de um registro têm o token enviado.

    A leitura é ``find_doc``: nunca cria o documento, nunca devolve do cache (um token rotacionado vale na hora) e
    devolve ``None`` para quem não existe. Quem chama passa a lista já deduplicada e decide o que fazer com cada ID
    (o verificador também não consulta banimento).
    """

    def __init__(
        self,
        get_database: Callable[[], Optional[Database]],
        *,
        timeout: float = PROOF_TIMEOUT,
        max_in_flight: int = PROOF_MAX_IN_FLIGHT,
    ):
        self._get_database = get_database
        self._timeout = timeout
        self._max_in_flight = max_in_flight
        self.in_flight = 0

    async def verify(self, ids: Sequence[int], token: str) -> frozenset[int]:
        """Os IDs cujo token guardado é o enviado; ``ProofUnavailable`` se a prova não pôde ser feita."""
        try:
            database = self._get_database()
        except Exception as exc:
            raise ProofUnavailable(type(exc).__name__) from None
        if database is None:
            raise ProofUnavailable("NoDatabase")
        if self.in_flight >= self._max_in_flight:
            raise ProofUnavailable("InFlightLimit")

        self.in_flight += 1  # um por registro, não por ID
        try:
            docs = await asyncio.wait_for(
                asyncio.gather(
                    *(database.find_doc(i, db_name=DBModel.users, collection=GLOBAL_COLLECTION) for i in ids),
                    return_exceptions=True,
                ),
                self._timeout,
            )
        # TimeoutError e o que o banco levantar; ``CancelledError`` não é ``Exception`` e passa.
        except Exception as exc:
            raise ProofUnavailable(type(exc).__name__) from None
        finally:
            self.in_flight -= 1

        failure = next((d for d in docs if isinstance(d, BaseException)), None)
        if failure is not None:
            raise ProofUnavailable(type(failure).__name__) from None

        return frozenset(
            i for i, doc in zip(ids, docs) if isinstance(doc, dict) and authenticates(token, doc.get("token"))
        )
