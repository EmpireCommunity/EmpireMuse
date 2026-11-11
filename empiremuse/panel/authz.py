"""Autorização por servidor: decide, com dados do bot, se um usuário pode gerenciar um servidor.

``session.guilds`` (do login) é só pré-filtro: a decisão real exige (a) um bot pronto do pool no servidor,
(b) o usuário como membro atual e (c) dono, ADMINISTRATOR ou MANAGE_GUILD agora.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional
from urllib.parse import urlencode

import aiohttp
import disnake

from empiremuse.panel.sessions import Session

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool

logger = logging.getLogger(__name__)

CDN_BASE = "https://cdn.discordapp.com"
ICON_SIZE = 64
CACHE_SECONDS = 30.0
MAX_CACHE_ENTRIES = 10_000
FETCH_TIMEOUT_SECONDS = 5.0
_GUILD_ID = re.compile(r"[0-9]{1,20}", re.ASCII)
INVITE_URL = "https://discord.com/oauth2/authorize"
INVITE_SCOPE = "bot applications.commands"


class GuildAccessDenied(Exception):
    """Acesso negado ou servidor inexistente (a resposta HTTP é a mesma: 404 genérico)."""


@dataclass(frozen=True)
class BotRef:
    id: str
    name: str
    avatar_url: str


@dataclass(frozen=True)
class GuildAccess:
    guild_id: str
    name: str
    icon_url: Optional[str]
    bots: tuple[BotRef, ...]


def guild_icon_url(guild_id: str, icon_hash: Optional[str]) -> Optional[str]:
    """URL do ícone do servidor na CDN do Discord, ou ``None`` sem ícone."""
    if not icon_hash:
        return None
    return f"{CDN_BASE}/icons/{guild_id}/{icon_hash}.png?size={ICON_SIZE}"


def ready_bots_with_guild(pool: BotPool, guild_id: int) -> list[tuple[Any, Any]]:
    """Pares (bot, servidor) dos bots prontos do pool que estão no servidor (cache do gateway), em ordem estável."""
    found = []
    for bot in pool.bots:
        if not bot.is_ready():
            continue
        guild = bot.get_guild(guild_id)
        if guild is not None:
            found.append((bot, guild))
    return found


@dataclass(frozen=True)
class InviteLink:
    bot_name: str
    bot_avatar_url: str
    url: str


def invite_url(client_id: str, guild_id: str, permissions: int) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "scope": INVITE_SCOPE,
            "permissions": permissions,
            "guild_id": guild_id,
            "disable_guild_select": "true",
        }
    )
    return f"{INVITE_URL}?{query}"


def missing_invites(pool: Any, guild_id: str) -> tuple[bool, tuple[InviteLink, ...]]:
    """(há bot pronto no servidor, convites dos bots prontos do pool que ainda não estão nele)."""
    present_ids = {str(bot.user.id) for bot, _ in ready_bots_with_guild(pool, int(guild_id))}
    permissions = pool.config["INVITE_PERMISSIONS"]
    invites = tuple(
        InviteLink(
            bot.user.name,
            bot.user.display_avatar.url,
            invite_url(str(bot.application_id or bot.user.id), guild_id, permissions),
        )
        for bot in pool.bots
        if bot.is_ready() and str(bot.user.id) not in present_ids
    )
    return bool(present_ids), invites


class GuildAuthorizer:
    """Autoriza o acesso a um servidor; decisões positivas ficam em cache por até ``cache_seconds`` (só leituras)."""

    def __init__(
        self, pool: BotPool, *, clock: Callable[[], float] = time.monotonic, cache_seconds: float = CACHE_SECONDS
    ) -> None:
        self._pool = pool
        self._clock = clock
        self._cache_seconds = cache_seconds
        self._cache: OrderedDict[tuple[str, str], tuple[float, GuildAccess]] = OrderedDict()

    async def authorize(self, session: Session, guild_id: str, *, fresh: bool = False) -> GuildAccess:
        """Devolve o acesso ao servidor ou levanta ``GuildAccessDenied``. ``fresh=True`` ignora o cache."""
        if not isinstance(guild_id, str) or not _GUILD_ID.fullmatch(guild_id):
            raise GuildAccessDenied
        if not any(guild.id == guild_id for guild in session.guilds):
            raise GuildAccessDenied
        key = (session.user.id, guild_id)
        if not fresh:
            cached = self._cached(key)
            if cached is not None:
                return cached
        try:
            access = await self._decide(session, guild_id)
        except GuildAccessDenied:
            self._cache.pop(key, None)
            raise
        self._remember(key, access)
        return access

    async def _decide(self, session: Session, guild_id: str) -> GuildAccess:
        user_id = session.user.id
        if not user_id.isdigit():
            raise GuildAccessDenied
        present = ready_bots_with_guild(self._pool, int(guild_id))
        if not present:
            raise GuildAccessDenied
        guild = present[0][1]
        member = await self._find_member(guild, int(user_id))
        if member is None or not self._may_manage(guild, int(user_id), member):
            raise GuildAccessDenied
        bots = tuple(
            BotRef(id=str(bot.user.id), name=bot.user.name, avatar_url=bot.user.display_avatar.url)
            for bot, _ in present
        )
        icon = guild.icon.key if guild.icon else None
        return GuildAccess(guild_id=guild_id, name=guild.name, icon_url=guild_icon_url(guild_id, icon), bots=bots)

    @staticmethod
    def _may_manage(guild: Any, user_id: int, member: Any) -> bool:
        permissions = member.guild_permissions
        return guild.owner_id == user_id or bool(permissions.administrator) or bool(permissions.manage_guild)

    @staticmethod
    async def _find_member(guild: Any, user_id: int) -> Optional[Any]:
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await asyncio.wait_for(guild.fetch_member(user_id), FETCH_TIMEOUT_SECONDS)
        except disnake.HTTPException:
            return None  # NotFound, Forbidden e demais erros da API: negar
        except (asyncio.TimeoutError, OSError, aiohttp.ClientError) as exc:
            # Só o tipo da exceção: a mensagem pode trazer URL/query do Discord.
            logger.warning("Falha de rede ao verificar membro no painel (%s)", type(exc).__name__)
            return None

    def _cached(self, key: tuple[str, str]) -> Optional[GuildAccess]:
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, access = entry
        if self._clock() >= expires_at:
            del self._cache[key]
            return None
        return access

    def _remember(self, key: tuple[str, str], access: GuildAccess) -> None:
        if self._cache_seconds <= 0:
            return
        self._cache.pop(key, None)
        while len(self._cache) >= MAX_CACHE_ENTRIES:
            self._cache.popitem(last=False)
        self._cache[key] = (self._clock() + self._cache_seconds, access)
