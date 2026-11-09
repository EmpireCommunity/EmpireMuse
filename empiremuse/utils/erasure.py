"""Apagamento dos dados de um membro: serviço, sepultura por usuário e segunda passada.

O serviço é único e recebe o ``user_id`` já resolvido pela porta (slash, painel ou, depois da confirmação do titular, o comando do dono);
a regra "só o próprio membro" vale na porta. Apagar o que não existe é sucesso, uma falha de armazenamento vira ``PARTIAL`` e nenhum texto
de exceção, valor apagado ou arquivo de log é tocado aqui. As regras puras ficam em ``empiremuse/rules/erasure.py``.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Optional

from empiremuse.rules.erasure import (
    ANONYMIZED,
    ERASE_CONCURRENCY,
    ERASE_RECHECK_S,
    ERASE_TOMBSTONE_S,
    MEMORY_KEY,
    RATE_WINDOW_S,
    STORES,
    ErasureReport,
    ErasureStatus,
    StoreAction,
    StoreRef,
    StoreResult,
    allow_erasure,
    build_report,
    custom_favorite_key_matches,
    fingerprint,
    only_defaults,
    reset_to_default,
    resurrected_paths,
)

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool

logger = logging.getLogger(__name__)

Source = Literal["slash", "panel", "owner"]
SOURCES = frozenset({"slash", "panel", "owner"})
CAS_ATTEMPTS = 3
USERS = next(store for store in STORES if store.key == "users")
SCROBBLES = next(store for store in STORES if store.key == "scrobbles")


class ErasureBusy(Exception):
    """Já há ``ERASE_CONCURRENCY`` apagamentos em andamento no processo; nada foi apagado."""


class ErasureRateLimited(Exception):
    """Passou de um limite por hora; nada foi apagado."""

    def __init__(self, retry_after_s: float) -> None:
        super().__init__("erasure rate limited")
        self.retry_after_s = retry_after_s


class ErasureTombstones:
    """Sepultura por usuário, só em memória: guarda quando o apagamento aconteceu, por ``ttl_s``.

    Quem lê ``global.users`` ou ``scrobbles.users`` para depois gravar (ou encher cache) captura ``read_at = now()`` ANTES de ler e, antes de
    gravar, pergunta ``stale(user_id, read_at)``: verdadeiro significa "houve apagamento depois da leitura", e o resultado é descartado.
    """

    def __init__(self, ttl_s: float = ERASE_TOMBSTONE_S, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_s
        self._clock = clock
        self._erased_at: dict[int, float] = {}

    def now(self) -> float:
        return self._clock()

    def mark(self, user_id: int) -> None:
        self.prune()
        self._erased_at[int(user_id)] = self._clock()

    def stale(self, user_id: int, read_at: float) -> bool:
        erased_at = self._erased_at.get(int(user_id))
        if erased_at is None:
            return False
        if self._clock() - erased_at >= self._ttl:
            del self._erased_at[int(user_id)]
            return False
        return erased_at >= read_at

    def prune(self) -> None:
        now = self._clock()
        for user_id in [uid for uid, at in self._erased_at.items() if now - at >= self._ttl]:
            del self._erased_at[user_id]


@dataclass(frozen=True)
class _Snapshot:
    """O que a passada 2 precisa: o sal e as impressões dos campos lidos no passo 1 (só em memória, nunca em log)."""

    salt: bytes
    prints: Mapping[str, str]


def _close_rpc(user_id: int) -> bool:
    from empiremuse.web_app import close_rpc_for  # importação tardia: importar o bot não carrega o servidor web

    return close_rpc_for(user_id)


class ErasureService:
    def __init__(
        self,
        pool: BotPool,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        rpc_closer: Callable[[int], bool] = _close_rpc,
    ) -> None:
        self.pool = pool
        self._clock = clock
        self._sleep = sleep
        self._rpc_closer = rpc_closer
        self._running = 0
        self._ok: dict[int, list[float]] = {}
        self._tries: dict[int, list[float]] = {}
        self._global_ok: list[float] = []
        self._tasks: set[asyncio.Task] = set()

    @property
    def tombstones(self) -> ErasureTombstones:
        return self.pool.erasure_tombstones

    # ------------------------------------------------------------------ entrada

    async def erase_user(
        self, user_id: int, *, source: Source, owner_id: Optional[int] = None, grant_id: Optional[str] = None
    ) -> ErasureReport:
        """Apaga tudo o que é do membro (§2 do contrato) e devolve o relatório.

        Levanta ``ErasureRateLimited`` e ``ErasureBusy`` (nada é apagado). Falha de armazenamento nunca levanta: vira ``PARTIAL``.
        Agenda a passada 2 a ``ERASE_RECHECK_S``. ``source="owner"`` exige ``owner_id`` e ``grant_id`` (vão para a auditoria).
        """
        user_id = int(user_id)
        if source not in SOURCES or (source == "owner" and (owner_id is None or grant_id is None)):
            raise ValueError("source/owner_id/grant_id inválidos")
        async with self.pool.user_locks.lock(user_id):
            self._check_limits(user_id)
            if self._running >= ERASE_CONCURRENCY:
                raise ErasureBusy
            self._running += 1
            try:
                snapshot, report = await self._run(user_id)
            finally:
                self._running -= 1
            self._record(user_id, report.status)
            self._audit(user_id, source, report, owner_id, grant_id)
            self._schedule_recheck(user_id, snapshot)
        return report

    # ------------------------------------------------------------------ limites

    def _check_limits(self, user_id: int) -> None:
        now = self._clock()
        decision = allow_erasure(self._ok.get(user_id, ()), self._tries.get(user_id, ()), self._global_ok, now)
        if not decision.allowed:
            raise ErasureRateLimited(decision.retry_after_s)

    def _record(self, user_id: int, status: ErasureStatus) -> None:
        now = self._clock()
        self._tries.setdefault(user_id, []).append(now)
        if status is ErasureStatus.OK:
            self._ok.setdefault(user_id, []).append(now)
            self._global_ok.append(now)
        self._prune_limits(now)

    def _prune_limits(self, now: float) -> None:
        for book in (self._ok, self._tries):
            for user_id in list(book):
                book[user_id] = [stamp for stamp in book[user_id] if now - stamp < RATE_WINDOW_S]
                if not book[user_id]:
                    del book[user_id]
        self._global_ok = [stamp for stamp in self._global_ok if now - stamp < RATE_WINDOW_S]

    # ------------------------------------------------------------------ passos 1 a 6

    async def _run(self, user_id: int) -> tuple[_Snapshot, ErasureReport]:
        self.tombstones.mark(user_id)  # antes de qualquer outro passo
        snapshot = await self._snapshot(user_id)
        memory = self._clear_memory(user_id)
        step3 = {store.key: await self._erase_store(store, user_id) for store in STORES}
        results = {store.key: await self._verify_store(store, user_id, step3[store.key]) for store in STORES}
        self._clear_caches(user_id)
        results[MEMORY_KEY] = memory
        self._destroy_panel_sessions(user_id)  # de novo: pega uma sessão criada durante a execução
        return snapshot, build_report(results)

    async def _snapshot(self, user_id: int) -> _Snapshot:
        salt = secrets.token_bytes(16)
        try:
            doc = await self.pool.database.find_doc(user_id, db_name=USERS.collection, collection=USERS.database)
        except Exception as exc:  # noqa: BLE001 - só o tipo vai ao log
            logger.warning("Apagamento: documento do usuário não lido para a passada 2 (%s)", type(exc).__name__)
            doc = None
        return _Snapshot(salt, fingerprint(user_id, doc, salt) if doc else {})

    def _clear_memory(self, user_id: int) -> StoreResult:
        """Passo 2: caches do processo, conexão RPC e sessões do painel. Nunca levanta."""
        count, ok = 0, True
        for cache in (self.pool.lastfm_sessions, self.pool.user_prefix_cache):
            count += int(cache.pop(user_id, None) is not None)
        try:
            count += int(bool(self._rpc_closer(user_id)))
            count += self._destroy_panel_sessions(user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Apagamento: memória não limpa por inteiro (%s)", type(exc).__name__)
            ok = False
        return StoreResult(count, ok)

    def _destroy_panel_sessions(self, user_id: int) -> int:
        store = getattr(self.pool, "panel_sessions", None)
        if store is None:
            return 0
        try:
            return int(store.destroy_user(str(user_id)))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Apagamento: sessões do painel não destruídas (%s)", type(exc).__name__)
            return 0

    def _clear_caches(self, user_id: int) -> None:
        """Passo 5 (e passada 2): de novo os caches que quem leu antes pode ter enchido."""
        self.pool.lastfm_sessions.pop(user_id, None)
        self.pool.user_prefix_cache.pop(user_id, None)
        for store in STORES:
            if store.action is StoreAction.DELETE:
                self._database(store).cache.pop(f"{store.database}:{store.collection}:{user_id}", None)

    def _database(self, store: StoreRef) -> Any:
        return self.pool.local_database if store.local else self.pool.database

    # ------------------------------------------------------------------ um armazenamento

    async def _pending_ids(self, store: StoreRef, user_id: int) -> list[str]:
        """Os ids dos documentos que ainda guardam algo do membro (lê do banco: sem cache e sem criar o padrão)."""
        database = self._database(store)
        if store.action is StoreAction.DELETE:
            doc = await database.find_doc(user_id, db_name=store.collection, collection=store.database)
            return [str(user_id)] if doc else []
        found: list[str] = []
        async for doc in database.iter_data(db_name=store.collection, collection=store.database):
            key = str(doc.get("_id", ""))
            if store.action is StoreAction.DELETE_MATCHING and custom_favorite_key_matches(key, user_id):
                found.append(key)
            elif store.action is StoreAction.ANONYMIZE and doc.get("updated_by") == str(user_id):
                found.append(key)
        return found

    async def _apply(self, store: StoreRef, user_id: int, ids: list[str]) -> None:
        database = self._database(store)
        if store.action is StoreAction.DELETE:
            await database.delete_data(user_id, db_name=store.collection, collection=store.database)
        elif store.action is StoreAction.DELETE_MATCHING:
            await database.delete_many(ids, db_name=store.collection, collection=store.database)
        else:
            for doc_id in ids:
                await self._anonymize(store, doc_id, user_id)

    async def _anonymize(self, store: StoreRef, doc_id: str, user_id: int) -> None:
        """Troca o ``updated_by`` por ``"deleted"`` com compare-and-set em ``rev`` (relê se alguém gravou no meio)."""
        database = self._database(store)
        for _ in range(CAS_ATTEMPTS):
            doc = await database.find_doc(doc_id, db_name=store.collection, collection=store.database)
            if doc is None or doc.get("updated_by") != str(user_id):
                return
            rev = doc.get("rev")
            if not isinstance(rev, int) or isinstance(rev, bool):
                raise ValueError("documento sem rev")
            saved = await database.save_doc_cas(
                doc_id,
                {**doc, "updated_by": ANONYMIZED, "rev": rev + 1},
                expected_rev=rev,
                db_name=store.collection,
                collection=store.database,
            )
            if saved:
                return
        raise RuntimeError("compare-and-set não passou")

    async def _erase_store(self, store: StoreRef, user_id: int) -> int:
        """Passo 3. Devolve quantos documentos tinham dado do membro; uma falha vira 0 e a passada 1 decide."""
        try:
            ids = await self._pending_ids(store, user_id)
            await self._apply(store, user_id, ids)
            return len(ids)
        except Exception as exc:  # noqa: BLE001 - só o tipo vai ao log
            logger.warning("Apagamento: falha em %s no passo 3 (%s)", store.key, type(exc).__name__)
            return 0

    async def _verify_store(self, store: StoreRef, user_id: int, erased: int) -> StoreResult:
        """Passo 4 (passada 1): sobrou documento → apaga de novo, uma vez; sobrou de novo → falha do armazenamento."""
        count = erased
        try:
            left = await self._pending_ids(store, user_id)
            if left:
                await self._apply(store, user_id, left)
                count += len(left)
                if await self._pending_ids(store, user_id):
                    return StoreResult(count, ok=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Apagamento: falha em %s na passada 1 (%s)", store.key, type(exc).__name__)
            return StoreResult(count, ok=False)
        return StoreResult(count)

    # ------------------------------------------------------------------ auditoria

    @staticmethod
    def _audit(
        user_id: int, source: str, report: ErasureReport, owner_id: Optional[int], grant_id: Optional[str]
    ) -> None:
        """Passo 6: só origem, usuário, estado e contagens (nenhum valor apagado). O ID é registro de segurança (retenção do log)."""
        counts = " ".join(f"{key}={value}" for key, value in report.counts.items())
        line = f"erasure source={source} user={user_id} status={report.status} {counts}"
        if report.failed:
            line += f" failed={','.join(report.failed)}"
        if source == "owner":
            line += f" owner={owner_id} grant={grant_id}"
        logger.info(line)

    # ------------------------------------------------------------------ passada 2

    def _schedule_recheck(self, user_id: int, snapshot: _Snapshot) -> None:
        task = asyncio.get_running_loop().create_task(self._recheck(user_id, snapshot))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_rechecks(self) -> None:
        """Espera as passadas 2 agendadas (testes e encerramento ordenado)."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _recheck(self, user_id: int, snapshot: _Snapshot) -> None:
        try:
            await self._sleep(ERASE_RECHECK_S)
            async with self.pool.user_locks.lock(user_id):
                await self._guarded("scrobbles", self._recheck_scrobbles(user_id))
                await self._guarded("users", self._recheck_users(user_id, snapshot))
                self._clear_caches(user_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("ERASE-RECHECK falhou (%s)", type(exc).__name__)

    @staticmethod
    async def _guarded(label: str, work: Awaitable[Any]) -> None:
        try:
            await work
        except Exception as exc:  # noqa: BLE001 - não muda o relatório já entregue
            logger.warning("ERASE-RECHECK %s falhou (%s)", label, type(exc).__name__)

    async def _recheck_scrobbles(self, user_id: int) -> None:
        """Só escritor de fundo grava em ``scrobbles.users`` e a sepultura o bloqueia: conteúdo ali é ressurreição."""
        local = self.pool.local_database
        doc = await local.find_doc(user_id, db_name=SCROBBLES.collection, collection=SCROBBLES.database)
        if doc and doc.get("tracks"):
            await local.delete_data(user_id, db_name=SCROBBLES.collection, collection=SCROBBLES.database)

    async def _recheck_users(self, user_id: int, snapshot: _Snapshot) -> None:
        """Campo com valor idêntico ao apagado volta ao padrão; valor diferente é uso novo e fica; sobrou só o padrão → apaga o documento."""
        if not snapshot.prints:
            return
        database = self.pool.database
        doc = await database.find_doc(user_id, db_name=USERS.collection, collection=USERS.database)
        if not doc:
            return
        paths = resurrected_paths(user_id, doc, snapshot.prints, snapshot.salt)
        if not paths:
            return
        cleaned = reset_to_default(doc, paths)
        if only_defaults(cleaned):
            await database.delete_data(user_id, db_name=USERS.collection, collection=USERS.database)
        else:
            await database.update_data(user_id, cleaned, db_name=USERS.collection, collection=USERS.database)
