"""Página Rádios (item 8): ``GET /painel/servidores/<gid>/radios`` lista as rádios do servidor;
``POST /painel/servidores/<gid>/radios/<bot:guild>`` grava as regras do chat da call (só ``chat.*``).

Só HTTP: autorização da R-6, allowlist, validação e compare-and-set ficam em ``panel/services/radio.py``.
"""

from __future__ import annotations

from typing import Any, Optional

import tornado.web

from empiremuse.panel.authz import GuildAccess, GuildAccessDenied
from empiremuse.panel.handlers.servers import SERVERS_PATH, initial_of
from empiremuse.panel.handlers.settings import (
    MAX_BODY_BYTES,
    RADIOS_CATEGORY,
    SettingsHandler,
    decode_args,
)
from empiremuse.panel.services.base import KIND_ERROR, FormContext, Formulario, panel_translator
from empiremuse.panel.services.radio import (
    FORBIDDEN_KEY,
    MAX_RADIOS,
    STATUS_FORBIDDEN,
    RadioPage,
    belongs_to,
    can_write,
    radio_id_of,
)
from empiremuse.panel.services.service import Outcome
from empiremuse.utils.usage import record_panel_action

PAGE_TEMPLATE = "config_radios.html"
FORM_TEMPLATE = "form_radio_chat.html"
_MSG_RATE = "Muitas alterações em pouco tempo. Aguarde um minuto e tente novamente."
_MSG_TOO_LARGE = ("Envio grande demais", "Os dados enviados excedem o limite permitido.")
_RATE_TITLE = "Muitas requisições"


class RadiosHandler(SettingsHandler):
    """``GET /painel/servidores/<gid>/radios``."""

    page = RadioPage()

    async def get(self, guild_id: str) -> None:  # type: ignore[override]
        try:
            ctx, access, action = await self._resolve(guild_id, RADIOS_CATEGORY, fresh=False)
        except GuildAccessDenied:
            self._not_found()
            return
        await self._render_page(200, access, ctx, action)

    async def post(self, guild_id: str) -> None:  # type: ignore[override]
        """A lista não recebe POST: cada rádio tem a própria URL."""
        raise tornado.web.HTTPError(405)

    async def _render_page(
        self, status: int, access: GuildAccess, ctx: FormContext, action: str, *, form: Optional[Formulario] = None
    ) -> None:
        tr = panel_translator(ctx.pool)
        premium = await ctx.pool.radio.premium.get(ctx.guild_id)
        editable = can_write(premium)
        rows = await self.page.rows(ctx, action, editable, tr)
        if form is not None:  # o resultado do POST entra no lugar do formulário daquela rádio
            rows = tuple(row._replace(form=form) if row.id == form.radio_id else row for row in rows)
        self.set_status(status)
        self.render_page(
            PAGE_TEMPLATE,
            **self._common(access),
            mostrar_seletor=False,
            premium=premium,
            pode_escrever=editable,
            radios=rows,
            max_radios=MAX_RADIOS,
        )

    @staticmethod
    def _common(access: GuildAccess) -> dict[str, Any]:
        return {"acesso": access, "inicial": initial_of(access.name), "bot_atual": None}


class RadioChatHandler(RadiosHandler):
    """``POST /painel/servidores/<gid>/radios/<bot:guild>``: regras do chat de uma rádio (R1 §3, R-6)."""

    async def get(self, guild_id: str, radio_id: str) -> None:  # type: ignore[override]
        self.redirect(f"{SERVERS_PATH}/{guild_id}/{RADIOS_CATEGORY}")

    async def post(self, guild_id: str, radio_id: str) -> None:  # type: ignore[override]
        if len(self.request.body) > MAX_BODY_BYTES:
            self._reject_plain(413, *_MSG_TOO_LARGE)
            return
        if not self._write_allowed():
            self.set_header("Retry-After", "60")
            self._reject_plain(429, _RATE_TITLE, _MSG_RATE)
            return
        try:
            ctx, access, action = await self._resolve(guild_id, RADIOS_CATEGORY, fresh=True)
        except GuildAccessDenied:
            self._not_found()
            return
        wanted = radio_id_of(ctx.guild_id, radio_id)
        radio = await ctx.pool.radio.store.get(wanted) if wanted is not None else None
        if radio is None or not belongs_to(radio, ctx.guild_id):  # rádio de outro servidor: 404, como um id inexistente
            self._not_found()
            return
        tr = panel_translator(ctx.pool)
        form_action = f"{action}/{radio.id}"
        premium = await ctx.pool.radio.premium.get(ctx.guild_id)
        if not can_write(premium) or not await self.page.can_edit(ctx, radio):
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
