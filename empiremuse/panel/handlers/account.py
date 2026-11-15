"""Conta do usuário: ``GET/POST /painel/conta`` e ``GET /painel/conta/favoritos/exportar``.

Só HTTP: regras e gravação ficam em ``panel/services/account*.py``. Os dados são sempre os do usuário da sessão (um
``user_id`` vindo do formulário ou da URL é ignorado) e o bot de entrada é o primeiro bot pronto do pool.
"""

from __future__ import annotations

import json
from typing import Optional

from empiremuse.panel.handlers.servers import MAX_BODY_BYTES, AuthenticatedHandler, decode_args
from empiremuse.panel.services.account import AccountOutcome, AccountService, AccountSessionGone
from empiremuse.panel.services.account_base import ACTION_UNKNOWN_TEXT, AccountContext, AccountSection
from empiremuse.panel.services.base import KIND_ERROR

PAGE_TEMPLATE = "conta.html"
UNAVAILABLE_TITLE = "Painel indisponível no momento"
UNAVAILABLE_MESSAGE = "Painel indisponível no momento. Tente novamente em instantes."
_MSG_RATE = "Muitas alterações em pouco tempo. Aguarde um minuto e tente novamente."
_TOO_LARGE = ("Envio grande demais", "Os dados enviados excedem o limite permitido.")
_UNKNOWN_ACTION = ("Dados inválidos", ACTION_UNKNOWN_TEXT)


class AccountBaseHandler(AuthenticatedHandler):
    @property
    def accounts(self) -> AccountService:
        return self.application.settings["account_service"]

    def _context(self) -> Optional[AccountContext]:
        pool = self.application.settings["pool"]
        bot = next((candidate for candidate in pool.bots if candidate.is_ready()), None)
        if bot is None:
            return None
        return AccountContext(pool=pool, bot=bot, user_id=int(self.session.user.id))

    def _unavailable(self) -> None:
        self._reject_plain(503, UNAVAILABLE_TITLE, UNAVAILABLE_MESSAGE)


class AccountHandler(AccountBaseHandler):
    """``GET`` mostra as seções; ``POST`` grava uma delas (``acao`` escolhe qual)."""

    async def get(self) -> None:  # type: ignore[override]
        ctx = self._context()
        if ctx is None:
            self._unavailable()
            return
        self._render_page(200, await self.accounts.read_all(ctx))

    async def post(self) -> None:  # type: ignore[override]
        if len(self.request.body) > MAX_BODY_BYTES:
            self._reject_plain(413, *_TOO_LARGE)
            return
        ctx = self._context()
        if ctx is None:
            self._unavailable()
            return
        args = decode_args(self)
        action = (args.get("acao") or [""])[0]
        section = self.accounts.section_for(action)
        if not self._write_allowed():
            await self._rate_limited(ctx, section)
            return
        if section is None:
            self._reject_plain(422, *_UNKNOWN_ACTION)
            return
        # a sessão é reconferida sob o lock do usuário: o apagamento de dados a destrói e a escrita enfileirada não grava
        session_id, store = self.session_id, self.application.settings["session_store"]
        try:
            outcome = await self.accounts.submit(
                section, ctx, action, args, alive=lambda: store.get(session_id) is not None
            )
        except AccountSessionGone:
            self.forget_session()
            self.render_error(401, "Sessão encerrada", "Sua sessão foi encerrada. Entre novamente para usar sua conta.")
            return
        await self._respond(ctx, section, outcome)

    async def _rate_limited(self, ctx: AccountContext, section: Optional[AccountSection]) -> None:
        self.set_header("Retry-After", "60")
        if section is None or not self._is_htmx():
            self.render_error(429, "Muitas requisições", _MSG_RATE)
            return
        form = await self.accounts.read(section, ctx, message=_MSG_RATE, kind=KIND_ERROR)
        self.set_status(429)
        self.render_page(form.template, estado=form)

    async def _respond(self, ctx: AccountContext, section: AccountSection, outcome: AccountOutcome) -> None:
        if self._is_htmx():
            self.set_status(outcome.status)
            self.render_page(outcome.form.template, estado=outcome.form)
            return
        states = await self.accounts.read_all(ctx)
        states[section.key] = outcome.form
        self._render_page(outcome.status, states)

    def _render_page(self, status: int, states: dict) -> None:
        self.set_status(status)
        self.render_page(PAGE_TEMPLATE, **states)


class FavoritesExportHandler(AccountBaseHandler):
    """``GET /painel/conta/favoritos/exportar``: JSON no formato de importação, como anexo, só do usuário da sessão."""

    async def get(self) -> None:  # type: ignore[override]
        ctx = self._context()
        if ctx is None:
            self._unavailable()
            return
        doc = await self.accounts.load(ctx)
        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.set_header("Content-Disposition", 'attachment; filename="favoritos.json"')
        self.set_header("Cache-Control", "no-store")
        self.finish(json.dumps(doc.get("fav_links") or {}, ensure_ascii=False, indent=4))
