"""Autorização de MEMBRO por servidor (T-660, contrato r10 §2 c; threat model T1-T11, CC2 e CC3).

"Membro" = quem está no servidor sem precisar gerenciá-lo. Nunca autoriza nada de gerente: o ``GuildAuthorizer`` (``authz.py``) segue
exigindo ``session.guilds`` e tem cache próprio; este módulo não o lê nem o alimenta.

Ordem (todo fracasso definitivo = o mesmo ``MemberAccessDenied``, que a API devolve como o mesmo ``404``):
1. id no formato; 2. id em ``session.guilds`` ou em ``session.member_guild_ids`` (senão nega **sem tocar no Discord**); 3. bot pronto do
pool no servidor; 4. membro agora: a ESCRITA sempre pergunta ao Discord (``fetch_member``, o cache do gateway não vale: sem o intent de
membros ele não vê saídas); a leitura usa o cache do gateway e depois o ``fetch_member``; 5. ``pending`` (triagem de membro) nega;
6. nenhuma permissão exigida.

CC2: as consultas ao Discord gastam um orçamento GLOBAL por bot (balde e teto de concorrência, entre todos os usuários). Orçamento esgotado,
tempo esgotado, 5xx e 429 do Discord = ``MemberLookupUnavailable`` (a API devolve ``503``) e **nunca** entram no cache; só ``NotFound`` e
``Forbidden`` (a pessoa não está ou o bot não pode ver) viram a negação guardada.
CC3: o cache (só de leitura, 30 s, positivo e negativo, teto com LRU) tem uma GERAÇÃO por usuário que ``purge_user`` (ouvinte do
apagamento e do banimento) incrementa: uma consulta que estava em andamento durante o apagamento não grava mais nada.
O log leva só o TIPO da exceção (a mensagem do aiohttp pode trazer a rota com o id do usuário); nenhuma linha junta usuário e servidor.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional

import aiohttp
import disnake

from empiremuse.panel.authz import guild_icon_url, ready_bots_with_guild
from empiremuse.panel.sessions import Session

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool

logger = logging.getLogger(__name__)

CACHE_SECONDS = 30.0
MAX_CACHE_ENTRIES = 10_000
FETCH_TIMEOUT_SECONDS = 5.0
# Orçamento por bot para as consultas de membro ao Discord (entre todos os usuários): rajada de 10, 2 por segundo, 4 ao mesmo tempo.
BUDGET_BURST = 10
BUDGET_PER_SECOND = 2.0
BUDGET_CONCURRENCY = 4
_GUILD_ID = re.compile(r"[0-9]{1,20}", re.ASCII)


class MemberAccessDenied(Exception):
    """Não é membro, servidor inexistente, sem bot, id fora das listas ou malformado: a resposta é sempre o mesmo ``404``."""


class MemberLookupUnavailable(Exception):
    """O Discord não respondeu de forma definitiva (orçamento esgotado, tempo, 5xx, 429): ``503``, nunca guardado no cache."""


@dataclass(frozen=True)
class MemberAccess:
    guild_id: str
    name: str
    icon_url: Optional[str]


class MemberBudget:
    """Balde e teto de concorrência POR BOT para as consultas de membro (CC2). ``acquire`` devolve ``False`` quando não há como consultar
    agora; quem consegue chama ``release`` ao terminar. Relógio injetável; sem espera."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        *,
        burst: int = BUDGET_BURST,
        per_second: float = BUDGET_PER_SECOND,
        concurrency: int = BUDGET_CONCURRENCY,
    ) -> None:
        self._clock, self._burst, self._rate, self._concurrency = clock, burst, per_second, concurrency
        self._tokens: dict[str, tuple[float, float]] = {}  # bot -> (fichas, quando foram contadas)
        self._running: dict[str, int] = {}

    def acquire(self, bot_id: str) -> bool:
        now = self._clock()
        tokens, at = self._tokens.get(bot_id, (float(self._burst), now))
        tokens = min(float(self._burst), tokens + (now - at) * self._rate)
        if tokens < 1 or self._running.get(bot_id, 0) >= self._concurrency:
            self._tokens[bot_id] = (tokens, now)
            return False
        self._tokens[bot_id] = (tokens - 1, now)
        self._running[bot_id] = self._running.get(bot_id, 0) + 1
        return True

    def release(self, bot_id: str) -> None:
        self._running[bot_id] = max(0, self._running.get(bot_id, 0) - 1)


def session_lists(session: Session, guild_id: str) -> bool:
    """O id está numa das listas da sessão (gerenciáveis ou de membro): o pré-filtro que vem antes de qualquer consulta ao Discord."""
    return guild_id in session.member_guild_ids or any(guild.id == guild_id for guild in session.guilds)


class MemberAuthorizer:
    """Decide se o usuário da sessão é membro agora do servidor. Cache só de leitura, por (usuário, servidor), com geração por usuário."""

    def __init__(
        self,
        pool: BotPool,
        *,
        clock: Callable[[], float] = time.monotonic,
        cache_seconds: float = CACHE_SECONDS,
        budget: Optional[MemberBudget] = None,
    ) -> None:
        self._pool = pool
        self._clock = clock
        self._cache_seconds = cache_seconds
        self._budget = budget if budget is not None else MemberBudget(clock)
        self._cache: OrderedDict[tuple[str, str], tuple[float, Optional[MemberAccess]]] = OrderedDict()
        self._generation: dict[str, int] = {}

    async def authorize(self, session: Session, guild_id: str, *, fresh: bool) -> MemberAccess:
        """``fresh=True`` (escrita) nunca lê nem grava o cache e sempre pergunta ao Discord. Levanta ``MemberAccessDenied`` ou
        ``MemberLookupUnavailable``."""
        if not isinstance(guild_id, str) or not _GUILD_ID.fullmatch(guild_id):
            raise MemberAccessDenied
        user_id = session.user.id
        if not user_id.isdigit() or not session_lists(session, guild_id):
            raise MemberAccessDenied  # nenhuma chamada ao Discord para um id fora das listas da própria sessão
        present = ready_bots_with_guild(self._pool, int(guild_id))
        if not present:
            raise MemberAccessDenied
        key = (user_id, guild_id)
        if not fresh:
            found, cached = self._cached(key)
            if found:
                if cached is None:
                    raise MemberAccessDenied
                return cached
        generation = self._generation.get(user_id, 0)
        bot, guild = present[0]
        try:
            await self._find_member(bot, guild, int(user_id), fresh=fresh)
        except MemberAccessDenied:
            if not fresh:
                self._remember(key, None, generation)
            raise
        access = MemberAccess(
            guild_id=guild_id,
            name=guild.name,
            icon_url=guild_icon_url(guild_id, guild.icon.key if guild.icon else None),
        )
        if not fresh:
            self._remember(key, access, generation)
        return access

    async def _find_member(self, bot: Any, guild: Any, user_id: int, *, fresh: bool) -> Any:
        if not fresh:
            member = guild.get_member(user_id)
            if member is not None:
                return self._admitted(member)
        bot_id = str(bot.user.id)
        if not self._budget.acquire(bot_id):
            raise MemberLookupUnavailable
        try:
            member = await asyncio.wait_for(guild.fetch_member(user_id), FETCH_TIMEOUT_SECONDS)
        except disnake.NotFound, disnake.Forbidden:
            raise MemberAccessDenied from None  # resposta definitiva do Discord: a pessoa não está (ou o bot não pode ver)
        except (disnake.HTTPException, asyncio.TimeoutError, OSError, aiohttp.ClientError) as exc:
            logger.warning("Falha ao consultar membro no Discord (%s)", type(exc).__name__)  # só o tipo (CC2, T11)
            raise MemberLookupUnavailable from None
        finally:
            self._budget.release(bot_id)
        return self._admitted(member)

    @staticmethod
    def _admitted(member: Any) -> Any:
        if getattr(member, "pending", False):
            raise MemberAccessDenied  # ainda na triagem de membro do servidor
        return member

    def purge_user(self, user_id: str) -> None:
        """Ouvinte de ``SessionStore.destroy_user`` (apagamento e banimento): descarta as decisões do usuário e muda a geração dele, para
        uma consulta em andamento não gravar depois (CC3)."""
        user_id = str(user_id)
        self._generation[user_id] = self._generation.get(user_id, 0) + 1
        for key in [key for key in self._cache if key[0] == user_id]:
            del self._cache[key]

    def _cached(self, key: tuple[str, str]) -> tuple[bool, Optional[MemberAccess]]:
        entry = self._cache.get(key)
        if entry is None:
            return False, None
        expires_at, access = entry
        if self._clock() >= expires_at:
            del self._cache[key]
            return False, None
        return True, access

    def _remember(self, key: tuple[str, str], access: Optional[MemberAccess], generation: int) -> None:
        if self._cache_seconds <= 0 or self._generation.get(key[0], 0) != generation:
            return  # apagamento ou banimento durante a consulta: nada volta ao cache (CC3)
        self._cache.pop(key, None)
        while len(self._cache) >= MAX_CACHE_ENTRIES:
            self._cache.popitem(last=False)
        self._cache[key] = (self._clock() + self._cache_seconds, access)
