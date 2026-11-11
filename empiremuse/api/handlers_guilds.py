"""Rotas de servidores e configurações (B3, contrato T-456 §4 "Servidores" e "Configurações").

Toda rota de servidor autoriza o usuário **a cada requisição** (o ``{g}`` só vale para quem gerencia o servidor agora); a
escrita reautoriza sem cache (``fresh=True``, m4a). Servidor negado e inexistente dão o mesmo ``404``.
"""

from __future__ import annotations

from typing import Any

from empiremuse.api.data import DataHandler, form_reply
from empiremuse.api.errors import BOT_UNAVAILABLE, FORBIDDEN, ApiError, conflict, validation_failed
from empiremuse.api.handlers import guild_payload
from empiremuse.api.idempotency import Reply
from empiremuse.api.writes import parse_guild_write
from empiremuse.panel.handlers.servers import missing_invites
from empiremuse.panel.handlers.settings import CATEGORY_FORMS
from empiremuse.panel.services.base import ForbiddenAction, FormContext, SettingsForm
from empiremuse.rules.panel_serialize import GUILD, serialize_access, serialize_form, serialize_invites
from empiremuse.rules.playlists import SCOPES
from empiremuse.utils.servers import ServerConfigConflict, ServerConfigUnavailable

GUILD_ID = r"([0-9]{1,20})"
CATEGORY = r"(player|dj|appearance|links|general)"
SERVICE_CATEGORY = {"player": "player", "dj": "dj", "appearance": "aparencia", "links": "links", "general": "geral"}
JSON_UTF8 = "application/json; charset=UTF-8"


class GuildsHandler(DataHandler):
    """``GET /guilds``: os servidores do login (pré-filtro) e, em cada um, se há bot pronto e os convites dos que faltam."""

    def get(self) -> None:
        guilds = sorted(self.session.guilds, key=lambda guild: (guild.name.casefold(), guild.id))
        entries = []
        for guild in guilds:
            present, invites = missing_invites(self.ctx.pool, guild.id)
            entries.append({**guild_payload(guild), "bot_present": present, "invites": serialize_invites(invites)})
        self.write({"guilds": entries})


class GuildHandler(DataHandler):
    async def get(self, guild_id: str) -> None:
        _, access = await self.guild_access(guild_id, write=False)
        _, invites = missing_invites(self.ctx.pool, guild_id)
        self.write({**serialize_access(access), "invites": serialize_invites(invites)})


class SettingsHandler(DataHandler):
    """``GET`` lê os formulários da categoria; ``PUT`` grava um deles, com ``expected_version`` (``409`` com a versão atual)."""

    @staticmethod
    def forms_of(category: str) -> tuple[SettingsForm, ...]:
        return CATEGORY_FORMS[SERVICE_CATEGORY[category]]

    async def get(self, guild_id: str, category: str) -> None:
        ctx, _ = await self.guild_access(guild_id, write=False)
        service = self.ctx.settings_service
        try:
            forms = {
                form.key: serialize_form(await service.read(form, ctx, ""), GUILD) for form in self.forms_of(category)
            }
        except ServerConfigUnavailable:
            raise ApiError(503, BOT_UNAVAILABLE) from None
        self.write({"category": category, "forms": forms})

    async def put(self, guild_id: str, category: str) -> None:
        ctx, _ = await self.guild_access(guild_id, write=True)  # antes de olhar o corpo: nada vaza a quem não pode
        body = self.json_body()
        forms = self.forms_of(category)
        parsed = parse_guild_write([form.key for form in forms], body)
        form = next(candidate for candidate in forms if candidate.key == parsed.form_key)

        async def work() -> Reply:
            return await self.apply(form, ctx, parsed.write.service_args(), category)

        await self.run_write(body, work)

    async def apply(self, form: SettingsForm, ctx: FormContext, args: dict[str, list[str]], category: str) -> Reply:
        service = self.ctx.settings_service
        try:
            try:
                outcome = await service.submit(
                    form, ctx, args, "", audit_user=ctx.user_id, category=SERVICE_CATEGORY[category]
                )
            except ForbiddenAction:
                raise ApiError(403, FORBIDDEN) from None
            except (
                ServerConfigConflict
            ):  # a gravação concorrente do store esgotou as tentativas: reler e tentar de novo
                raise conflict((await service.read(form, ctx, "")).versao) from None
        except ServerConfigUnavailable:
            raise ApiError(503, BOT_UNAVAILABLE) from None
        return form_reply(outcome.status, outcome.form, GUILD)


class LinksExportHandler(DataHandler):
    """``GET /guilds/{g}/links/export[?scope=jukebox|radio]``: as playlists do servidor no formato de importação."""

    async def get(self, guild_id: str) -> None:
        ctx, _ = await self.guild_access(guild_id, write=False)
        scope = self.get_query_argument("scope", None, strip=False)
        if scope is not None and scope not in SCOPES:
            raise validation_failed("scope")
        try:
            content = await ctx.pool.servers.playlists.export_bytes(ctx.guild_id, scope=scope)
        except ServerConfigUnavailable:
            raise ApiError(503, BOT_UNAVAILABLE) from None
        self.set_header("Content-Type", JSON_UTF8)
        self.finish(content)


def guild_routes() -> list[tuple[str, Any, str]]:
    """(expressão do caminho sob o prefixo, handler, caminho do OpenAPI)."""
    return [
        ("/guilds", GuildsHandler, "/guilds"),
        (f"/guilds/{GUILD_ID}", GuildHandler, "/guilds/{guild_id}"),
        (f"/guilds/{GUILD_ID}/settings/{CATEGORY}", SettingsHandler, "/guilds/{guild_id}/settings/{category}"),
        (f"/guilds/{GUILD_ID}/links/export", LinksExportHandler, "/guilds/{guild_id}/links/export"),
    ]
