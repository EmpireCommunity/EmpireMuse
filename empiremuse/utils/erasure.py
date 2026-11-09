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
    INTERNAL_KEY,
    MEMORY_KEY,
    RATE_WINDOW_S,
    SAVED_QUEUE_KEY,
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
    read_is_stale,
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
    gravar, pergunta ``stale(user_id, read_at)``: verdadeiro significa "houve apagamento depois da leitura" (``mark`` roda duas vezes por
    execução: o segundo é o FIM da exclusão) ou "a leitura é velha demais", e o resultado é descartado.
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

    def stale(self, user_id: int, read_at: Optional[float]) -> bool:
        """``read_is_stale``: houve apagamento depois da leitura OU a leitura tem ``ttl_s`` ou mais de idade (com ou sem sepultura).

        ``read_at=None`` (hora da leitura desconhecida) falha fechado: sempre velha.
        """
        now = self._clock()
        erased_at = self._erased_at.get(int(user_id))
        if erased_at is not None and now - erased_at >= self._ttl:
            del self._erased_at[int(user_id)]
            erased_at = None
        return read_is_stale(erased_at, float("-inf") if read_at is None else read_at, now, self._ttl)

    def prune(self) -> None:
        now = self._clock()
        for user_id in [uid for uid, at in self._erased_at.items() if now - at >= self._ttl]:
            del self._erased_at[user_id]


@dataclass(frozen=True)
class _Snapshot:
    """O que a passada 2 precisa: o sal e as impressões dos campos lidos no passo 1 (só em memória, nunca em log)."""

    salt: bytes
    prints: Mapping[str, str]
    ok: bool = True  # falso: o documento não foi lido e a passada 2 não tem com o que comparar


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

        Levanta ``ErasureRateLimited`` e ``ErasureBusy`` (nada é apagado). Falha de armazenamento e erro inesperado em qualquer passo nunca
        levantam: viram ``PARTIAL`` (``failed=("internal",)`` no segundo caso) e a auditoria e a contagem de limite ainda rodam.
        Agenda a passada 2 a ``ERASE_RECHECK_S``. ``source="owner"`` exige ``owner_id`` e ``grant_id`` não vazios (vão para a auditoria).

        PEGA o lock do usuário sozinho (``pool.user_locks``): QUEM CHAMA NÃO PODE SEGURÁ-LO (``asyncio.Lock`` não é reentrante; segurar trava
        para sempre). É lento (varre coleções): quem responde a uma interação dá ``defer`` antes de chamar.
        """
        user_id = int(user_id)
        if source not in SOURCES or (source == "owner" and not (owner_id and grant_id)):
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
        snapshot = _Snapshot(secrets.token_bytes(16), {}, ok=False)
        try:
            self.tombstones.mark(user_id)  # passo 1: antes de qualquer outro
            snapshot = await self._snapshot(user_id)
            memory = self._clear_memory(user_id)
            step3 = {store.key: await self._erase_store(store, user_id) for store in STORES}
            step3[SAVED_QUEUE_KEY] = await self._erase_queue(user_id)
            results = {store.key: await self._verify_store(store, user_id, step3[store.key]) for store in STORES}
            results[SAVED_QUEUE_KEY] = await self._verify_queue(user_id, step3[SAVED_QUEUE_KEY])
            if not snapshot.ok:  # sem as impressões a passada 2 não confere ``users``: não dá para garantir o resultado
                results[USERS.key] = StoreResult(results[USERS.key].count, ok=False)
            self.tombstones.mark(user_id)  # passo 5: ``erased_at`` passa a ser o FIM da exclusão
            self._clear_caches(user_id)
            late = self._close_connections(user_id)  # RPC e sessões do painel de novo (uma criada durante a execução)
            results[MEMORY_KEY] = StoreResult(memory.count + late.count, memory.ok and late.ok)
            return snapshot, build_report(results)
        except Exception as exc:  # noqa: BLE001 - só o tipo vai ao log; ``erase_user`` nunca levanta por erro inesperado
            logger.warning("Apagamento: erro inesperado (%s)", type(exc).__name__)
            self._mark_quietly(user_id)
            return snapshot, ErasureReport(ErasureStatus.PARTIAL, {}, (INTERNAL_KEY,))

    def _mark_quietly(self, user_id: int) -> None:
        try:
            self.tombstones.mark(user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Apagamento: sepultura não marcada (%s)", type(exc).__name__)

    async def _snapshot(self, user_id: int) -> _Snapshot:
        salt = secrets.token_bytes(16)
        try:
            doc = await self.pool.database.find_doc(user_id, db_name=USERS.collection, collection=USERS.database)
        except Exception as exc:  # noqa: BLE001 - só o tipo vai ao log
            logger.warning("Apagamento: documento do usuário não lido para a passada 2 (%s)", type(exc).__name__)
            return _Snapshot(salt, {}, ok=False)
        return _Snapshot(salt, fingerprint(user_id, doc, salt) if doc else {})

    def _clear_memory(self, user_id: int) -> StoreResult:
        """Passo 2: caches do processo, conexão RPC e sessões do painel. Nunca levanta."""
        popped = sum(
            int(cache.pop(user_id, None) is not None)
            for cache in (self.pool.lastfm_sessions, self.pool.user_prefix_cache)
        )
        closed = self._close_connections(user_id)
        return StoreResult(popped + closed.count, closed.ok)

    def _close_connections(self, user_id: int) -> StoreResult:
        """Fecha a conexão RPC e destrói as sessões do painel do membro. Nunca levanta; uma falha vira ``ok=False``."""
        count, ok = 0, True
        for close in (self._rpc_closer, self._destroy_panel_sessions):
            try:
                count += int(close(user_id))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Apagamento: memória não limpa por inteiro (%s)", type(exc).__name__)
                ok = False
        return StoreResult(count, ok)

    def _destroy_panel_sessions(self, user_id: int) -> int:
        store = getattr(self.pool, "panel_sessions", None)
        if store is None:
            return 0
        return int(store.destroy_user(str(user_id)))

    def _clear_caches(self, user_id: int) -> None:
        """Passo 5 (e passada 2): de novo os caches que quem leu antes pode ter enchido (o RPC e as sessões ficam em ``_close_connections``)."""
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
        by_updater = {"updated_by": str(user_id)} if store.action is StoreAction.ANONYMIZE else None
        async for doc in database.iter_data(db_name=store.collection, collection=store.database, filter=by_updater):
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
            failures = 0
            for doc_id in ids:  # tenta todos: uma falha não deixa os demais com o ID do membro
                try:
                    await self._anonymize(store, doc_id, user_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Apagamento: falha em %s ao anonimizar (%s)", store.key, type(exc).__name__)
                    failures += 1
            if failures:
                raise RuntimeError("falha ao anonimizar")

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

    # ------------------------------------------------------------------ fila salva (arquivos, §2 linha 23)

    @staticmethod
    async def _queue_call(name: str, user_id: int) -> Any:
        from empiremuse.utils.music import saved_queue  # importação tardia: o pacote de música importa o cliente do bot

        return await asyncio.get_running_loop().run_in_executor(None, getattr(saved_queue, name), user_id)

    async def _erase_queue(self, user_id: int) -> int:
        try:
            return int(await self._queue_call("delete_saved_queues", user_id))
        except Exception as exc:  # noqa: BLE001 - só o tipo vai ao log
            logger.warning("Apagamento: falha em %s no passo 3 (%s)", SAVED_QUEUE_KEY, type(exc).__name__)
            return 0

    async def _verify_queue(self, user_id: int, erased: int) -> StoreResult:
        """Passada 1: sobrou arquivo → apaga de novo, uma vez; sobrou de novo (ou erro de E/S) → falha do armazenamento."""
        count = erased
        try:
            if await self._queue_call("saved_queue_files", user_id):
                count += int(await self._queue_call("delete_saved_queues", user_id))
                if await self._queue_call("saved_queue_files", user_id):
                    return StoreResult(count, ok=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Apagamento: falha em %s na passada 1 (%s)", SAVED_QUEUE_KEY, type(exc).__name__)
            return StoreResult(count, ok=False)
        return StoreResult(count)

    # ------------------------------------------------------------------ auditoria

    @staticmethod
    def _audit(
        user_id: int, source: str, report: ErasureReport, owner_id: Optional[int], grant_id: Optional[str]
    ) -> None:
        """Passo 6: só origem, usuário, estado e contagens (nenhum valor apagado). O ID é registro de segurança (retenção do log).

        WARNING, como a trilha de auditoria do dono (sai mesmo com o log desligado), com os argumentos separados do texto fixo.
        """
        text, args = "erasure source=%s user=%s status=%s %s", [source, user_id, report.status.value, ""]
        args[3] = " ".join(f"{key}={value}" for key, value in report.counts.items())
        if report.failed:
            text, args = text + " failed=%s", [*args, ",".join(report.failed)]
        if source == "owner":
            text, args = text + " owner=%s grant=%s", [*args, owner_id, grant_id]
        # ``empiremuse_emit``: o agregador de logs junta WARNING de mesmo template; sem isto a 2ª execução em 60 s perderia ID, estado e contagens
        logger.warning(text, *args, extra={"empiremuse_emit": True})

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
                await self._guarded("rpc", self._close_again(user_id))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("ERASE-RECHECK falhou (%s)", type(exc).__name__)

    async def _close_again(self, user_id: int) -> None:
        """Passada 2: uma conexão RPC aberta depois do passo 5 também cai."""
        self._rpc_closer(user_id)

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
