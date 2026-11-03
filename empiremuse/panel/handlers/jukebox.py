"""Categoria Jukebox: página, configurar/reiniciar (em dois passos) e acompanhamento das tarefas.

Só HTTP: validação, token e execução ficam em ``panel/services/jukebox.py`` e ``panel/services/tasks.py``. A Jukebox é
do servidor (seção ``jukebox`` do documento único): sem seletor de bot; quem executa no Discord é o bot do painel.
A rota antiga (``/canal``) só redireciona: GET dá 301 para ``/jukebox``; POST dá 404.
"""

from __future__ import annotations

from typing import Any, Optional

import tornado.web

from empiremuse.panel.authz import GuildAccess, GuildAccessDenied
from empiremuse.panel.handlers.servers import initial_of
from empiremuse.panel.handlers.settings import (
    JUKEBOX_CATEGORY,
    MAX_BODY_BYTES,
    UNAVAILABLE_TITLE,
    SettingsHandler,
    decode_args,
)
from empiremuse.panel.services.base import (
    CONFLICT_TEXT,
    HEX16,
    INVALID_TEXT,
    KIND_ERROR,
    Args,
    FormContext,
    Formulario,
    first,
)
from empiremuse.panel.services.jukebox import (
    MSG_BUSY,
    MSG_LIMIT,
    MSG_TOKEN,
    Check,
    Confirmation,
    JukeboxService,
    SetupConfirmation,
    TaskView,
)
from empiremuse.panel.services.jukebox_options import OPTIONS_ACTION, JukeboxOptionsForm
from empiremuse.panel.services.service import Outcome
from empiremuse.panel.services.tasks import Task, TaskBusy, TaskLimit, valid_task_id
from empiremuse.utils.server_jukebox import JukeboxConfig
from empiremuse.utils.servers import ServerConfigConflict, ServerConfigUnavailable
from empiremuse.utils.usage import record_panel_action

JUKEBOX_TEMPLATE = "bloco_jukebox.html"
TASK_TEMPLATE = "bloco_tarefa_jukebox.html"
PAGE_TEMPLATE = "config_jukebox.html"
OPTIONS_TEMPLATE = "form_jukebox_opcoes.html"
OPTIONS_FORM = JukeboxOptionsForm()  # opções A e B: formulário de servidor exibido nesta página, acao=opcoes
LEGACY_SLUG = "canal"  # a rota antiga da categoria, mantida só como redirecionamento
_MSG_RATE = "Muitas alterações em pouco tempo. Aguarde um minuto e tente novamente."
_MSG_ACTION = "Ação desconhecida."
_MSG_VERSION = "Não foi possível validar o formulário. Recarregue a página e tente novamente."
_KNOWN_ACTIONS = ("configurar", "confirmar_configurar", "reiniciar", "confirmar_reiniciar")
_DEFAULT_VALUES = {
    "modo": "existente",
    "canal": "",
    "titulo_forum": "",
    "tipo": "texto",
    "categoria": "",
}


def _audited_action(args: Args) -> str:
    """A ação do formulário como rótulo fixo para o registro de uso (o valor enviado pelo cliente nunca é gravado)."""
    action = first(args, "acao")
    return action if action in _KNOWN_ACTIONS else "desconhecida"


class JukeboxHandler(SettingsHandler):
    """``GET/POST /painel/servidores/<gid>/jukebox``."""

    @property
    def jukebox(self) -> JukeboxService:
        return self.application.settings["jukebox_service"]

    async def _config(self, ctx: FormContext) -> JukeboxConfig:
        return await ctx.pool.servers.jukebox.get(ctx.guild_id)

    def _own_active_task(self, ctx: FormContext) -> Optional[Task]:
        """Tarefa ativa do servidor só se for do próprio usuário (a de outro daria 404 no polling)."""
        active = self.jukebox.runner.active_for_guild(ctx.guild_id)
        return active if active is not None and active.owner == ctx.user_id else None

    # ------------------------------------------------------------------ GET

    async def get(self, guild_id: str) -> None:  # type: ignore[override]
        try:
            await self._get(guild_id)
        except ServerConfigUnavailable as exc:
            self.render_error(503, UNAVAILABLE_TITLE, exc.text)

    async def _get(self, guild_id: str) -> None:
        try:
            ctx, access, action = await self._resolve(guild_id, JUKEBOX_CATEGORY, fresh=False)
        except GuildAccessDenied:
            self._not_found()
            return
        config = await self._config(ctx)
        await self._render(200, access, ctx, self._namespace_for(ctx, config, action, task=self._own_active_task(ctx)))

    # ------------------------------------------------------------------ POST

    async def post(self, guild_id: str) -> None:  # type: ignore[override]
        try:
            await self._post(guild_id)
        except ServerConfigUnavailable as exc:
            self.render_error(503, UNAVAILABLE_TITLE, exc.text)

    async def _post(self, guild_id: str) -> None:
        if len(self.request.body) > MAX_BODY_BYTES:
            self._reject_plain(413, "Envio grande demais", "Os dados enviados excedem o limite permitido.")
            return
        if not self._write_allowed():
            await self._rate_limited_jukebox(guild_id)
            return
        try:
            ctx, access, action = await self._resolve(guild_id, JUKEBOX_CATEGORY, fresh=True)
        except GuildAccessDenied:
            self._not_found()
            return
        args = decode_args(self)
        if first(args, "acao") == OPTIONS_ACTION:
            await self._post_options(ctx, access, action, args)
            return
        if self.jukebox.runner.active_for_guild(ctx.guild_id) is not None:
            # Antes do lock: a tarefa em andamento o segura até terminar e este POST não deve esperar por ela.
            config = await self._config(ctx)
            await self._render(
                409,
                access,
                ctx,
                self._namespace_for(ctx, config, action, message=MSG_BUSY, task=self._own_active_task(ctx)),
            )
            return
        try:
            async with self.application.settings["settings_service"].lock(self.jukebox.SCOPE, ctx.guild_id):
                status, namespace = await self._dispatch(ctx, action, args)
        except ServerConfigConflict:  # outro escritor venceu a corrida no store: o usuário recarrega
            config = await self._config(ctx)
            status, namespace = 409, self._namespace_for(ctx, config, action, message=CONFLICT_TEXT)
        record_panel_action(ctx, f"{JUKEBOX_CATEGORY}/{_audited_action(args)}", status)
        await self._render(status, access, ctx, namespace)

    # ------------------------------------------------------------------ opções A e B (acao=opcoes)

    async def _post_options(self, ctx: FormContext, access: GuildAccess, action: str, args: Args) -> None:
        """As duas chaves pelo fluxo genérico dos formulários: sem tarefa e sem efeito no Discord."""
        if self.jukebox.runner.active_for_guild(ctx.guild_id) is not None:  # a tarefa segura a trava do servidor
            state = await self.service.read(OPTIONS_FORM, ctx, action, message=MSG_BUSY, kind=KIND_ERROR)
            outcome = Outcome(409, state)
        else:
            try:
                outcome = await self.service.submit(
                    OPTIONS_FORM, ctx, args, action, audit_user=ctx.user_id, category=JUKEBOX_CATEGORY
                )
            except ServerConfigConflict:
                state = await self.service.read(OPTIONS_FORM, ctx, action, message=CONFLICT_TEXT, kind=KIND_ERROR)
                outcome = Outcome(409, state)
        record_panel_action(ctx, f"{JUKEBOX_CATEGORY}/{OPTIONS_ACTION}", outcome.status)
        self.set_status(outcome.status)
        if self._is_htmx():
            self._render_options(access, outcome.form)
            return
        config = await self._config(ctx)
        await self._render(outcome.status, access, ctx, self._namespace_for(ctx, config, action), opcoes=outcome.form)

    def _render_options(self, access: GuildAccess, form: Formulario) -> None:
        self.render_page(OPTIONS_TEMPLATE, form=form, acesso=access, inicial=initial_of(access.name), bot_atual=None)

    async def _dispatch(self, ctx: FormContext, action: str, args: Args) -> tuple[int, dict[str, Any]]:
        config = await self._config(ctx)
        sent = first(args, "versao")
        if sent is None or not HEX16.fullmatch(sent):
            return 422, self._namespace_for(ctx, config, action, message=_MSG_VERSION)
        if sent != self.jukebox.version(config):
            return 409, self._namespace_for(ctx, config, action, message=CONFLICT_TEXT)
        handler = {
            "configurar": self._configure,
            "confirmar_configurar": self._configure_confirm,
            "reiniciar": self._reset_step_one,
            "confirmar_reiniciar": self._reset_confirm,
        }.get(first(args, "acao") or "")
        if handler is None:
            return 422, self._namespace_for(ctx, config, action, message=_MSG_ACTION)
        return await handler(ctx, config, action, args)

    async def _configure(
        self, ctx: FormContext, config: JukeboxConfig, action: str, args: Args
    ) -> tuple[int, dict[str, Any]]:
        check = self.jukebox.validate_setup(ctx, args)
        if check.errors or check.plan is None:
            return 422, self._invalid(ctx, config, action, check)
        if check.plan.purges:  # canal existente: as mensagens dele vão embora, o usuário confirma antes
            confirmation = self.jukebox.setup_confirmation(ctx, check.plan, check.echo)
            return 200, self._namespace_for(ctx, config, action, values=check.echo, setup_confirmation=confirmation)
        return self._start(ctx, config, action, lambda: self.jukebox.start_setup(ctx, check.plan))

    async def _configure_confirm(
        self, ctx: FormContext, config: JukeboxConfig, action: str, args: Args
    ) -> tuple[int, dict[str, Any]]:
        check = self.jukebox.validate_setup(ctx, args)  # revalida: o mundo pode ter mudado entre os passos
        if check.errors or check.plan is None:
            return 422, self._invalid(ctx, config, action, check)
        if not check.plan.purges:
            return 422, self._namespace_for(ctx, config, action, values=check.echo, message=_MSG_ACTION)
        status = self.jukebox.check_setup_confirmation(ctx, check.plan, args)
        if status == "outro":
            return 409, self._namespace_for(ctx, config, action, values=check.echo, message=CONFLICT_TEXT)
        if status == "invalido":
            return 422, self._namespace_for(ctx, config, action, values=check.echo, message=MSG_TOKEN)
        result = self._start(ctx, config, action, lambda: self.jukebox.start_setup(ctx, check.plan))
        if result[0] == 202:
            self.jukebox.tokens.consume(first(args, "token") or "")
        return result

    async def _reset_step_one(
        self, ctx: FormContext, config: JukeboxConfig, action: str, args: Args
    ) -> tuple[int, dict[str, Any]]:
        error = self.jukebox.check_reset(ctx, config)
        if error:
            return 422, self._namespace_for(ctx, config, action, message=error, errors={"acao": error})
        return 200, self._namespace_for(ctx, config, action, confirmation=self.jukebox.confirmation(ctx, config))

    async def _reset_confirm(
        self, ctx: FormContext, config: JukeboxConfig, action: str, args: Args
    ) -> tuple[int, dict[str, Any]]:
        error = self.jukebox.check_reset(ctx, config)
        if error:
            return 422, self._namespace_for(ctx, config, action, message=error, errors={"acao": error})
        status = self.jukebox.check_confirmation(ctx, config, args)
        if status == "outro":
            return 409, self._namespace_for(ctx, config, action, message=CONFLICT_TEXT)
        if status == "invalido":
            return 422, self._namespace_for(ctx, config, action, message=MSG_TOKEN)
        result = self._start(ctx, config, action, lambda: self.jukebox.start_reset(ctx))
        if result[0] == 202:
            self.jukebox.tokens.consume(first(args, "token") or "")
        return result

    def _start(self, ctx: FormContext, config: JukeboxConfig, action: str, submit) -> tuple[int, dict[str, Any]]:
        try:
            task = submit()
        except TaskBusy:
            return 409, self._namespace_for(ctx, config, action, message=MSG_BUSY, task=self._own_active_task(ctx))
        except TaskLimit:
            return 429, self._namespace_for(ctx, config, action, message=MSG_LIMIT)
        return 202, self._namespace_for(ctx, config, action, task=task)

    def _invalid(self, ctx: FormContext, config: JukeboxConfig, action: str, check: Check) -> dict[str, Any]:
        return self._namespace_for(ctx, config, action, values=check.echo, errors=check.errors, missing=check.missing)

    async def _rate_limited_jukebox(self, guild_id: str) -> None:
        self.set_header("Retry-After", "60")
        if not self._is_htmx():
            self.render_error(429, "Muitas requisições", _MSG_RATE)
            return
        try:
            ctx, access, action = await self._resolve(guild_id, JUKEBOX_CATEGORY, fresh=False)
        except GuildAccessDenied:
            self._not_found()
            return
        if first(decode_args(self), "acao") == OPTIONS_ACTION:
            self.set_status(429)
            self._render_options(
                access, await self.service.read(OPTIONS_FORM, ctx, action, message=_MSG_RATE, kind=KIND_ERROR)
            )
            return
        config = await self._config(ctx)
        await self._render(429, access, ctx, self._namespace_for(ctx, config, action, message=_MSG_RATE))

    # ------------------------------------------------------------------ montagem do que vai ao template

    def _namespace_for(
        self,
        ctx: FormContext,
        config: JukeboxConfig,
        action: str,
        *,
        values: Optional[dict] = None,
        errors: Optional[dict] = None,
        message: Optional[str] = None,
        task: Optional[Task] = None,
        confirmation: Optional[Confirmation] = None,
        setup_confirmation: Optional[SetupConfirmation] = None,
        missing: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        groups, types, categories = self.jukebox.choices(ctx)
        text = message or (INVALID_TEXT if errors else None)
        form = Formulario(
            key=JUKEBOX_CATEGORY,
            form_id="jukebox-conteudo",
            template=JUKEBOX_TEMPLATE,
            acao=action,
            versao=self.jukebox.version(config),
            valores={**_DEFAULT_VALUES, **(values or {})},
            erros=errors or {},
            mensagem=text,
            mensagem_tipo=KIND_ERROR if text else None,
        )
        view: Optional[TaskView] = self.jukebox.task_view(task, ctx.guild_id) if task is not None else None
        return {
            "jukebox_atual": self.jukebox.current(ctx, config),
            "formulario": form,
            "canais": groups,
            "tipos": types,
            "categorias_discord": categories,
            "permissoes_faltando": tuple(missing),
            "tarefa": view,
            "confirmacao": confirmation,
            "confirmacao_configurar": setup_confirmation,
        }

    async def _render(
        self,
        status: int,
        access: GuildAccess,
        ctx: FormContext,
        namespace: dict[str, Any],
        *,
        opcoes: Optional[Formulario] = None,
    ) -> None:
        self.set_status(status)
        if opcoes is None:  # o bloco das opções mora no fragmento da Jukebox: vai em toda renderização
            opcoes = await self.service.read(OPTIONS_FORM, ctx, namespace["formulario"].acao)
        common = {"acesso": access, "inicial": initial_of(access.name), "bot_atual": None, "opcoes": opcoes}
        if self._is_htmx():
            self.render_page(JUKEBOX_TEMPLATE, **common, **namespace)
            return
        self.render_page(PAGE_TEMPLATE, mostrar_seletor=False, **common, **namespace)


class JukeboxTaskHandler(JukeboxHandler):
    """``GET /painel/servidores/<gid>/jukebox/tarefas/<id>``: estado da tarefa (só o dono, com acesso ainda válido)."""

    async def get(self, guild_id: str, task_id: str) -> None:  # type: ignore[override]
        task = self.jukebox.runner.get(task_id) if valid_task_id(task_id) else None
        if task is None or task.owner != self.session.user.id or str(task.guild_id) != guild_id:
            self._not_found()
            return
        try:
            ctx, access, action = await self._resolve(guild_id, JUKEBOX_CATEGORY, fresh=False, bot_id=task.bot_id)
        except GuildAccessDenied:
            self._not_found()
            return
        view = self.jukebox.task_view(task, ctx.guild_id)
        if self._is_htmx():
            self.render_page(TASK_TEMPLATE, tarefa=view)
            return
        try:
            config = await self._config(ctx)
        except ServerConfigUnavailable as exc:
            self.render_error(503, UNAVAILABLE_TITLE, exc.text)
            return
        await self._render(200, access, ctx, self._namespace_for(ctx, config, action, task=task))

    async def post(self, *args: str) -> None:  # type: ignore[override]
        """A URL de acompanhamento só aceita GET (o POST herdado tem outra assinatura e daria 500)."""
        raise tornado.web.HTTPError(405)


class LegacyChannelHandler(tornado.web.RequestHandler):
    """A rota antiga da categoria: GET redireciona (301) para ``/jukebox`` com o mesmo resto do caminho; POST é 404.

    Só reescreve o caminho (o id do servidor vem validado pela rota, dígitos): nada do que o cliente manda vai ao
    ``Location`` além dele e do resto já casado pela expressão da rota.
    """

    def get(self, guild_id: str, rest: str = "") -> None:
        self.redirect(f"/painel/servidores/{guild_id}/jukebox{rest}", permanent=True)

    def head(self, guild_id: str, rest: str = "") -> None:
        self.get(guild_id, rest)

    def post(self, *args: str) -> None:
        raise tornado.web.HTTPError(404)
