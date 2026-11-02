"""Leitura e gravação de formulários com controle otimista de versão, lock por escopo e auditoria sem valores."""

from __future__ import annotations

import copy
import logging
import weakref
from asyncio import Lock
from typing import Any, NamedTuple, Optional

from empiremuse.panel.services.base import (
    CONFLICT_TEXT,
    HEX16,
    INVALID_TEXT,
    KIND_ERROR,
    KIND_OK,
    SAVED_TEXT,
    VERSION_TEXT,
    Args,
    FormContext,
    Formulario,
    SettingsForm,
    first,
    version_of,
)

logger = logging.getLogger(__name__)

STATUS_OK = 200
STATUS_CONFLICT = 409
STATUS_INVALID = 422


class Outcome(NamedTuple):
    status: int
    form: Formulario


class SettingsService:
    """Regras de leitura/escrita dos formulários. Dados só via ``bot.get_data/update_data`` (e as versões globais)."""

    def __init__(self) -> None:
        self._locks: "weakref.WeakValueDictionary[tuple[str, int], Lock]" = weakref.WeakValueDictionary()

    def lock(self, scope: str, guild_id: int) -> Lock:
        """Lock do escopo ``(id do bot | \"global\", servidor)``, compartilhado com as tarefas da Jukebox."""
        return self._lock(scope, guild_id)

    def _lock(self, scope: str, guild_id: int) -> Lock:
        key = (scope, guild_id)
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = Lock()
        return lock

    def build(
        self,
        form: SettingsForm,
        ctx: FormContext,
        current: dict[str, Any],
        action: str,
        *,
        aux: Optional[dict[str, Any]] = None,
        values: Optional[dict[str, Any]] = None,
        errors: Optional[dict[str, str]] = None,
        message: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> Formulario:
        full = {**current, **(aux or {})}  # a versão só considera ``current``; ``aux`` é só para exibir
        shown = form.display(ctx, full) if values is None else {**form.display(ctx, full), **values}
        return Formulario(
            key=form.key,
            form_id=form.form_id,
            template=form.template,
            acao=action,
            versao=version_of(current),
            valores=shown,
            erros=errors or {},
            mensagem=message,
            mensagem_tipo=kind,
            extras=form.extras(ctx, full),
            alias=form.template_alias,
        )

    async def read(
        self,
        form: SettingsForm,
        ctx: FormContext,
        action: str,
        *,
        message: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> Formulario:
        doc = await form.load(ctx)
        return self.build(form, ctx, form.extract(doc), action, aux=form.aux(ctx, doc), message=message, kind=kind)

    async def submit(
        self, form: SettingsForm, ctx: FormContext, args: Args, action: str, *, audit_user: str, category: str
    ) -> Outcome:
        await form.guard(ctx, args)
        async with self._lock(form.scope(ctx), ctx.guild_id):
            outcome, job = await self._submit_locked(form, ctx, args, action)
        if job is not None:
            self._audit(form, ctx, category, audit_user, job[2])
            await self._run_effects(form, ctx, job[0], job[1])
        return outcome

    async def _submit_locked(self, form, ctx, args, action):
        doc = await form.load(ctx)
        current = form.extract(doc)
        aux = form.aux(ctx, doc)
        sent = first(args, "versao")
        if sent is None or not HEX16.fullmatch(sent):
            return Outcome(
                STATUS_INVALID, self.build(form, ctx, current, action, aux=aux, message=VERSION_TEXT, kind=KIND_ERROR)
            ), None
        if sent != version_of(current):
            return Outcome(
                STATUS_CONFLICT, self.build(form, ctx, current, action, aux=aux, message=CONFLICT_TEXT, kind=KIND_ERROR)
            ), None
        parsed = form.parse(ctx, {**current, **aux}, args)
        if parsed.errors:
            return Outcome(
                STATUS_INVALID,
                self.build(
                    form,
                    ctx,
                    current,
                    action,
                    aux=aux,
                    values=parsed.echo,
                    errors=parsed.errors,
                    message=INVALID_TEXT,
                    kind=KIND_ERROR,
                ),
            ), None
        changed = tuple(name for name in current if parsed.new.get(name) != current[name])
        if not changed:
            return Outcome(
                STATUS_OK, self.build(form, ctx, current, action, aux=aux, message=SAVED_TEXT, kind=KIND_OK)
            ), None
        new_doc = copy.deepcopy(doc)
        form.apply(new_doc, parsed.new)
        await form.store(ctx, new_doc, doc)
        saved = form.extract(new_doc)
        outcome = Outcome(
            STATUS_OK,
            self.build(form, ctx, saved, action, aux=form.aux(ctx, new_doc), message=SAVED_TEXT, kind=KIND_OK),
        )
        return outcome, (current, saved, changed)

    @staticmethod
    def _audit(form: SettingsForm, ctx: FormContext, category: str, user_id: str, changed: tuple[str, ...]) -> None:
        # Só nomes dos campos: valores (prefixo, texto de status, ids) nunca vão para o log.
        logger.info(
            "Configuração alterada: usuario=%s servidor=%s escopo=%s categoria=%s campos=%s",
            user_id,
            ctx.guild_id,
            form.scope(ctx),
            category,
            ",".join(changed),
        )

    @staticmethod
    async def _run_effects(form: SettingsForm, ctx: FormContext, current: dict, new: dict) -> None:
        try:
            await form.effects(ctx, current, new)
        except Exception as exc:  # o dado já foi gravado: falha de efeito não derruba a resposta
            logger.warning("Falha ao aplicar efeitos da configuração %s (%s)", form.key, type(exc).__name__)
