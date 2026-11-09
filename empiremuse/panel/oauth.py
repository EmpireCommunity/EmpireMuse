"""Cliente OAuth2 do Discord (authorization code + PKCE S256). Tokens e segredos nunca vão para logs ou erros."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import secrets
from typing import Any, Callable, Optional
from urllib.parse import urlencode

import aiohttp

from empiremuse.panel.sessions import ManagedGuild, PanelUser
from empiremuse.panel.settings import PanelSettings

DISCORD_API = "https://discord.com/api/v10"
AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
OAUTH_SCOPE = "identify guilds"
PROMPT_NONE = "none"  # login silencioso: o padrão de todo login
PROMPT_CONSENT = "consent"  # a pessoa reconfirma no Discord (só para apagar dados)
_PROMPTS = frozenset({PROMPT_NONE, PROMPT_CONSENT})
REQUEST_TIMEOUT_SECONDS = 10
GUILDS_PAGE_LIMIT = 200
VERIFIER_BYTES = 48
PERMISSION_ADMINISTRATOR = 0x8
PERMISSION_MANAGE_GUILD = 0x20
MANAGEMENT_PERMISSIONS = PERMISSION_ADMINISTRATOR | PERMISSION_MANAGE_GUILD


class DiscordOAuthError(Exception):
    """Falha na comunicação com o Discord. A mensagem nunca contém tokens, código ou segredos."""


def _default_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS))


def make_pkce_pair() -> tuple[str, str]:
    """Devolve ``(code_verifier, code_challenge)`` com método S256 (RFC 7636)."""
    verifier = secrets.token_urlsafe(VERIFIER_BYTES)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class DiscordOAuthClient:
    def __init__(
        self, settings: PanelSettings, client_id: str, session_factory: Callable[[], Any] = _default_session
    ) -> None:
        self._settings = settings
        self._client_id = client_id
        self._session_factory = session_factory

    def authorize_url(self, state: str, code_challenge: str, prompt: str = PROMPT_NONE) -> str:
        if prompt not in _PROMPTS:
            raise ValueError("prompt inválido")
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._client_id,
                "scope": OAUTH_SCOPE,
                "state": state,
                "redirect_uri": self._settings.redirect_uri,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "prompt": prompt,
            }
        )
        return f"{AUTHORIZE_URL}?{query}"

    async def exchange_code(self, code: str, code_verifier: str) -> str:
        data = await self._request(
            "POST",
            "/oauth2/token",
            data={
                "client_id": self._client_id,
                "client_secret": self._settings.oauth_client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._settings.redirect_uri,
                "code_verifier": code_verifier,
            },
        )
        token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise DiscordOAuthError("Resposta do Discord sem token de acesso.")
        return token

    async def fetch_user(self, access_token: str) -> dict:
        data = await self._request("GET", "/users/@me", headers=_bearer(access_token))
        if not isinstance(data, dict):
            raise DiscordOAuthError("Resposta inesperada do Discord ao ler o usuário.")
        return data

    async def fetch_guilds(self, access_token: str) -> list[dict]:
        data = await self._request(
            "GET", "/users/@me/guilds", headers=_bearer(access_token), params={"limit": GUILDS_PAGE_LIMIT}
        )
        if not isinstance(data, list):
            raise DiscordOAuthError("Resposta inesperada do Discord ao ler os servidores.")
        return data

    async def revoke(self, access_token: str) -> None:
        await self._request(
            "POST",
            "/oauth2/token/revoke",
            data={
                "token": access_token,
                "token_type_hint": "access_token",
                "client_id": self._client_id,
                "client_secret": self._settings.oauth_client_secret,
            },
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            async with self._session_factory() as session:
                async with session.request(method, DISCORD_API + path, **kwargs) as response:
                    if response.status >= 400:
                        raise DiscordOAuthError(f"O Discord respondeu com status {response.status}.")
                    return await response.json(content_type=None)
        except DiscordOAuthError:
            raise
        except aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError:
            raise DiscordOAuthError("Falha de comunicação com o Discord.") from None


def _bearer(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def resolve_client_id(pool: Any, settings: PanelSettings) -> str:
    """``PANEL_OAUTH_CLIENT_ID`` ou, se vazio, o id da aplicação do primeiro bot do pool ("" se indisponível)."""
    if settings.oauth_client_id:
        return settings.oauth_client_id
    for bot in getattr(pool, "bots", None) or []:
        app_id = getattr(bot, "application_id", None) or getattr(getattr(bot, "user", None), "id", None)
        if app_id:
            return str(app_id)
    return ""


def make_client_provider(
    pool: Any, settings: PanelSettings, oauth_client: Optional[DiscordOAuthClient] = None
) -> Callable[[], Optional[DiscordOAuthClient]]:
    """Devolve uma função que entrega o cliente OAuth, resolvendo o client_id só no primeiro uso
    (os bots podem ainda não estar prontos quando o servidor web sobe)."""
    cache: list[DiscordOAuthClient] = [oauth_client] if oauth_client else []

    def provider() -> Optional[DiscordOAuthClient]:
        if not cache:
            client_id = resolve_client_id(pool, settings)
            if not client_id:
                return None
            cache.append(DiscordOAuthClient(settings, client_id))
        return cache[0]

    return provider


def parse_user(raw: dict) -> PanelUser:
    """Converte a resposta de ``/users/@me`` em ``PanelUser`` (levanta ``DiscordOAuthError`` se inválida)."""
    user_id, username = raw.get("id"), raw.get("username")
    if not isinstance(user_id, str) or not user_id.isdigit() or not isinstance(username, str) or not username:
        raise DiscordOAuthError("Resposta inesperada do Discord ao ler o usuário.")
    global_name, avatar = raw.get("global_name"), raw.get("avatar")
    return PanelUser(
        id=user_id,
        username=username,
        global_name=global_name if isinstance(global_name, str) and global_name else None,
        avatar=avatar if isinstance(avatar, str) and avatar else None,
    )


_GUILD_ID = re.compile(r"[0-9]{1,20}", re.ASCII)


def parse_managed_guilds(raw: list[dict]) -> tuple[ManagedGuild, ...]:
    """Mantém só servidores em que o usuário é dono, administrador ou tem Gerenciar Servidor (pré-filtro)."""
    guilds = []
    for item in raw:
        try:
            guild_id = item["id"]
            if not isinstance(guild_id, str) or not _GUILD_ID.fullmatch(guild_id):
                continue  # só ids de servidor numéricos (str de dígitos ASCII)
            permissions = int(item.get("permissions", 0))
            guild = ManagedGuild(
                id=guild_id,
                name=str(item["name"]),
                icon=item.get("icon") or None,
                permissions=permissions,
                owner=bool(item.get("owner", False)),
            )
        except AttributeError, KeyError, TypeError, ValueError:
            continue
        if guild.owner or permissions & MANAGEMENT_PERMISSIONS:
            guilds.append(guild)
    return tuple(guilds)
