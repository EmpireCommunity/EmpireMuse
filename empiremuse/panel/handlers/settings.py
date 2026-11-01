"""Páginas de configuração do servidor (Player, DJ, Aparência, Playlists e Geral): GET mostra, POST grava um formulário.

Só HTTP: regras, validações e efeitos ficam em ``panel/services/``. Toda escrita reautoriza com ``fresh=True``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import urlencode

from empiremuse.panel.authz import BotRef, GuildAccess, GuildAccessDenied, ready_bots_with_guild
from empiremuse.panel.handlers.servers import (
    MAX_BODY_BYTES,
    NOT_FOUND_MESSAGE,
    NOT_FOUND_TITLE,
    SERVERS_PATH,
    AuthenticatedHandler,
    decode_args,
    initial_of,
)
from empiremuse.panel.services.appearance import SkinsGlobalForm, SkinsServerForm
from empiremuse.panel.services.base import (
    CONFLICT_TEXT,
    DIGITS,
    KIND_ERROR,
    ForbiddenAction,
    Formulario,
    FormContext,
    SettingsForm,
)
from empiremuse.panel.services.general import LanguageForm, PrefixForm, VoiceStatusForm
from empiremuse.panel.services.links import LinksForm
from empiremuse.panel.services.listen_along import ListenAlongForm
from empiremuse.panel.services.player import DjForm, PlayerForm
from empiremuse.panel.services.service import Outcome, SettingsService
from empiremuse.utils.servers import ServerConfigConflict, ServerConfigUnavailable
from empiremuse.utils.usage import record_panel_action

logger = logging.getLogger(__name__)

WRITE_LIMIT_PER_MINUTE = 20
CATEGORY_FORMS: dict[str, tuple[SettingsForm, ...]] = {
    "player": (PlayerForm(),),
    "dj": (DjForm(),),
    "aparencia": (SkinsServerForm(), SkinsGlobalForm()),
    "links": (LinksForm(),),
    "geral": (PrefixForm(), VoiceStatusForm(), LanguageForm(), ListenAlongForm()),
}
PER_BOT_CATEGORIES: frozenset[str] = (
    frozenset()
)  # tudo é do servidor (doc único): sem seletor de bot em categoria alguma
CHANNEL_CATEGORY = "canal"  # tem handler próprio (panel/handlers/channel.py); quem executa no Discord é o bot do painel
_MSG_TOO_LARGE = ("Envio grande demais", "Os dados enviados excedem o limite permitido.")
_MSG_RATE = "Muitas alterações em pouco tempo. Aguarde um minuto e tente novamente."
_MSG_NO_FORM = ("Dados inválidos", "Não foi possível identificar o formulário enviado.")
UNAVAILABLE_TITLE = "Configuração indisponível"
_MSG_FORBIDDEN = "Você não tem a permissão necessária no Discord para esta ação (Gerenciar Canais)."


def pick_form(forms: tuple[SettingsForm, ...], args: dict[str, list[str]]) -> Optional[SettingsForm]:
    """Formulário endereçado pelo POST (categoria com um só formulário sempre é ele; senão, pelos campos-marcador)."""
    if len(forms) == 1:
        return forms[0]
    return next((form for form in forms if any(name in args for name in form.markers)), None)


class SettingsHandler(AuthenticatedHandler):
    @property
    def service(self) -> SettingsService:
        return self.application.settings["settings_service"]

    async def get(self, guild_id: str, category: str) -> None:
        try:
            await self._get(guild_id, category)
        except ServerConfigUnavailable as exc:  # leitura do doc único falhou: mensagem própria, não o 500 genérico
            self.render_error(503, UNAVAILABLE_TITLE, exc.text)

    async def post(self, guild_id: str, category: str) -> None:
        try:
            await self._post(guild_id, category)
        except ServerConfigUnavailable as exc:  # vale também para a releitura dos blocos de Conflict, 429 e respostas
            self.render_error(503, UNAVAILABLE_TITLE, exc.text)

    async def _get(self, guild_id: str, category: str) -> None:
        try:
            ctx, access, action = await self._resolve(guild_id, category, fresh=False)
        except GuildAccessDenied:
            self._not_found()
            return
        states = {form.key: await self.service.read(form, ctx, action) for form in CATEGORY_FORMS[category]}
        self._render_full(category, access, ctx, states)

    async def _post(self, guild_id: str, category: str) -> None:
        if len(self.request.body) > MAX_BODY_BYTES:
            self._reject_plain(413, *_MSG_TOO_LARGE)
            return
        if category in CATEGORY_FORMS and not self._write_allowed():
            await self._rate_limited(guild_id, category)
            return
        try:
            ctx, access, action = await self._resolve(guild_id, category, fresh=True)
        except GuildAccessDenied:
            self._not_found()
            return
        args = decode_args(self)
        form = pick_form(CATEGORY_FORMS[category], args)
        if form is None:
            self._reject_plain(422, *_MSG_NO_FORM)
            return
        try:
            outcome = await self.service.submit(form, ctx, args, action, audit_user=ctx.user_id, category=category)
        except ForbiddenAction:
            state = await self.service.read(form, ctx, action, message=_MSG_FORBIDDEN, kind=KIND_ERROR)
            outcome = Outcome(403, state)
        except ServerConfigConflict:  # a gravação concorrente do store esgotou as tentativas: o usuário recarrega
            state = await self.service.read(form, ctx, action, message=CONFLICT_TEXT, kind=KIND_ERROR)
            outcome = Outcome(409, state)
        record_panel_action(ctx, f"{category}/{form.key}", outcome.status)
        await self._respond(category, access, ctx, action, form, outcome)

    # ------------------------------------------------------------------ resolução (autorização + bot)

    async def _resolve(
        self, guild_id: str, category: str, *, fresh: bool, bot_id: Optional[str] = None
    ) -> tuple[FormContext, GuildAccess, str]:
        if category not in CATEGORY_FORMS and category != CHANNEL_CATEGORY:
            raise GuildAccessDenied
        pool = self.application.settings["pool"]
        access = await self.application.settings["authorizer"].authorize(self.session, guild_id, fresh=fresh)
        per_bot = category in PER_BOT_CATEGORIES
        if bot_id is not None:
            if bot_id not in {bot.id for bot in access.bots}:
                raise GuildAccessDenied
        elif per_bot:
            bot_id = self._chosen_bot_id(access)
        elif category == CHANNEL_CATEGORY:
            bot_id = await self._panel_bot_id(pool, guild_id, access)
        else:
            bot_id = access.bots[0].id
        pair = next((p for p in ready_bots_with_guild(pool, int(guild_id)) if str(p[0].user.id) == bot_id), None)
        if pair is None:
            raise GuildAccessDenied
        ctx = FormContext(pool=pool, bot=pair[0], guild=pair[1], guild_id=int(guild_id), user_id=self.session.user.id)
        action = f"{SERVERS_PATH}/{guild_id}/{category}"
        if per_bot:
            action += "?" + urlencode({"bot": bot_id})
        return ctx, access, action

    @staticmethod
    async def _panel_bot_id(pool: Any, guild_id: str, access: GuildAccess) -> str:
        """O bot do painel executa setup/reset do canal de pedidos; sem eleição válida, o 1º bot autorizado."""
        try:
            panel_bot = await pool.servers.request.panel_bot(int(guild_id))
        except ServerConfigUnavailable:
            panel_bot = None
        allowed = {bot.id for bot in access.bots}
        if panel_bot is not None and panel_bot.user is not None and str(panel_bot.user.id) in allowed:
            return str(panel_bot.user.id)
        return access.bots[0].id

    def _chosen_bot_id(self, access: GuildAccess) -> str:
        requested = self.get_query_argument("bot", None, strip=False)
        if requested is None:
            return access.bots[0].id
        if not DIGITS.fullmatch(requested) or requested not in {bot.id for bot in access.bots}:
            raise GuildAccessDenied
        return requested

    # ------------------------------------------------------------------ respostas

    def _not_found(self) -> None:
        self.render_error(404, NOT_FOUND_TITLE, NOT_FOUND_MESSAGE)

    async def _rate_limited(self, guild_id: str, category: str) -> None:
        self.set_header("Retry-After", "60")
        form = pick_form(CATEGORY_FORMS[category], decode_args(self)) if self._is_htmx() else None
        if form is None:
            self.render_error(429, "Muitas requisições", _MSG_RATE)
            return
        try:
            ctx, access, action = await self._resolve(guild_id, category, fresh=False)
        except GuildAccessDenied:
            self._not_found()
            return
        state = await self.service.read(form, ctx, action, message=_MSG_RATE, kind=KIND_ERROR)
        self.set_status(429)
        self._render_fragment(state, access, ctx)

    async def _respond(
        self, category: str, access: GuildAccess, ctx: FormContext, action: str, form: SettingsForm, outcome: Outcome
    ) -> None:
        self.set_status(outcome.status)
        if self._is_htmx():
            self._render_fragment(outcome.form, access, ctx)
            return
        states = {}
        for other in CATEGORY_FORMS[category]:
            states[other.key] = outcome.form if other is form else await self.service.read(other, ctx, action)
        self._render_full(category, access, ctx, states)

    def _namespace(self, states: dict[str, Formulario], access: GuildAccess, ctx: FormContext) -> dict[str, Any]:
        extras: dict[str, Any] = {}
        for state in states.values():
            extras.update(state.extras)
        namespace = {
            **extras,
            "acesso": access,
            "inicial": initial_of(access.name),
            "bot_atual": self._bot_ref(access, ctx),
        }
        if "links" in states:
            namespace["exportar_url"] = f"{SERVERS_PATH}/{access.guild_id}/links/exportar"
        return namespace

    @staticmethod
    def _bot_ref(access: GuildAccess, ctx: FormContext) -> Optional[BotRef]:
        return next((bot for bot in access.bots if bot.id == ctx.bot_id), None)

    def _aliases(self, states: dict[str, Formulario]) -> dict[str, Formulario]:
        return {state.alias: state for state in states.values() if state.alias}

    def _render_fragment(self, state: Formulario, access: GuildAccess, ctx: FormContext) -> None:
        namespace = self._namespace({state.key: state}, access, ctx)
        self.render_page(state.template, form=state, **self._aliases({state.key: state}), **namespace)

    def _render_full(self, category: str, access: GuildAccess, ctx: FormContext, states: dict[str, Formulario]) -> None:
        namespace = self._namespace(states, access, ctx)
        per_bot = category in PER_BOT_CATEGORIES
        if not per_bot:
            namespace["bot_atual"] = None
        self.render_page(
            f"config_{category}.html",
            forms=states,
            mostrar_seletor=per_bot and len(access.bots) >= 2,
            **self._aliases(states),
            **namespace,
        )


class LinksExportHandler(SettingsHandler):
    """``GET /painel/servidores/<gid>/links/exportar``: as playlists do servidor no formato de importação, como anexo."""

    async def get(self, guild_id: str) -> None:  # type: ignore[override]
        try:
            ctx, _, _ = await self._resolve(guild_id, "links", fresh=False)
        except GuildAccessDenied:
            self._not_found()
            return
        try:
            content = await ctx.pool.servers.playlists.export_bytes(ctx.guild_id)
        except ServerConfigUnavailable as exc:
            self.render_error(503, UNAVAILABLE_TITLE, exc.text)
            return
        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.set_header("Content-Disposition", f'attachment; filename="playlists-{guild_id}.json"')
        self.set_header("Cache-Control", "no-store")
        self.finish(content)
