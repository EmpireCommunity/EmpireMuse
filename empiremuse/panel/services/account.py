"""Conta do usuário no painel: leitura e gravação das seções com versão otimista, lock por usuário e auditoria.

Dados só por ``bot.get_global_data/update_global_data(user_id, db_name=DBModel.users)``, sempre do usuário da sessão.
Como em ``panel/services/service.py``: o documento do cache é só leitura (a gravação usa uma cópia profunda e mexe só
nos campos da seção; ``ver`` fica intacto) e a auditoria registra usuário, ação e NOMES de campos — nunca valores,
URLs, nomes de favoritos, token ou sessionkey.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, NamedTuple, Optional

from empiremuse.panel.services.account_base import ACCOUNT_PATH, AccountContext, AccountSection, Parsed
from empiremuse.panel.services.account_links import FavoritesSection, IntegrationsSection
from empiremuse.panel.services.account_misc import LastFmSection, PrefixSection, RpcTokenSection
from empiremuse.panel.services.base import (
    CONFLICT_TEXT,
    HEX16,
    INVALID_TEXT,
    KIND_ERROR,
    KIND_OK,
    SAVED_TEXT,
    VERSION_TEXT,
    Args,
    DBModel,
    Formulario,
    first,
    version_of,
)
from empiremuse.panel.services.service import STATUS_CONFLICT, STATUS_INVALID, STATUS_OK, SettingsService
from empiremuse.utils.user_locks import USERS_SCOPE

logger = logging.getLogger(__name__)

USERS_LOCK_SCOPE = USERS_SCOPE


class AccountOutcome(NamedTuple):
    status: int
    form: Formulario


class AccountService:
    def __init__(self, settings_service: SettingsService) -> None:
        self._locks = settings_service
        self.sections: tuple[AccountSection, ...] = (
            PrefixSection(),
            FavoritesSection(),
            IntegrationsSection(),
            LastFmSection(),
            RpcTokenSection(),
        )
        self._by_action = {action: section for section in self.sections for action in section.actions}

    def section_for(self, action: Optional[str]) -> Optional[AccountSection]:
        return self._by_action.get(action or "")

    def section(self, key: str) -> AccountSection:
        return next(section for section in self.sections if section.key == key)

    # ------------------------------------------------------------------ leitura

    @staticmethod
    async def load(ctx: AccountContext) -> dict:
        """Documento do cache do banco: somente leitura (nunca mutar nem regravar este objeto)."""
        return await ctx.bot.get_global_data(ctx.user_id, db_name=DBModel.users)

    async def build(
        self,
        section: AccountSection,
        ctx: AccountContext,
        doc: dict,
        *,
        echo: Optional[dict[str, Any]] = None,
        errors: Optional[dict[str, str]] = None,
        message: Optional[str] = None,
        kind: Optional[str] = None,
        shown: Optional[dict[str, Any]] = None,
    ) -> Formulario:
        extras = {**await section.extras(ctx, doc), **(shown or {})}
        return Formulario(
            key=section.key,
            form_id=section.form_id,
            template=section.template,
            acao=ACCOUNT_PATH,
            versao=version_of(section.version_fields(doc)),
            valores={**section.defaults(doc), **(echo or {})},
            erros=errors or {},
            mensagem=message,
            mensagem_tipo=kind,
            extras=extras,
        )

    async def read_all(self, ctx: AccountContext) -> dict[str, Formulario]:
        doc = await self.load(ctx)
        return {section.key: await self.build(section, ctx, doc) for section in self.sections}

    async def read(
        self, section: AccountSection, ctx: AccountContext, *, message: Optional[str] = None, kind: Optional[str] = None
    ) -> Formulario:
        return await self.build(section, ctx, await self.load(ctx), message=message, kind=kind)

    # ------------------------------------------------------------------ escrita

    async def submit(self, section: AccountSection, ctx: AccountContext, action: str, args: Args) -> AccountOutcome:
        async with self._locks.lock(USERS_LOCK_SCOPE, ctx.user_id):
            doc = await self.load(ctx)
            refusal = await section.guard(ctx, doc, action)
            if refusal is not None:
                return await self._fail(refusal.status, section, ctx, doc, message=refusal.message)
            sent = first(args, "versao")
            if sent is None or not HEX16.fullmatch(sent):
                return await self._fail(STATUS_INVALID, section, ctx, doc, message=VERSION_TEXT)
            if sent != version_of(section.version_fields(doc)):
                return await self._fail(STATUS_CONFLICT, section, ctx, doc, message=CONFLICT_TEXT)
            parsed = await section.parse(ctx, doc, action, args)
            if parsed.errors:
                return await self._invalid(section, ctx, doc, parsed)
            return await self._save(section, ctx, doc, action, parsed)

    async def _fail(
        self, status: int, section: AccountSection, ctx: AccountContext, doc: dict, *, message: str
    ) -> AccountOutcome:
        return AccountOutcome(status, await self.build(section, ctx, doc, message=message, kind=KIND_ERROR))

    async def _invalid(self, section: AccountSection, ctx: AccountContext, doc: dict, parsed: Parsed) -> AccountOutcome:
        message = parsed.errors.get("acao") or INVALID_TEXT
        form = await self.build(
            section, ctx, doc, echo=parsed.echo, errors=parsed.errors, message=message, kind=KIND_ERROR
        )
        return AccountOutcome(STATUS_INVALID, form)

    async def _save(
        self, section: AccountSection, ctx: AccountContext, doc: dict, action: str, parsed: Parsed
    ) -> AccountOutcome:
        current = section.values(doc)
        changed = tuple(name for name, value in parsed.new.items() if current.get(name) != value)
        if not changed:
            form = await self.build(section, ctx, doc, message=SAVED_TEXT, kind=KIND_OK)
            return AccountOutcome(STATUS_OK, form)
        new_doc = copy.deepcopy(doc)
        section.apply(new_doc, parsed.new)
        await self._run_before_store(section, ctx, doc)
        await ctx.bot.update_global_data(ctx.user_id, new_doc, db_name=DBModel.users)
        # Só nomes dos campos: valores (prefixo, URLs, nomes, token) nunca vão para o log.
        logger.info("Conta alterada: usuario=%s acao=%s campos=%s", ctx.user_id, action, ",".join(changed))
        await self._run_effects(section, ctx, parsed.new)
        form = await self.build(section, ctx, new_doc, message=SAVED_TEXT, kind=KIND_OK, shown=parsed.shown)
        return AccountOutcome(STATUS_OK, form)

    @staticmethod
    async def _run_before_store(section: AccountSection, ctx: AccountContext, doc: dict) -> None:
        try:
            await section.before_store(ctx, doc)
        except Exception as exc:  # o efeito prévio não impede a gravação; só o tipo vai ao log
            logger.warning("Falha no efeito prévio da conta (%s: %s)", section.key, type(exc).__name__)

    @staticmethod
    async def _run_effects(section: AccountSection, ctx: AccountContext, new: dict[str, Any]) -> None:
        try:
            await section.effects(ctx, new)
        except Exception as exc:  # o dado já foi gravado: falha de efeito não derruba a resposta
            logger.warning("Falha ao aplicar efeitos da conta (%s: %s)", section.key, type(exc).__name__)
