"""Páginas de servidores: lista (com convites dos bots ausentes) e visão geral de um servidor autorizado."""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from empiremuse.panel.authz import GuildAccessDenied, guild_icon_url, ready_bots_with_guild
from empiremuse.panel.handlers.base import PanelHandler
from empiremuse.panel.sessions import ManagedGuild, Session

SERVERS_PATH = "/painel/servidores"
LOGIN_PATH = "/painel/login"
INVITE_URL = "https://discord.com/oauth2/authorize"
INVITE_SCOPE = "bot applications.commands"
NOT_FOUND_TITLE = "Servidor não encontrado"
NOT_FOUND_MESSAGE = "Servidor não encontrado ou você não tem permissão para gerenciá-lo."
MAX_BODY_BYTES = 16 * 1024
HX_REQUEST = "HX-Request"
CATEGORIES = ("Player", "Jukebox", "DJ", "Aparência", "Links", "Geral")


@dataclass(frozen=True)
class InviteLink:
    bot_name: str
    bot_avatar_url: str
    url: str


@dataclass(frozen=True)
class ServerCard:
    guild_id: str
    name: str
    icon_url: str | None
    initial: str
    configurable: bool
    invites: tuple[InviteLink, ...]


def decode_args(handler: Any) -> dict[str, list[str]]:
    """Argumentos do corpo do POST como texto (bytes inválidos viram U+FFFD)."""
    return {
        name: [value.decode("utf-8", "replace") for value in values]
        for name, values in handler.request.body_arguments.items()
    }


def initial_of(name: str) -> str:
    """Primeiro caractere do nome em maiúscula (para quando o servidor não tem ícone)."""
    return name.strip()[:1].upper() or "?"


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


def build_card(guild: ManagedGuild, pool: Any) -> ServerCard:
    """Cartão de um servidor da sessão: "Configurar" se há bot pronto nele; senão um convite por bot ausente."""
    configurable, invites = missing_invites(pool, guild.id)
    return ServerCard(
        guild_id=guild.id,
        name=guild.name,
        icon_url=guild_icon_url(guild.id, guild.icon),
        initial=initial_of(guild.name),
        configurable=configurable,
        invites=invites,
    )


class AuthenticatedHandler(PanelHandler):
    """Exige sessão: anônimo é enviado a ``/painel/login?next=<caminho>`` (sem consultar o Discord)."""

    def prepare(self) -> None:
        if self.current_session is None:
            self.redirect(f"{LOGIN_PATH}?{urlencode({'next': self.request.path})}")

    @property
    def session(self) -> Session:
        return self.current_session

    def _is_htmx(self) -> bool:
        return self.request.headers.get(HX_REQUEST) == "true"

    def _reject_plain(self, status: int, title: str, message: str) -> None:
        """413/422 sem formulário: página de erro, ou só o aviso quando a chamada vem do htmx."""
        if not self._is_htmx():
            self.render_error(status, title, message)
            return
        self.set_status(status)
        self.set_header("Cache-Control", "no-store")
        self.set_header("Content-Type", "text/html; charset=UTF-8")
        self.finish(f'<p class="aviso aviso-erro" role="alert">{html.escape(message)}</p>')

    def _write_allowed(self) -> bool:
        return self.application.settings["write_limiter"].allow(f"escrita:{self.session.user.id}")


class ServersHandler(AuthenticatedHandler):
    def get(self) -> None:
        guilds = sorted(self.session.guilds, key=lambda g: (g.name.casefold(), g.id))
        cards = tuple(build_card(guild, self.application.settings["pool"]) for guild in guilds)
        self.render_page("servidores.html", cartoes=cards)


class ServerDetailHandler(AuthenticatedHandler):
    async def get(self, guild_id: str) -> None:
        authorizer = self.application.settings["authorizer"]
        try:
            access = await authorizer.authorize(self.session, guild_id)
        except GuildAccessDenied:
            self.render_error(404, NOT_FOUND_TITLE, NOT_FOUND_MESSAGE)
            return
        _, invites = missing_invites(self.application.settings["pool"], access.guild_id)
        self.render_page(
            "servidor.html", acesso=access, inicial=initial_of(access.name), categorias=CATEGORIES, convites=invites
        )
