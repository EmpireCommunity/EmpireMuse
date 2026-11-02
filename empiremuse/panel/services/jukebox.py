"""Categoria Jukebox (configurar e reiniciar): validação síncrona, tokens das confirmações e jobs das tarefas.

A Jukebox é UMA por servidor (item 11, P4): lida e gravada só pela seção ``jukebox`` do documento único
(``pool.servers.jukebox``); o ``rev`` dela é a versão do formulário (P-V7). As regras e os efeitos no Discord ficam em
``utils/music/jukebox_channel.py`` (compartilhado com os comandos) e quem os executa é o bot do painel. Aqui:
- a validação roda ANTES de criar a tarefa (422 sem nenhum efeito no Discord nem no banco);
- configurar num canal que já existe SEMPRE limpa as mensagens dele (spec final do dono), por isso tem dois passos
  (confirmação com token assinado, 60 s, uso único); criar um canal novo e usar um fórum não limpam nada e vão direto;
- reiniciar (o /jukebox reset) tem dois passos pelo mesmo token;
- os jobs releem a seção dentro da trava do servidor e passam o ``JukeboxConfig`` lido ao módulo compartilhado.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, NamedTuple, Optional

import disnake

from empiremuse.panel.services.base import DIGITS, Args, FormContext, first, version_of
from empiremuse.panel.services.jukebox_tokens import JukeboxTokens
from empiremuse.panel.services.service import SettingsService
from empiremuse.panel.services.tasks import Job, Task, TaskFailure, TaskRunner
from empiremuse.rules import limits
from empiremuse.utils import branding
from empiremuse.utils.music.converters import perms_translations
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.music.jukebox_channel import (
    MissingManagePermissions,
    apply_jukebox_channel,
    build_channel_kwargs,
    create_jukebox_channel,
    ensure_can_create_channel,
    ensure_can_reset,
    find_current_jukebox_message,
    find_jukebox_channel,
    jukebox_reset,
    missing_channel_permissions,
    validate_jukebox_target,
)
from empiremuse.utils.server_jukebox import JukeboxConfig

logger = logging.getLogger(__name__)

MODES = ("existente", "criar")
KINDS = {"texto": "text", "voz": "voice", "palco": "stage"}
TASK_SETUP = "configurar"
TASK_RESET = "reiniciar"
_ACCEPTED = (disnake.TextChannel, disnake.VoiceChannel, disnake.StageChannel, disnake.ForumChannel)
_TYPE_NAMES = (
    (disnake.StageChannel, "palco"),
    (disnake.VoiceChannel, "voz"),
    (disnake.ForumChannel, "forum"),
    (disnake.TextChannel, "texto"),
)

MSG_MODE = "Escolha como configurar a Jukebox."
MSG_CHANNEL = "Escolha um canal da lista."
MSG_TITLE = f"O título do post deve ter de {limits.FORUM_TITLE_MIN} a {limits.FORUM_TITLE_MAX} caracteres."
MSG_TYPE = "Escolha o tipo de canal: texto, voz ou palco."
MSG_STAGE = "Canais de palco só existem em servidores com o recurso Comunidade ativado."
MSG_CATEGORY = "Escolha uma categoria da lista (ou deixe sem categoria)."
MSG_PURGE = (
    "A Jukebox apaga todas as mensagens do canal ao ser configurada: o bot precisa das permissões "
    "Gerenciar Mensagens e Mostrar histórico de mensagens nele."
)
MSG_NO_CHANNEL = "A Jukebox não está configurada neste servidor."
MSG_TOKEN = "A confirmação expirou ou é inválida. Comece de novo."
MSG_BUSY = "Já existe uma operação em andamento neste servidor. Aguarde terminar e tente de novo."
MSG_LIMIT = "Há operações demais em andamento. Aguarde um pouco e tente de novo."
MSG_SETUP_OK = "A Jukebox foi configurada."
MSG_RESET_OK = "A Jukebox foi reiniciada."


class PanelActor:
    """Quem executa a ação no Discord: o usuário do painel (menção e ``str()`` para motivos/avisos)."""

    def __init__(self, user_id: str) -> None:
        self.id = user_id
        self.mention = f"<@{user_id}>"

    def __str__(self) -> str:
        return f"{self.id} (painel)"


def plain_message(text: str) -> str:
    """Texto de um ``GenericError`` sem markdown, menções nem blocos de código."""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"<[@#][!&]?\d+>", "", text)
    text = text.replace("**", "").replace("`", "")
    return re.sub(r"[ \t]+", " ", text).strip()


MSG_MANAGE_PERMISSIONS = (
    f"O {branding.BOT_NAME} precisa da permissão Gerenciar permissões neste canal (ou de "
    "Administrador). Conceda a permissão ao cargo do bot ou escolha Criar um canal novo."
)


def refusal_message(exc: GenericError) -> str:
    """Mensagem da recusa em termos do painel (a do módulo compartilhado fala do comando do Discord)."""
    if isinstance(exc, MissingManagePermissions):
        return MSG_MANAGE_PERMISSIONS
    return plain_message(exc.text)


# ============================================================================ estruturas dos templates


class ChannelItem(NamedTuple):
    id: str
    nome: str
    tipo: str


class ChannelGroup(NamedTuple):
    categoria: Optional[str]
    canais: tuple[ChannelItem, ...]


class TypeOption(NamedTuple):
    value: str
    label: str


class CategoryOption(NamedTuple):
    id: str
    nome: str


class CurrentJukebox(NamedTuple):
    configurado: bool
    nome: Optional[str]
    removido: bool
    bot_painel: Optional[str] = None  # nome do bot que envia o painel e atende o canal


class Confirmation(NamedTuple):
    """Passo 2 do reiniciar: token e o nome do canal (que fica, com as mensagens dos usuários)."""

    token: str
    nome_canal: str


class SetupConfirmation(NamedTuple):
    """Passo 2 do configurar num canal existente: token, o canal (nome) e os campos a reenviar."""

    token: str
    nome_canal: str
    campos: tuple[tuple[str, str], ...]


class TaskView(NamedTuple):
    url: str
    estado: str
    final: bool
    titulo: str
    mensagem: str


@dataclass(frozen=True)
class SetupPlan:
    mode: str
    channel_id: int = 0
    title: str = ""
    kind: str = "text"
    category_id: int = 0

    @property
    def purges(self) -> bool:
        """Configurar num canal existente (que não é fórum) apaga as mensagens dele: pede confirmação."""
        return self.mode == "existente" and not self.title


@dataclass
class Check:
    """Resultado de uma validação: ``errors`` vazio = pode executar. ``echo`` devolve o que o usuário enviou."""

    errors: dict[str, str]
    echo: dict[str, Any]
    missing: tuple[str, ...] = ()
    plan: Optional[SetupPlan] = None


# ============================================================================ serviço


class JukeboxService:
    def __init__(
        self, runner: TaskRunner, settings: SettingsService, secret: str, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.runner, self._settings = runner, settings
        self.tokens = JukeboxTokens(secret, clock)

    # ------------------------------------------------------------------ leitura

    SCOPE = "servidor"  # trava e auditoria: a Jukebox é do servidor, não de um bot

    @staticmethod
    def version(config: JukeboxConfig) -> str:
        """Versão do formulário: o conteúdo da seção (canal e mensagem), o mesmo que o store confere ao gravar (CAS)."""
        return version_of({"channel": config.channel, "message_id": config.message_id})

    @staticmethod
    def resolve(ctx: FormContext, raw: Optional[str], kinds: tuple) -> Optional[Any]:
        """Canal do cache do bot neste servidor, de um dos tipos aceitos; ``None`` se id inválido/alheio/de outro tipo."""
        if raw is None or not DIGITS.fullmatch(raw):
            return None
        channel = ctx.bot.get_channel(int(raw))
        if channel is None or getattr(getattr(channel, "guild", None), "id", None) != ctx.guild.id:
            return None
        return channel if isinstance(channel, kinds) else None

    @staticmethod
    def _guild_channels(guild: Any) -> list:
        channels = guild.channels
        return list(channels.values()) if isinstance(channels, dict) else list(channels)

    def current(self, ctx: FormContext, config: JukeboxConfig) -> CurrentJukebox:
        bot_name = str(getattr(ctx.bot.user, "name", "") or "") or None
        if not config.channel:
            return CurrentJukebox(False, None, False, bot_name)
        channel = ctx.bot.get_channel(config.channel)
        name = str(channel.name) if channel is not None else None
        return CurrentJukebox(True, name, name is None, bot_name)

    def choices(
        self, ctx: FormContext
    ) -> tuple[tuple[ChannelGroup, ...], tuple[TypeOption, ...], tuple[CategoryOption, ...]]:
        channels = self._guild_channels(ctx.guild)
        grouped: dict[Optional[str], list[ChannelItem]] = {}
        for channel in channels:
            kind = next((name for cls, name in _TYPE_NAMES if isinstance(channel, cls)), None)
            if kind is None:
                continue
            category = getattr(channel, "category", None)
            grouped.setdefault(str(category.name) if category is not None else None, []).append(
                ChannelItem(str(channel.id), str(channel.name), kind)
            )
        groups = tuple(
            ChannelGroup(name, tuple(sorted(items, key=lambda i: i.nome.casefold())))
            for name, items in sorted(grouped.items(), key=lambda kv: (kv[0] is not None, (kv[0] or "").casefold()))
        )
        types = [TypeOption("texto", "Canal de texto"), TypeOption("voz", "Canal de voz")]
        if "COMMUNITY" in ctx.guild.features:
            types.append(TypeOption("palco", "Canal de palco"))
        categories = tuple(
            CategoryOption(str(c.id), str(c.name)) for c in channels if isinstance(c, disnake.CategoryChannel)
        )
        return groups, tuple(types), categories

    # ------------------------------------------------------------------ validação (síncrona, sem efeitos)

    @staticmethod
    def echo_of(args: Args) -> dict[str, Any]:
        """Os campos do formulário de configurar como vieram (o que o passo 2 reenvia)."""
        return {
            "modo": first(args, "modo") or "",
            "canal": first(args, "canal") or "",
            "titulo_forum": (first(args, "titulo_forum") or "").strip(),
            "tipo": first(args, "tipo") or "",
            "categoria": first(args, "categoria") or "",
        }

    def validate_setup(self, ctx: FormContext, args: Args) -> Check:
        echo = self.echo_of(args)
        mode = echo["modo"]
        if mode not in MODES:
            return Check({"modo": MSG_MODE}, echo)
        if mode == "existente":
            return self._validate_existing(ctx, echo)
        return self._validate_create(ctx, echo)

    def _validate_existing(self, ctx: FormContext, echo: dict) -> Check:
        target = self.resolve(ctx, echo["canal"], _ACCEPTED)
        if target is None:
            return Check({"canal": MSG_CHANNEL}, echo)
        try:
            validate_jukebox_target(ctx.bot, ctx.guild, target, existing_channel=True)
        except GenericError as exc:
            missing = tuple(perms_translations.get(p, p) for p in missing_channel_permissions(target))
            return Check({"canal": refusal_message(exc)}, echo, missing)
        errors: dict[str, str] = {}
        is_forum = isinstance(target, disnake.ForumChannel)
        if is_forum and not limits.FORUM_TITLE_MIN <= len(echo["titulo_forum"]) <= limits.FORUM_TITLE_MAX:
            errors["titulo_forum"] = MSG_TITLE
        if not is_forum and not self._can_purge(ctx, target):  # a limpeza é sempre: sem permissão não configura
            errors["canal"] = MSG_PURGE
        if errors:
            return Check(errors, echo)
        plan = SetupPlan("existente", target.id, echo["titulo_forum"] if is_forum else "")
        return Check({}, echo, plan=plan)

    @staticmethod
    def _can_purge(ctx: FormContext, target: Any) -> bool:
        permissions = target.permissions_for(ctx.guild.me)
        return bool(permissions.manage_messages and permissions.read_message_history)

    def _validate_create(self, ctx: FormContext, echo: dict) -> Check:
        errors: dict[str, str] = {}
        kind = KINDS.get(echo["tipo"])
        if kind is None:
            errors["tipo"] = MSG_TYPE
        elif kind == "stage" and "COMMUNITY" not in ctx.guild.features:
            errors["tipo"] = MSG_STAGE
        else:
            try:
                ensure_can_create_channel(ctx.bot, ctx.guild)
            except GenericError as exc:
                errors["tipo"] = refusal_message(exc)
        category_id = 0
        if echo["categoria"] != "":
            category = self.resolve(ctx, echo["categoria"], (disnake.CategoryChannel,))
            if category is None:
                errors["categoria"] = MSG_CATEGORY
            else:
                category_id = category.id
        if errors:
            return Check(errors, echo)
        return Check({}, echo, plan=SetupPlan("criar", kind=kind or "text", category_id=category_id))

    # ------------------------------------------------------------------ confirmações (token assinado)

    def setup_confirmation(self, ctx: FormContext, plan: SetupPlan, echo: dict[str, Any]) -> SetupConfirmation:
        """Passo 2 do configurar: o token prende o canal escolhido; os campos voltam ocultos para o reenvio."""
        token = self.tokens.issue(ctx.user_id, ctx.guild_id, TASK_SETUP, str(plan.channel_id))
        channel = ctx.bot.get_channel(plan.channel_id)
        name = str(getattr(channel, "name", "") or "")
        fields = tuple((key, str(value)) for key, value in echo.items() if key in ("modo", "canal"))
        return SetupConfirmation(token, name, fields)

    def check_setup_confirmation(self, ctx: FormContext, plan: SetupPlan, args: Args) -> Optional[str]:
        """``None`` se o token do passo 2 vale para ESTE canal; senão ``"invalido"`` ou ``"outro"``."""
        token = first(args, "token") or ""
        return self.tokens.check(token, ctx.user_id, ctx.guild_id, TASK_SETUP, str(plan.channel_id))

    def check_reset(self, ctx: FormContext, config: JukeboxConfig) -> Optional[str]:
        """Mensagem de erro (pt-BR) se reiniciar não pode começar; ``None`` se pode."""
        if not config.channel:
            return MSG_NO_CHANNEL
        try:
            ensure_can_reset(ctx.guild)
        except GenericError as exc:
            return refusal_message(exc)
        if self.current(ctx, config).removido:
            return MSG_NO_CHANNEL
        return None

    def confirmation(self, ctx: FormContext, config: JukeboxConfig) -> Confirmation:
        token = self.tokens.issue(ctx.user_id, ctx.guild_id, TASK_RESET, str(config.channel))
        return Confirmation(token, self.current(ctx, config).nome or "")

    def check_confirmation(self, ctx: FormContext, config: JukeboxConfig, args: Args) -> Optional[str]:
        """``None`` se o token do passo 2 vale para a Jukebox atual; senão ``"invalido"`` ou ``"outro"``."""
        token = first(args, "token") or ""
        return self.tokens.check(token, ctx.user_id, ctx.guild_id, TASK_RESET, str(config.channel or ""))

    # ------------------------------------------------------------------ tarefas

    def start_setup(self, ctx: FormContext, plan: SetupPlan) -> Task:
        return self.runner.submit(ctx.user_id, ctx.guild_id, ctx.bot_id, TASK_SETUP, self._setup_job(ctx, plan))

    def start_reset(self, ctx: FormContext) -> Task:
        return self.runner.submit(ctx.user_id, ctx.guild_id, ctx.bot_id, TASK_RESET, self._reset_job(ctx))

    def _setup_job(self, ctx: FormContext, plan: SetupPlan) -> Job:
        async def job() -> str:
            async with self._settings.lock(self.SCOPE, ctx.guild_id):
                await self._setup(ctx, plan)
            return MSG_SETUP_OK

        return job

    async def _setup(self, ctx: FormContext, plan: SetupPlan) -> None:
        config = await ctx.pool.servers.jukebox.get(ctx.guild_id)  # relido na trava: o módulo grava com esse rev
        player, original_message = await find_current_jukebox_message(ctx.bot, ctx.guild, config)
        channel_kwargs = build_channel_kwargs(ctx.guild)
        try:
            target = await self._target(ctx, plan, channel_kwargs)
            await apply_jukebox_channel(
                ctx.bot,
                ctx.guild,
                PanelActor(ctx.user_id),
                target,
                existing_channel=plan.mode == "existente",
                channel_kwargs=channel_kwargs,
                channel_name=plan.title,
                original_message=original_message,
                player=player,
                config=config,
            )
        except GenericError as exc:
            raise TaskFailure(refusal_message(exc))
        self._audit(ctx, TASK_SETUP)

    async def _target(self, ctx: FormContext, plan: SetupPlan, channel_kwargs: dict) -> Any:
        if plan.mode == "criar":
            ensure_can_create_channel(ctx.bot, ctx.guild)
            parent = ctx.bot.get_channel(plan.category_id) if plan.category_id else ctx.guild
            if parent is None:
                raise TaskFailure(MSG_CATEGORY)
            return await create_jukebox_channel(ctx.bot, parent, plan.kind, channel_kwargs)
        target = ctx.bot.get_channel(plan.channel_id)
        if target is None:
            raise TaskFailure(MSG_CHANNEL)
        return target

    def _reset_job(self, ctx: FormContext) -> Job:
        async def job() -> str:
            async with self._settings.lock(self.SCOPE, ctx.guild_id):
                await self._reset(ctx)
            return MSG_RESET_OK

        return job

    async def _reset(self, ctx: FormContext) -> None:
        """Limpa a seção, apaga o painel e devolve as opções A e B aos padrões (o módulo compartilhado faz os três);
        o canal e as mensagens dos usuários ficam."""
        config = await ctx.pool.servers.jukebox.get(ctx.guild_id)

        async def notify(event: str) -> None:
            return None

        try:
            channel = await find_jukebox_channel(ctx.bot, ctx.guild_id, config)
            await jukebox_reset(
                ctx.bot,
                ctx.guild,
                PanelActor(ctx.user_id),
                channel,
                channel_inter=self._detached_channel(ctx),
                config=config,
                notify=notify,
            )
        except GenericError as exc:
            raise TaskFailure(refusal_message(exc))
        self._audit(ctx, TASK_RESET)

    @staticmethod
    def _detached_channel(ctx: FormContext) -> Any:
        """Onde o player volta a "falar" depois de reiniciar: o canal de voz atual dele, se houver."""
        voice = getattr(ctx.guild.me, "voice", None)
        return getattr(voice, "channel", None)

    @classmethod
    def _audit(cls, ctx: FormContext, kind: str) -> None:
        # Só ids e o tipo da operação: título, nome de canal e token nunca vão para o log.
        logger.info(
            "Configuração alterada: usuario=%s servidor=%s escopo=%s categoria=jukebox campos=%s bot=%s",
            ctx.user_id,
            ctx.guild_id,
            cls.SCOPE,
            kind,
            ctx.bot_id,
        )

    # ------------------------------------------------------------------ exibição da tarefa

    @staticmethod
    def task_view(task: Task, guild_id: int) -> TaskView:
        title = "Configurando a Jukebox" if task.kind == TASK_SETUP else "Reiniciando a Jukebox"
        if task.state == "erro":
            title = "Não foi possível concluir"
        elif task.state == "ok":
            title = "Concluído"
        message = task.message or (
            "Aguardando a vez…" if task.state == "pendente" else "Isso pode levar alguns segundos."
        )
        return TaskView(
            f"/painel/servidores/{guild_id}/jukebox/tarefas/{task.id}", task.state, task.final, title, message
        )
