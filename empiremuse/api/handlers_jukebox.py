"""Rotas da Jukebox (B4, contrato T-456 §4 "Jukebox"; ameaças T-436/T-458 m4).

``GET/PUT /guilds/{g}/jukebox`` leem e gravam as duas opções (formulário com CAS); ``POST /guilds/{g}/jukebox/tasks``
configura ou reinicia (202 ``task_id``) e ``GET …/tasks/{id}`` acompanha. Configurar num canal que já existe limpa as
mensagens dele e reiniciar apaga o painel: os dois pedem **confirmação em dois passos** (o passo 1, sem ``confirm_token``,
devolve o token e não faz nada; o passo 2 o apresenta). O token é assinado com a subchave da Jukebox, preso a usuário,
servidor, finalidade e canal, vale 60 s e uma vez só.

Toda escrita autoriza o usuário agora (``fresh=True``). A tarefa só aparece para quem a criou **e** no servidor dela; de
outro usuário ou outro servidor é o mesmo ``404`` de uma tarefa que não existe. Nenhum texto de usuário final sai: só o
estado da tarefa e códigos. Os serviços e as regras são os do painel genérico (``panel/services``).
"""

from __future__ import annotations

from typing import Any, Optional

from empiremuse.api.data import DataHandler, form_reply
from empiremuse.api.errors import BOT_UNAVAILABLE, ApiError, conflict, not_found, rate_limited, validation_failed
from empiremuse.api.idempotency import Reply
from empiremuse.api.writes_jukebox import TASK_CONFIGURE, TaskRequest, parse_options_write, parse_task_request
from empiremuse.panel.services.base import Formulario, FormContext
from empiremuse.panel.services.jukebox import Check, JukeboxService
from empiremuse.panel.services.jukebox_options import OPTIONS_ACTION, JukeboxOptionsForm
from empiremuse.panel.services.tasks import Task, TaskBusy, TaskLimit
from empiremuse.rules.panel_serialize import GUILD, serialize_form
from empiremuse.utils.server_jukebox import JukeboxConfig
from empiremuse.utils.servers import ServerConfigConflict, ServerConfigUnavailable
from empiremuse.utils.usage import record_panel_action

GUILD_ID = r"([0-9]{1,20})"
TASK_ID = r"([A-Za-z0-9_-]{32})"
CATEGORY = "jukebox"
OPTIONS_FORM = JukeboxOptionsForm()
STATUS_ACCEPTED, STATUS_OK, STATUS_CONFLICT = 202, 200, 409
TASK_RETRY_AFTER_SECONDS = 60
# o rótulo do registro de uso é o do painel genérico (``jukebox/<ação>``); nada que o usuário mandou vai para ele
ACTION_LABELS = {
    (TASK_CONFIGURE, False): "configurar",
    (TASK_CONFIGURE, True): "confirmar_configurar",
    ("reset", False): "reiniciar",
    ("reset", True): "confirmar_reiniciar",
}
TASK_KINDS = {"configurar": "configure", "reiniciar": "reset"}


def task_payload(task: Task) -> dict[str, Any]:
    """O estado de uma tarefa: o id, o tipo, o estado e se acabou. A mensagem (texto de usuário final) não sai."""
    return {"task_id": task.id, "kind": TASK_KINDS.get(task.kind, task.kind), "state": task.state, "final": task.final}


class JukeboxRoute(DataHandler):
    @property
    def jukebox(self) -> JukeboxService:
        return self.ctx.jukebox_service

    @staticmethod
    async def config_of(ctx: FormContext) -> JukeboxConfig:
        try:
            return await ctx.pool.servers.jukebox.get(ctx.guild_id)
        except ServerConfigUnavailable:
            raise ApiError(503, BOT_UNAVAILABLE) from None

    def own_active_task(self, ctx: FormContext) -> Optional[Task]:
        """A tarefa ativa do servidor, só se for do próprio usuário (a de outro seria ``404`` no acompanhamento)."""
        active = self.jukebox.runner.active_for_guild(ctx.guild_id)
        return active if active is not None and active.owner == ctx.user_id else None


class JukeboxHandler(JukeboxRoute):
    async def get(self, guild_id: str) -> None:
        ctx, _ = await self.guild_access(guild_id, write=False)
        config = await self.config_of(ctx)
        current = self.jukebox.current(ctx, config)
        groups, types, categories = self.jukebox.choices(ctx)
        options = await self.ctx.settings_service.read(OPTIONS_FORM, ctx, "")
        task = self.own_active_task(ctx)
        self.write(
            {
                "version": self.jukebox.version(config),
                "configured": current.configurado,
                "channel": (
                    {"id": str(config.channel), "name": current.nome, "missing": current.removido}
                    if current.configurado
                    else None
                ),
                "panel_bot": current.bot_painel,
                "options": serialize_form(options, GUILD),
                "choices": {
                    "channels": [
                        {
                            "category": group.categoria,
                            "channels": [
                                {"id": item.id, "name": item.nome, "type": item.tipo} for item in group.canais
                            ],
                        }
                        for group in groups
                    ],
                    "types": [option.value for option in types],
                    "categories": [{"id": item.id, "name": item.nome} for item in categories],
                },
                "task": task_payload(task) if task is not None else None,
            }
        )

    async def put(self, guild_id: str) -> None:
        ctx, _ = await self.guild_access(guild_id, write=True)
        body = self.json_body()
        version, args = parse_options_write(body)

        async def work() -> Reply:
            return await self.apply_options(ctx, version, args)

        await self.run_write(body, work)

    async def apply_options(self, ctx: FormContext, version: str, args: dict[str, list[str]]) -> Reply:
        service = self.ctx.settings_service
        label = f"{CATEGORY}/{OPTIONS_ACTION}"
        if self.jukebox.runner.active_for_guild(ctx.guild_id) is not None:  # a tarefa segura a trava do servidor
            record_panel_action(ctx, label, STATUS_CONFLICT)
            raise conflict()
        try:
            outcome = await service.submit(
                OPTIONS_FORM, ctx, {**args, "versao": [version]}, "", audit_user=ctx.user_id, category=CATEGORY
            )
        except ServerConfigConflict:
            record_panel_action(ctx, label, STATUS_CONFLICT)
            raise conflict((await service.read(OPTIONS_FORM, ctx, "")).versao) from None
        except ServerConfigUnavailable:
            raise ApiError(503, BOT_UNAVAILABLE) from None
        record_panel_action(ctx, label, outcome.status)
        return form_reply(outcome.status, outcome.form, GUILD)


class JukeboxTasksHandler(JukeboxRoute):
    """``POST /guilds/{g}/jukebox/tasks``: começa a configurar ou a reiniciar (``202``) depois da confirmação."""

    async def post(self, guild_id: str) -> None:
        ctx, _ = await self.guild_access(guild_id, write=True)
        body = self.json_body()
        request = parse_task_request(body)

        async def work() -> Reply:
            return await self.start(ctx, request)

        # o passo 1 só emite um token (nada a repetir); o passo 2 cria a tarefa e a repetição devolve o mesmo ``task_id``
        await self.run_write(body, work, replayable=request.confirm_token is not None)

    async def start(self, ctx: FormContext, request: TaskRequest) -> Reply:
        label = f"{CATEGORY}/{ACTION_LABELS[(request.action, request.confirm_token is not None)]}"
        reply: Optional[Reply] = None
        try:
            if self.jukebox.runner.active_for_guild(ctx.guild_id) is not None:
                raise conflict()  # antes da trava: a tarefa em andamento a segura até acabar
            async with self.ctx.settings_service.lock(self.jukebox.SCOPE, ctx.guild_id):
                reply = await self.dispatch(ctx, request)
        except ServerConfigConflict:
            record_panel_action(ctx, label, STATUS_CONFLICT)
            raise conflict() from None
        except ServerConfigUnavailable:
            raise ApiError(503, BOT_UNAVAILABLE) from None
        except ApiError as error:
            record_panel_action(ctx, label, error.status_code)
            raise
        record_panel_action(ctx, label, reply.status)
        return reply

    async def dispatch(self, ctx: FormContext, request: TaskRequest) -> Reply:
        config = await self.config_of(ctx)
        current = self.jukebox.version(config)
        if request.version != current:
            raise conflict(current)
        if request.action == TASK_CONFIGURE:
            return self.configure(ctx, request)
        return self.reset(ctx, config, request)

    def configure(self, ctx: FormContext, request: TaskRequest) -> Reply:
        service = self.jukebox
        check: Check = service.validate_setup(ctx, request.args)
        if check.errors or check.plan is None:
            raise validation_failed(*check.errors)
        plan = check.plan
        token = request.confirm_token
        if plan.purges and token is None:  # canal existente: as mensagens dele vão embora, o usuário confirma antes
            confirmation = service.setup_confirmation(ctx, plan, check.echo)
            return Reply(STATUS_OK, {"requires_confirmation": True, "confirm_token": confirmation.token})
        if plan.purges and service.check_setup_confirmation(ctx, plan, {"token": [token or ""]}) is not None:
            raise validation_failed("confirm_token")
        return self.submit(lambda: service.start_setup(ctx, plan), token if plan.purges else None)

    def reset(self, ctx: FormContext, config: JukeboxConfig, request: TaskRequest) -> Reply:
        service = self.jukebox
        if service.check_reset(ctx, config):
            raise validation_failed("action")
        token = request.confirm_token
        if token is None:
            return Reply(
                STATUS_OK, {"requires_confirmation": True, "confirm_token": service.confirmation(ctx, config).token}
            )
        if service.check_confirmation(ctx, config, {"token": [token]}) is not None:
            raise validation_failed("confirm_token")
        return self.submit(lambda: service.start_reset(ctx), token)

    def submit(self, start: Any, token: Optional[str]) -> Reply:
        try:
            task = start()
        except TaskBusy:
            raise conflict() from None
        except TaskLimit:
            raise rate_limited(TASK_RETRY_AFTER_SECONDS) from None
        if token is not None:
            self.jukebox.tokens.consume(token)
        return Reply(STATUS_ACCEPTED, {"task_id": task.id, "state": task.state})


class JukeboxTaskHandler(JukeboxRoute):
    """``GET /guilds/{g}/jukebox/tasks/{id}``: o estado da tarefa, só do dono e só no servidor dela."""

    async def get(self, guild_id: str, task_id: str) -> None:
        ctx, _ = await self.guild_access(guild_id, write=False)
        task = self.jukebox.runner.get(task_id)
        if task is None or task.owner != self.session.user.id or str(task.guild_id) != str(ctx.guild_id):
            raise not_found()
        self.write(task_payload(task))


def jukebox_routes() -> list[tuple[str, Any, str]]:
    """(expressão do caminho sob o prefixo, handler, caminho do OpenAPI)."""
    return [
        (f"/guilds/{GUILD_ID}/jukebox", JukeboxHandler, "/guilds/{guild_id}/jukebox"),
        (f"/guilds/{GUILD_ID}/jukebox/tasks", JukeboxTasksHandler, "/guilds/{guild_id}/jukebox/tasks"),
        (
            f"/guilds/{GUILD_ID}/jukebox/tasks/{TASK_ID}",
            JukeboxTaskHandler,
            "/guilds/{guild_id}/jukebox/tasks/{task_id}",
        ),
    ]
