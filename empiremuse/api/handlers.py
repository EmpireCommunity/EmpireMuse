"""Rotas da fundação (B1): ``/meta``, ``/health`` e as sessões de usuário (contrato T-456 §3 e §4)."""

from __future__ import annotations

import logging
import re
from typing import Any

from empiremuse.api.base import AUTH_KEY, AUTH_NONE, AUTH_SESSION, AUTH_SESSION_OPTIONAL, ApiHandler
from empiremuse.api.errors import (
    BOT_UNAVAILABLE,
    DISCORD_UNAVAILABLE,
    FORBIDDEN,
    REAUTH_USER_MISMATCH,
    SESSION_EXPIRED,
    ApiError,
    validation_failed,
)
from empiremuse.panel.oauth import (
    DiscordOAuthClient,
    DiscordOAuthError,
    parse_managed_guilds,
    parse_user,
    resolve_client_id,
)
from empiremuse.panel.sessions import ManagedGuild, PanelUser, Session
from empiremuse.utils.user_ban import user_bans_of

logger = logging.getLogger(__name__)

API_VERSIONS = ["v1"]
FEATURES = ["sessions", "reauth", "account", "guilds", "guild-settings", "idempotency-key"]
CODE_MAX_LENGTH = 512
_CODE_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}", re.ASCII)  # RFC 7636
_CODE_REFUSED = 400  # só o 400 diz que o código é inválido, já usado ou vencido; 401 é o client OAuth recusado


def user_payload(user: PanelUser) -> dict[str, Any]:
    return {"id": user.id, "username": user.username, "global_name": user.global_name, "avatar": user.avatar}


def guild_payload(guild: ManagedGuild) -> dict[str, Any]:
    # lista de permissão de campos; ``permissions`` vai como texto (o inteiro passa de 2**53 em alguns casos)
    return {
        "id": guild.id,
        "name": guild.name,
        "icon": guild.icon,
        "owner": guild.owner,
        "permissions": str(guild.permissions),
    }


class HealthHandler(ApiHandler):
    """Aberto só na porta interna, sem chave, para o healthcheck do painel (corpo fixo, nada mais)."""

    auth = AUTH_NONE

    def get(self) -> None:
        self.write({"status": "ok"})


class MetaHandler(ApiHandler):
    auth = AUTH_KEY

    def get(self) -> None:
        settings = self.ctx.settings
        client_id = resolve_client_id(self.ctx.pool, settings)
        if not client_id:
            raise ApiError(503, BOT_UNAVAILABLE)  # os bots ainda não subiram: o painel tenta de novo
        self.write(
            {
                "api_versions": API_VERSIONS,
                "bot_version": self.ctx.bot_version(),
                "client_id": client_id,
                "redirect_uri": settings.redirect_uri,
                "features": FEATURES,
            }
        )


class SessionHandler(ApiHandler):
    auth = AUTH_SESSION

    def get(self) -> None:
        self.write(session_payload(self.ctx, self.session))

    def delete(self) -> None:
        self.ctx.store.destroy_api(self.session_token)
        self.set_status(204)


def session_payload(ctx: Any, session: Session, token: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if token is not None:
        payload["session_token"] = token
    payload.update(
        user=user_payload(session.user),
        guilds=[guild_payload(guild) for guild in session.guilds],
        expires_at=ctx.iso_at(ctx.store.expires_at(session)),
        authenticated_at=ctx.iso_at(session.created_at),
    )
    return payload


class SessionsHandler(ApiHandler):
    """``POST /sessions``: o bot troca o ``code`` do Discord, cria a sessão e devolve o token (uma única vez)."""

    auth = AUTH_SESSION_OPTIONAL
    creates_sessions = True

    async def post(self) -> None:
        body = self.json_body()
        code, verifier, reauth = body.get("code"), body.get("code_verifier"), body.get("reauth", False)
        invalid = [
            name
            for name, ok in (
                ("code", isinstance(code, str) and 0 < len(code) <= CODE_MAX_LENGTH and code.isascii()),
                ("code_verifier", isinstance(verifier, str) and _CODE_VERIFIER.fullmatch(verifier) is not None),
                ("reauth", isinstance(reauth, bool)),
            )
            if not ok
        ]
        if invalid:
            raise validation_failed(*invalid)
        if reauth and self.session is None:
            raise ApiError(401, SESSION_EXPIRED)  # a reautenticação amarra o usuário da sessão atual (M1)
        bans = user_bans_of(self.ctx.pool)
        if bans is not None and not bans.loaded:
            raise ApiError(503, BOT_UNAVAILABLE)  # sem a lista de banidos o login fica fechado (D2)
        oauth = self.ctx.oauth_provider()
        if oauth is None:
            raise ApiError(503, BOT_UNAVAILABLE)
        user, guilds = await self._read_identity(oauth, code, verifier)
        store = self.ctx.store
        # Daqui até criar a sessão não há ``await``: o que se confere agora vale na hora de gravar.
        anchor = store.get_api(self.session_token, self.key_fingerprint) if reauth else None
        # A sessão de origem pode ter morrido durante a troca com o Discord (logout, apagamento, outra reauth).
        if reauth and anchor is None:
            raise ApiError(401, SESSION_EXPIRED)
        if bans is not None and bans.is_blocked(user.id):
            raise ApiError(403, FORBIDDEN)
        if reauth:
            if user.id != anchor.user.id:
                raise ApiError(403, REAUTH_USER_MISMATCH)  # nada muda
            store.destroy_api(self.session_token)  # nunca renova no lugar: token novo, o antigo morre
        token = store.create_api(user, guilds, self.key_fingerprint)
        session = store.get_api(token, self.key_fingerprint)
        self.set_status(201)
        self.write(session_payload(self.ctx, session, token))

    async def _read_identity(
        self, oauth: DiscordOAuthClient, code: str, verifier: str
    ) -> tuple[PanelUser, tuple[ManagedGuild, ...]]:
        """Troca o código e lê usuário e servidores; o token do Discord é sempre revogado e nunca guardado nem logado."""
        access_token = None
        try:
            try:
                access_token = await oauth.exchange_code(code, verifier)
            except DiscordOAuthError as error:
                if error.status == _CODE_REFUSED:
                    logger.warning("API do painel externo: o Discord recusou o código: %s", error)
                    raise validation_failed("code") from None
                logger.error("API do painel externo: falha na troca do código com o Discord: %s", error)
                raise ApiError(502, DISCORD_UNAVAILABLE) from None
            try:
                user = parse_user(await oauth.fetch_user(access_token))
                guilds = parse_managed_guilds(await oauth.fetch_guilds(access_token))
            except DiscordOAuthError as error:
                logger.warning("API do painel externo: falha ao ler a identidade no Discord: %s", error)
                raise ApiError(502, DISCORD_UNAVAILABLE) from None
            return user, guilds
        finally:
            if access_token:
                await self._revoke(oauth, access_token)

    @staticmethod
    async def _revoke(oauth: DiscordOAuthClient, access_token: str) -> None:
        try:
            await oauth.revoke(access_token)
        except DiscordOAuthError as error:
            logger.warning("API do painel externo: não foi possível revogar o token do Discord: %s", error)
