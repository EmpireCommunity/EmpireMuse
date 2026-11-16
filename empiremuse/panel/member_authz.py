"""Autorização de MEMBRO por servidor (T-660, contrato r11 §2 c; threat model T1-T11, CC2 e CC3).

"Membro" = quem está no servidor sem precisar gerenciá-lo. Nunca autoriza nada de gerente: o ``GuildAuthorizer`` (``authz.py``) segue
exigindo ``session.guilds`` e tem cache próprio; este módulo não o lê nem o alimenta.

Ordem (todo fracasso definitivo = o mesmo ``MemberAccessDenied``, que a API devolve como o mesmo ``404``):
1. id no formato; 2. id em ``session.guilds`` ou em ``session.member_guild_ids`` (senão nega **sem tocar no Discord**); 3. bot pronto do
pool no servidor; 4. membro agora: a ESCRITA sempre pergunta ao Discord (``fetch_member``: a resposta da API é a fonte que manda; o cache do
gateway pode estar atrasado num evento de saída ou de expulsão); a LEITURA usa o cache do gateway e, num servidor já carregado (o bot liga
sempre o intent de membros, ``utils/client.py``), "não está no cache" é a negação definitiva, sem consulta; só num servidor ainda não
carregado a leitura pergunta ao Discord; 5. ``pending`` (triagem de membro) nega; 6. nenhuma permissão exigida.

CC2: toda consulta ao Discord gasta, nesta ordem, um balde POR (bot, usuário) e o orçamento GLOBAL do bot (balde e teto de concorrência, entre
todos os usuários): uma pessoa sozinha (por exemplo, com ids velhos na sessão) esgota o próprio balde antes de tirar o dos outros. Sem
orçamento, tempo esgotado, 5xx e 429 do Discord = ``MemberLookupUnavailable`` (a API devolve ``503``) e **nunca** entram no cache; só
``NotFound`` e ``Forbidden`` (a pessoa não está ou o bot não pode ver) viram a negação guardada.
CC3: o cache (só de leitura, 30 s, positivo e negativo, teto com LRU) tem uma GERAÇÃO por usuário que ``purge_user`` (ouvinte do
apagamento e do banimento) muda enquanto houver consulta em andamento dele: a consulta que termina depois do apagamento não grava nada. Sem
consulta em andamento não há geração guardada (o dicionário não cresce por usuário apagado).
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
# Orçamento global por bot para as consultas de membro ao Discord (entre todos os usuários): rajada de 10, 2 por segundo, 4 ao mesmo tempo.
BUDGET_BURST = 10
BUDGET_PER_SECOND = 2.0
BUDGET_CONCURRENCY = 4
# O balde de cada usuário em cada bot, conferido ANTES do global (SF1, contrato r11): rajada de 5, uma consulta a cada 5 s.
USER_BURST = 5
USER_PER_SECOND = 0.2
MAX_USER_BUCKETS = 10_000
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


@dataclass(frozen=True)
class MemberGrant:
    """A decisão e o par (bot, servidor) que ela usou: quem chama nunca procura o bot de novo (CS3)."""

    access: MemberAccess
    bot: Any
    guild: Any


class _Bucket:
    """Um balde de fichas com relógio injetável (sem espera)."""

    def __init__(self, burst: float, per_second: float, now: float) -> None:
        self.burst, self.rate, self.tokens, self.at = burst, per_second, float(burst), now

    def refill(self, now: float) -> float:
        self.tokens, self.at = min(self.burst, self.tokens + (now - self.at) * self.rate), now
        return self.tokens


class MemberBudget:
    """Os baldes das consultas de membro (CC2, SF1): um por (bot, usuário), conferido antes, e o global do bot com teto de concorrência.
    ``acquire`` gasta uma ficha dos dois ou de nenhum e devolve ``False`` quando não há como consultar agora; quem consegue chama ``release``."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        *,
        burst: int = BUDGET_BURST,
        per_second: float = BUDGET_PER_SECOND,
        concurrency: int = BUDGET_CONCURRENCY,
        user_burst: int = USER_BURST,
        user_per_second: float = USER_PER_SECOND,
    ) -> None:
        self._clock, self._concurrency = clock, concurrency
        self._burst, self._rate, self._user_burst, self._user_rate = burst, per_second, user_burst, user_per_second
        self._bots: dict[str, _Bucket] = {}
        self._users: OrderedDict[tuple[str, str], _Bucket] = OrderedDict()
        self._running: dict[str, int] = {}

    def acquire(self, bot_id: str, user_id: str) -> bool:
        now = self._clock()
        user = self._users.pop((bot_id, user_id), None) or _Bucket(self._user_burst, self._user_rate, now)
        self._users[(bot_id, user_id)] = user  # mais recente no fim; o mais antigo sai quando passa do teto
        while len(self._users) > MAX_USER_BUCKETS:
            self._users.popitem(last=False)
        bot = self._bots.setdefault(bot_id, _Bucket(self._burst, self._rate, now))
        if user.refill(now) < 1 or bot.refill(now) < 1 or self._running.get(bot_id, 0) >= self._concurrency:
            return False
        user.tokens -= 1
        bot.tokens -= 1
        self._running[bot_id] = self._running.get(bot_id, 0) + 1
        return True

    def release(self, bot_id: str) -> None:
        self._running[bot_id] = max(0, self._running.get(bot_id, 0) - 1)


def session_lists(session: Session, guild_id: str) -> bool:
    """O id está numa das listas da sessão (gerenciáveis ou de membro): o pré-filtro que vem antes de qualquer consulta ao Discord."""
    return guild_id in session.member_guild_ids or any(guild.id == guild_id for guild in session.guilds)


class MemberAuthorizer:
    """Decide se o usuário da sessão é membro agora do servidor. Cache só de leitura (LRU), por (usuário, servidor), com geração por usuário."""

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
        self._generation: dict[str, int] = {}  # só de quem tem consulta em andamento (CC3)
        self._inflight: dict[str, int] = {}

    async def authorize(self, session: Session, guild_id: str, *, fresh: bool) -> MemberGrant:
        """``fresh=True`` (escrita) nunca lê o cache e sempre pergunta ao Discord; uma negação dela também descarta a decisão guardada.
        Levanta ``MemberAccessDenied`` ou ``MemberLookupUnavailable``."""
        if not isinstance(guild_id, str) or not _GUILD_ID.fullmatch(guild_id):
            raise MemberAccessDenied
        user_id = session.user.id
        if not user_id.isdigit() or not session_lists(session, guild_id):
            raise MemberAccessDenied  # nenhuma chamada ao Discord para um id fora das listas da própria sessão
        present = ready_bots_with_guild(self._pool, int(guild_id))
        if not present:
            raise MemberAccessDenied
        bot, guild = present[0]
        key = (user_id, guild_id)
        if not fresh:
            found, cached = self._cached(key)
            if found:
                if cached is None:
                    raise MemberAccessDenied
                return MemberGrant(cached, bot, guild)
        generation = self._begin(user_id)
        try:  # o cache é gravado ANTES de ``_end``: só enquanto a geração da consulta ainda existe dá para saber se houve apagamento
            await self._find_member(bot, guild, user_id, fresh=fresh)
            icon = guild_icon_url(guild_id, guild.icon.key if guild.icon else None)
            access = MemberAccess(guild_id=guild_id, name=guild.name, icon_url=icon)
            if not fresh:
                self._remember(key, access, generation)
            return MemberGrant(access, bot, guild)
        except MemberAccessDenied:
            if fresh:
                self._cache.pop(key, None)  # quem saiu deixa de ler pela decisão guardada (CS1)
            else:
                self._remember(key, None, generation)
            raise
        finally:
            self._end(user_id)

    async def _find_member(self, bot: Any, guild: Any, user_id: str, *, fresh: bool) -> Any:
        if not fresh:
            member = guild.get_member(int(user_id))
            if member is not None:
                return self._admitted(member)
            if getattr(guild, "chunked", False) is True:
                raise MemberAccessDenied  # servidor carregado (intent de membros): fora do cache = não está, sem gastar REST (SF1)
        bot_id = str(bot.user.id)
        if not self._budget.acquire(bot_id, user_id):
            raise MemberLookupUnavailable
        try:
            member = await asyncio.wait_for(guild.fetch_member(int(user_id)), FETCH_TIMEOUT_SECONDS)
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
        """Ouvinte de ``SessionStore.destroy_user`` (apagamento e banimento): descarta as decisões do usuário e, se há consulta dele em
        andamento, muda a geração para ela não gravar depois (CC3)."""
        user_id = str(user_id)
        if self._inflight.get(user_id):
            self._generation[user_id] = self._generation.get(user_id, 0) + 1
        for key in [key for key in self._cache if key[0] == user_id]:
            del self._cache[key]

    def _begin(self, user_id: str) -> int:
        self._inflight[user_id] = self._inflight.get(user_id, 0) + 1
        return self._generation.get(user_id, 0)

    def _end(self, user_id: str) -> None:
        left = self._inflight.get(user_id, 1) - 1
        if left > 0:
            self._inflight[user_id] = left
            return
        self._inflight.pop(user_id, None)
        self._generation.pop(user_id, None)  # ninguém mais compara com ela: nada fica por usuário (CS2)

    def _cached(self, key: tuple[str, str]) -> tuple[bool, Optional[MemberAccess]]:
        entry = self._cache.get(key)
        if entry is None:
            return False, None
        expires_at, access = entry
        if self._clock() >= expires_at:
            del self._cache[key]
            return False, None
        self._cache.move_to_end(key)  # LRU: a usada agora é a última a sair (CM3)
        return True, access

    def _remember(self, key: tuple[str, str], access: Optional[MemberAccess], generation: int) -> None:
        if self._cache_seconds <= 0 or self._generation.get(key[0], 0) != generation:
            return  # apagamento ou banimento durante a consulta: nada volta ao cache (CC3)
        self._cache.pop(key, None)
        while len(self._cache) >= MAX_CACHE_ENTRIES:
            self._cache.popitem(last=False)
        self._cache[key] = (self._clock() + self._cache_seconds, access)
