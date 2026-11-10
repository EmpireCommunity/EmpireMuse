# -*- coding: utf-8 -*-
"""Usuários banidos pelo dono (E5, contrato §7): lista persistida, cópia em memória e as consequências do ban.

A lista fica na coleção ``banned_users`` do banco global (um documento por usuário: data, motivo e dono) e uma cópia em memória responde
``is_blocked`` na hora, sem I/O, para todos os portões. Um só ``asyncio.Lock`` cobre carga, ban e unban (m-E1). Quem pode banir é decidido
por quem chama (``env_owner_ids``, nunca ``is_owner``); aqui só há a mecânica. A coleção NÃO entra em ``rules.erasure.STORES``: o
apagamento dos dados do membro não remove o registro de banimento.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Container
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from empiremuse.rules.usage import MS_PER_SECOND
from empiremuse.rules.user_ban import (
    BAN_ACTION,
    RPC_BLOCKED_REASON,
    UNBAN_ACTION,
    USER_BANS_BOOT_WAIT_S,
    USER_BANS_RETRY_S,
    UserBanRecord,
    WarningLimiter,
    is_blocked,
    new_user_ban_record,
    user_ban_from_doc,
    user_ban_to_doc,
)
from empiremuse.utils.db import DBModel

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool

logger = logging.getLogger(__name__)

BANS_DB = "global"
FAILED_PANEL = "panel_sessions"
FAILED_RPC = "rpc"


class UserBanError(Exception):
    """Base dos erros do banimento que o dono vê (nunca o texto de exceção do banco)."""


class AlreadyBanned(UserBanError):
    """O usuário já está banido: o registro original (``by``, ``banned_at``) não é sobrescrito (m-E3)."""


class UserBansUnavailable(UserBanError):
    """A lista não está carregada (banco fora do ar): ``ban`` e ``unban`` nunca valem só em memória (m-E2)."""


@dataclass(frozen=True)
class BanResult:
    """``OK`` = tudo feito; ``PARTIAL`` = gravado, mas uma consequência falhou (``failed``): o dono é avisado (m-E5)."""

    status: Literal["OK", "PARTIAL"]
    failed: tuple[str, ...] = ()


def _close_rpc(user_id: int, reason: str) -> bool:
    from empiremuse.web_app import close_rpc_for  # importação tardia: importar o bot não carrega o servidor web

    return close_rpc_for(user_id, reason)


class UserBans:
    def __init__(
        self,
        pool: BotPool,
        *,
        owners: Container[int] = frozenset(),
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        rpc_closer: Callable[[int, str], bool] = _close_rpc,
    ) -> None:
        self.pool = pool
        self._owners = owners  # ``OWNER_IDS`` do .env, lido uma vez (nunca ``is_owner``)
        self._clock = clock
        self._sleep = sleep
        self._rpc_closer = rpc_closer
        self._records: dict[int, UserBanRecord] = {}
        self._loaded = False
        self._warned_unloaded = False
        self._lock = asyncio.Lock()
        self._retry_task: asyncio.Task | None = None
        self._warnings = WarningLimiter()  # Q13: um só para o pool inteiro (N bots não dão N avisos)

    @property
    def loaded(self) -> bool:
        """A lista veio do banco? Falso enquanto o banco falhou: ``records()`` vazio então não prova que não há banidos."""
        return self._loaded

    def is_blocked(self, user_id: object, *, exempt: bool = False) -> bool:
        """Regra única do portão (``rules.user_ban.is_blocked``); sem I/O e nunca recarrega sozinha."""
        return is_blocked(user_id, self._records, self._owners, exempt=exempt)

    def allow_warning(self, user_id: int) -> bool:
        """Pode avisar este banido em mensagem de canal agora? No máximo 1 por usuário por hora, em todos os bots."""
        return self._warnings.allow(user_id)

    def records(self) -> list[UserBanRecord]:
        """Os banimentos, do mais recente para o mais antigo."""
        return sorted(self._records.values(), key=lambda record: (record.banned_at, record.user_id), reverse=True)

    # ---------------------------------------------------------------- carga

    async def ensure_loaded(self) -> bool:
        """Carrega a lista do banco uma vez; falha do banco devolve ``False`` (tenta de novo na próxima chamada)."""
        async with self._lock:
            return await self._load_locked()

    async def _load_locked(self) -> bool:
        if self._loaded:
            return True
        try:
            documents = await self.pool.database.query_data(db_name=DBModel.banned_users, collection=BANS_DB)
        except Exception as exc:  # noqa: BLE001 - sem a lista o bot segue (D2); os banimentos valem quando o banco voltar
            if not self._warned_unloaded:
                self._warned_unloaded = True
                logger.warning("Lista de usuários banidos não carregada (%s)", type(exc).__name__)
            return False
        loaded: dict[int, UserBanRecord] = {}
        malformed = 0
        for document in documents:
            record = user_ban_from_doc(document)
            if record is None:
                malformed += 1
            else:
                loaded[record.user_id] = record
        if malformed:  # m-E4: documento malformado não desbane em silêncio; só a contagem vai ao log
            logger.warning("Lista de usuários banidos: %d documento(s) malformado(s) ignorado(s)", malformed)
        self._records = loaded
        self._loaded = True
        self._warned_unloaded = False
        return True

    async def boot(self, *, wait_s: float = USER_BANS_BOOT_WAIT_S) -> bool:
        """Antes de conectar ao gateway (D2): tenta carregar por ``wait_s``; sem banco, abre sem lista e tenta de novo em segundo plano."""
        try:
            if await asyncio.wait_for(self.ensure_loaded(), wait_s):
                return True
        except TimeoutError:
            if not self._warned_unloaded:
                self._warned_unloaded = True
                logger.warning("Lista de usuários banidos não carregada (tempo esgotado)")
        except Exception as exc:  # noqa: BLE001 - D2: qualquer falha da carga (não só timeout) abre o bot sem lista; só o tipo vai ao log
            if not self._warned_unloaded:
                self._warned_unloaded = True
                logger.warning("Lista de usuários banidos não carregada (%s)", type(exc).__name__)
        self.start_retry()
        return False

    def start_retry(self) -> None:
        if self._retry_task is None or self._retry_task.done():
            self._retry_task = asyncio.ensure_future(self._retry_until_loaded())

    async def _retry_until_loaded(self) -> None:
        while not self._loaded:
            await self._sleep(USER_BANS_RETRY_S)
            try:
                await self.ensure_loaded()
            except Exception:  # noqa: BLE001 - a nova tentativa nunca morre por um erro inesperado
                logger.warning("Nova tentativa de carga da lista de banidos falhou", exc_info=True)

    def stop(self) -> None:
        if self._retry_task is not None:
            self._retry_task.cancel()

    # ------------------------------------------------------------- ban/unban

    async def ban(self, owner_id: int, target_id: int, reason: object) -> BanResult:
        """Ordem do m-E5: carga, recusa de reban, banco (o erro sobe), memória; depois, fora do lock, sessão do painel e RPC; por fim a auditoria."""
        async with self._lock:
            if not await self._load_locked():
                raise UserBansUnavailable
            if target_id in self._records:
                raise AlreadyBanned
            record = new_user_ban_record(target_id, reason, owner_id, int(self._clock() * MS_PER_SECOND))
            await self.pool.database.update_data(
                target_id, user_ban_to_doc(record), db_name=DBModel.banned_users, collection=BANS_DB
            )
            self._records = {**self._records, target_id: record}
        failed = self._cut_access(target_id)
        self._audit(BAN_ACTION, owner_id, target_id)
        return BanResult("PARTIAL" if failed else "OK", failed)

    async def unban(self, owner_id: int, target_id: int) -> bool:
        """Apaga o documento e depois a memória. Lista não carregada = erro (nunca ``False``); ``False`` só quando o usuário não está banido."""
        async with self._lock:
            if not await self._load_locked():
                raise UserBansUnavailable
            if target_id not in self._records:
                return False
            await self.pool.database.delete_data(target_id, DBModel.banned_users, BANS_DB)
            self._records = {uid: rec for uid, rec in self._records.items() if uid != target_id}
        self._audit(UNBAN_ACTION, owner_id, target_id)
        return True

    def _cut_access(self, user_id: int) -> tuple[str, ...]:
        """Sessões do painel e conexão RPC do banido; cada falha vira ``failed`` (parcial), nunca impede a outra."""
        failed: list[str] = []
        try:
            sessions = getattr(self.pool, "panel_sessions", None)
            if sessions is not None:
                sessions.destroy_user(str(user_id))
        except Exception as exc:  # noqa: BLE001 - o ban já foi gravado; o dono recebe o parcial
            logger.warning("Banimento: sessões do painel não destruídas (%s)", type(exc).__name__)
            failed.append(FAILED_PANEL)
        try:
            self._rpc_closer(user_id, RPC_BLOCKED_REASON)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Banimento: conexão RPC não fechada (%s)", type(exc).__name__)
            failed.append(FAILED_RPC)
        return tuple(failed)

    @staticmethod
    def _audit(action: str, owner_id: int, target_id: int) -> None:
        """D4: dono, alvo e ação; NUNCA o motivo (pode ter dado pessoal). ``empiremuse_emit``: o agregador não junta esta trilha."""
        logger.warning(
            "banimento de usuário: ação=%s dono=%s alvo=%s",
            action,
            owner_id,
            target_id,
            extra={"empiremuse_emit": True},
        )


def user_bans_of(owner: object) -> UserBans | None:
    """A lista de usuários banidos do pool de um bot ou do próprio pool; ``None`` se não houver (fakes de teste)."""
    pool = getattr(owner, "pool", owner)
    bans = getattr(pool, "user_bans", None)
    return bans if isinstance(bans, UserBans) else None
