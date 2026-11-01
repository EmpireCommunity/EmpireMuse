"""Página Rádios (item 8): ``GET /painel/servidores/<gid>/radios`` lista as rádios do servidor;
``POST /painel/servidores/<gid>/radios/<bot:guild>`` grava as regras do chat da call (só ``chat.*``).

Só HTTP: autorização da R-6, allowlist, validação e compare-and-set ficam em ``panel/services/radio.py``. Banco das
rádios fora do ar (``RadioUnavailable``) responde 503 com mensagem própria, como as outras páginas.
"""

from __future__ import annotations

from typing import Any, Optional

import tornado.web

from empiremuse.panel.authz import GuildAccess, GuildAccessDenied
from empiremuse.panel.handlers.servers import SERVERS_PATH, initial_of
from empiremuse.panel.handlers.settings import (
    MAX_BODY_BYTES,
    RADIOS_CATEGORY,
    UNAVAILABLE_TITLE,
    SettingsHandler,
    decode_args,
)
from empiremuse.panel.services.base import KIND_ERROR, FormContext, Formulario, panel_translator
from empiremuse.panel.services.radio import (
    FORBIDDEN_KEY,
    STATUS_FORBIDDEN,
    RadioPage,
    belongs_to,
    can_write,
    radio_id_of,
)
from empiremuse.panel.services.radio_live import SEARCH_FIELD, SEARCH_RAW_LIMIT, LiveOutcome, LiveView
from empiremuse.panel.services.service import Outcome
from empiremuse.rules.radio import KIND_LIVE, MAX_PER_GUILD
from empiremuse.utils.radio.premium import RadioUnavailable
from empiremuse.utils.usage import record_panel_action

PAGE_TEMPLATE = "config_radios.html"
FORM_TEMPLATE = "form_radio_chat.html"
LIVE_TEMPLATE = "form_radio_reserva.html"
STATIONS_TEMPLATE = "_radio_estacoes.html"
UNAVAILABLE_KEY = "panel.radio.unavailable"
_MSG_RATE = "Muitas alterações em pouco tempo. Aguarde um minuto e tente novamente."
_MSG_TOO_LARGE = ("Envio grande demais", "Os dados enviados excedem o limite permitido.")
_RATE_TITLE = "Muitas requisições"


class RadiosHandler(SettingsHandler):
    """``GET /painel/servidores/<gid>/radios``."""

    page = RadioPage()

    async def get(self, guild_id: str) -> None:  # type: ignore[override]
        try:
            await self._get(guild_id)
        except RadioUnavailable:
            self._unavailable()

    async def post(self, guild_id: str) -> None:  # type: ignore[override]
        """A lista não recebe POST: cada rádio tem a própria URL."""
        raise tornado.web.HTTPError(405)

    async def _get(self, guild_id: str) -> None:
        try:
            ctx, access, action = await self._resolve(guild_id, RADIOS_CATEGORY, fresh=False)
        except GuildAccessDenied:
            self._not_found()
            return
        await self._render_page(200, access, ctx, action)

    def _unavailable(self) -> None:
        text = panel_translator(self.application.settings.get("pool"))(UNAVAILABLE_KEY)
        self.render_error(503, UNAVAILABLE_TITLE, text)

    async def _render_page(
        self,
        status: int,
        access: GuildAccess,
        ctx: FormContext,
        action: str,
        *,
        form: Optional[Formulario] = None,
        live: Optional[LiveView] = None,
    ) -> None:
        tr = panel_translator(ctx.pool)
        premium = await ctx.pool.radio.premium.get(ctx.guild_id)
        editable = can_write(premium)
        rows = await self.page.rows(ctx, action, editable, tr)
        if form is not None:  # o resultado do POST entra no lugar do formulário daquela rádio
            rows = tuple(row._replace(form=form) if row.id == form.radio_id else row for row in rows)
        if live is not None:
            rows = tuple(row._replace(ao_vivo=live) if row.id == live.radio_id else row for row in rows)
        self.set_status(status)
        self.render_page(
            PAGE_TEMPLATE,
            **self._common(access),
            mostrar_seletor=False,
            premium=premium,
            pode_escrever=editable,
            radios=rows,
            max_radios=MAX_PER_GUILD,
        )

    @staticmethod
    def _common(access: GuildAccess) -> dict[str, Any]:
        return {"acesso": access, "inicial": initial_of(access.name), "bot_atual": None}

    async def _radio_for_write(self, guild_id: str, radio_id: str, *, fresh: bool) -> Optional[tuple]:
        """``(ctx, access, action, radio, pode)`` ou ``None`` depois de responder 404; ``pode`` = premium e ``manage_messages``."""
        try:
            ctx, access, action = await self._resolve(guild_id, RADIOS_CATEGORY, fresh=fresh)
        except GuildAccessDenied:
            self._not_found()
            return None
        wanted = radio_id_of(ctx.guild_id, radio_id)
        radio = await ctx.pool.radio.store.get(wanted) if wanted is not None else None
        if radio is None or not belongs_to(radio, ctx.guild_id):  # rádio de outro servidor: 404, como um id inexistente
            self._not_found()
            return None
        premium = await ctx.pool.radio.premium.get(ctx.guild_id)
        allowed = can_write(premium) and await self.page.can_edit(ctx, radio)
        return ctx, access, action, radio, allowed


class RadioChatHandler(RadiosHandler):
    """``POST /painel/servidores/<gid>/radios/<bot:guild>``: regras do chat de uma rádio (R1 §3, R-6)."""

    async def get(self, guild_id: str, radio_id: str) -> None:  # type: ignore[override]
        self.redirect(f"{SERVERS_PATH}/{guild_id}/{RADIOS_CATEGORY}")

    async def post(self, guild_id: str, radio_id: str) -> None:  # type: ignore[override]
        try:
            await self._post(guild_id, radio_id)
        except RadioUnavailable:
            self._unavailable()

    async def _post(self, guild_id: str, radio_id: str) -> None:
        if len(self.request.body) > MAX_BODY_BYTES:
            self._reject_plain(413, *_MSG_TOO_LARGE)
            return
        if not self._write_allowed():
            self.set_header("Retry-After", "60")
            self._reject_plain(429, _RATE_TITLE, _MSG_RATE)
            return
        found = await self._radio_for_write(guild_id, radio_id, fresh=True)
        if found is None:
            return
        ctx, access, action, radio, allowed = found
        tr = panel_translator(ctx.pool)
        form_action = f"{action}/{radio.id}"
        if not allowed:
            form = self.page.form(radio, form_action, message=tr(FORBIDDEN_KEY), kind=KIND_ERROR)
            outcome = Outcome(STATUS_FORBIDDEN, form)
        else:
            outcome = await self.page.submit(ctx, radio, decode_args(self), form_action, tr)
        record_panel_action(ctx, f"{RADIOS_CATEGORY}/chat", outcome.status)
        if self._is_htmx():
            self.set_status(outcome.status)
            self.render_page(FORM_TEMPLATE, form=outcome.form, **self._common(access))
            return
        await self._render_page(outcome.status, access, ctx, action, form=outcome.form)


class RadioStationsHandler(RadiosHandler):
    """``GET /painel/servidores/<gid>/radios/<bot:guild>/estacoes?q=``: busca no diretório para o seletor da reserva (R3, R-7)."""

    async def get(self, guild_id: str, radio_id: str) -> None:  # type: ignore[override]
        try:
            await self._search(guild_id, radio_id)
        except RadioUnavailable:
            self._unavailable()

    async def post(self, *args: str) -> None:  # type: ignore[override]
        raise tornado.web.HTTPError(405)

    async def _search(self, guild_id: str, radio_id: str) -> None:
        if not self._write_allowed():  # cada busca gasta o bucket global do diretório: vale o limitador de escrita
            self.set_header("Retry-After", "60")
            self._reject_plain(429, _RATE_TITLE, _MSG_RATE)
            return
        found = await self._radio_for_write(guild_id, radio_id, fresh=False)
        if found is None:
            return
        ctx, access, action, radio, allowed = found
        if radio.kind != KIND_LIVE:
            self._not_found()
            return
        tr = panel_translator(ctx.pool)
        if not allowed:  # sem premium ou sem manage_messages: nenhuma chamada ao diretório (R-7)
            self._reject_plain(STATUS_FORBIDDEN, UNAVAILABLE_TITLE, tr(FORBIDDEN_KEY))
            return
        term = self.get_query_argument(SEARCH_FIELD, "", strip=False)[:SEARCH_RAW_LIMIT]
        results, hint = await self.page.live.search(ctx, radio, term, tr)
        record_panel_action(ctx, f"{RADIOS_CATEGORY}/estacoes", 200)
        view = await self.page.live.view(
            ctx, radio, f"{action}/{radio.id}", True, results=results, hint=hint, term=term
        )
        if self._is_htmx():
            self.render_page(STATIONS_TEMPLATE, form=view.form, **self._common(access))
            return
        await self._render_page(200, access, ctx, action, live=view)


class RadioReserveHandler(RadiosHandler):
    """``POST /painel/servidores/<gid>/radios/<bot:guild>/reserva``: a estação reserva de uma rádio ao vivo (R3, R-6)."""

    async def get(self, guild_id: str, radio_id: str) -> None:  # type: ignore[override]
        self.redirect(f"{SERVERS_PATH}/{guild_id}/{RADIOS_CATEGORY}")

    async def post(self, guild_id: str, radio_id: str) -> None:  # type: ignore[override]
        try:
            await self._post(guild_id, radio_id)
        except RadioUnavailable:
            self._unavailable()

    async def _post(self, guild_id: str, radio_id: str) -> None:
        if len(self.request.body) > MAX_BODY_BYTES:
            self._reject_plain(413, *_MSG_TOO_LARGE)
            return
        if not self._write_allowed():
            self.set_header("Retry-After", "60")
            self._reject_plain(429, _RATE_TITLE, _MSG_RATE)
            return
        found = await self._radio_for_write(guild_id, radio_id, fresh=True)
        if found is None:
            return
        ctx, access, action, radio, allowed = found
        tr = panel_translator(ctx.pool)
        radio_action = f"{action}/{radio.id}"
        if not allowed:
            view = await self.page.live.view(ctx, radio, radio_action, True, message=tr(FORBIDDEN_KEY), kind=KIND_ERROR)
            outcome = LiveOutcome(STATUS_FORBIDDEN, view)
        else:
            outcome = await self.page.live.set_reserve(ctx, radio, decode_args(self), radio_action, tr)
        record_panel_action(ctx, f"{RADIOS_CATEGORY}/reserva", outcome.status)
        self.set_status(outcome.status)
        if self._is_htmx():
            self.render_page(LIVE_TEMPLATE, vivo=outcome.view, **self._common(access))
            return
        await self._render_page(outcome.status, access, ctx, action, live=outcome.view)
