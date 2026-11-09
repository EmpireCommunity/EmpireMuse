# -*- coding: utf-8 -*-
import asyncio
import logging
import os
import re
import sys
import time
from functools import partial
from typing import Union, Optional

import disnake
from aiohttp import ClientError, ClientSession, ClientTimeout
from disnake.ext import commands
from disnake.http import Route

import empiremuse.wavelink as wavelink
from empiremuse import paths
from empiremuse.config_loader import load_config
from empiremuse.cogs.data_erasure import OwnerRequestView
from empiremuse.rules.erasure import OWNER_COMMAND_NAME, owner_target_id, parse_owner_request
from empiremuse.rules.errors import RuleError
from empiremuse.rules.git_remote import sanitized_repo_url
from empiremuse.rules.owner_shell import build_shell_env, mask_secrets, mask_shell_output
from empiremuse.rules.release import Version, changelog_section, changelog_sections
from empiremuse.rules.settings import validate_prefix
from empiremuse.updater.git_flow import (
    EXIT_RESTART,
    UpdateError,
    UpdateRollbackFailed,
    apply_git_update,
    fetch_remote_changelog,
    git_env,
    latest_official_version,
    plan_git_update,
    run_exec,
)
from empiremuse.updater.docker_flow import (
    describe_exit,
    plan_docker_update,
    send_helper_log,
    start_docker_update,
    watch_helper,
)
from empiremuse.updater.mode import MODE_DOCKER, MODE_GIT, MODE_PANEL, update_mode
from empiremuse.updater.notice import DiscordReport
from empiremuse.updater.proxy_client import ProxyClient
from empiremuse.updater.releases import fetch_changelog, list_releases
from empiremuse.updater.state import UpdateRequest, clear_pending, read_pending
from empiremuse.updater.views import ConfirmView
from empiremuse.utils import branding
from empiremuse.utils.client import BotCore
from empiremuse.utils.db import DBModel
from empiremuse.utils.music.checks import check_voice, check_requester_channel, can_connect
from empiremuse.utils.music.converters import URL_REG, md_link
from empiremuse.utils.music.errors import GenericError, NoVoice, code_suffix, log_unexpected_error
from empiremuse.utils.music.interactions import SelectBotVoice
from empiremuse.utils.music.lavalink_server_list import validated_server_list
from empiremuse.utils.music.models import LavalinkPlayer
from empiremuse.utils.others import CustomContext, string_to_file, CommandArgparse, select_bot_pool
from empiremuse.utils.owner_panel import panel_command, PanelView
from empiremuse.version import image_version

MSG_INVALID_PREFIX = "**O prefixo não pode conter espaços ou ter acima de 5 caracteres.**"

logger = logging.getLogger(__name__)

# Trecho máximo do comando que vai para o log de execução de comandos de owner.
MAX_LOGGED_COMMAND_LENGTH = 200

# Saída máxima (em caracteres) lida do histórico do git.
MAX_GIT_READ_OUTPUT = 4 * 1024 * 1024
GIT_READ_TIMEOUT = 60
# Janela do "o que mudou" mostrada quando o bot não se atualiza sozinho (cabe com folga num embed).
MANUAL_CHANGELOG_CHARS = 1500

# ``!updatelog`` sem ``.git`` (imagem Docker): a seção do CHANGELOG da versão em execução é buscada uma vez por versão e por
# processo (o cog ``Owner`` existe uma vez por bot). Todo 200 fica em cache; uma falha só é tentada de novo depois de
# ``CHANGELOG_FAILURE_SECONDS``; chamadas simultâneas esperam a mesma consulta (padrão ``_cache``/``_loading`` do painel).
CHANGELOG_FAILURE_SECONDS = 60
_changelog_cache: dict[str, str] = {}
_changelog_failed_at: dict[str, float] = {}
_changelog_loading: dict[str, "asyncio.Task[str]"] = {}
# Já avisou que não há ``.git`` (na imagem isso é o normal: um aviso por processo, as demais vezes em debug).
_warned_no_git = False


def format_git_log(data_list: list):
    data = []

    for d in data_list:
        if not d:
            continue
        t = d.split("*****")
        data.append({"commit": t[0], "abbreviated_commit": t[1], "subject": t[2], "timestamp": t[3]})

    return data


def _pool_watch_tasks(pool) -> set[asyncio.Task]:
    """Conjunto de tarefas de acompanhamento do auxiliar no ``pool`` (criado sob demanda em fakes sem ``__init__``)."""
    tasks = getattr(pool, "helper_watch_tasks", None)
    if not isinstance(tasks, set):
        tasks = set(tasks) if tasks else set()
        pool.helper_watch_tasks = tasks
    return tasks


def _finish_watch(tasks: set[asyncio.Task], task: asyncio.Task) -> None:
    tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning("update: o acompanhamento do auxiliar terminou com erro", exc_info=task.exception())


class CommandFailed(Exception):
    """O comando terminou com status diferente de zero; a mensagem traz a saída e o status."""


async def run_command(cmd: str):
    p = await asyncio.create_subprocess_shell(
        cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        # Ambiente mínimo: o .env carregado no processo (tokens, senhas) não fica visível ao comando.
        env=build_shell_env(os.environ, windows=os.name == "nt"),
    )
    stdout, stderr = await p.communicate()
    r = ShellResult(p.returncode, stdout, stderr)
    if r.status != 0:
        raise CommandFailed(f"{r.stderr or r.stdout}\n\nStatus Code: {r.status}")
    return str(r.stdout)


async def run_git(*args: str, git_dir: str) -> str:
    """Git de leitura sem shell: ``--git-dir`` absoluto, ambiente mínimo (sem ``GIT_DIR`` herdado), saída mascarada.

    A árvore de trabalho é a pasta de execução. ``git_dir`` relativo vale contra o cwd do PROCESSO, como o
    ``os.path.isdir`` de quem chama, e por isso é resolvido antes de o git mudar de pasta.
    Levanta ``CommandFailed`` com a saída se o git sair com status diferente de zero.
    """
    command = [
        "git",
        f"--git-dir={os.path.abspath(git_dir)}",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        *args,
    ]
    result = await run_exec(
        *command,
        cwd=paths.RUNTIME_DIR,
        env=git_env(os.environ),
        timeout=GIT_READ_TIMEOUT,
        max_output=MAX_GIT_READ_OUTPUT,
    )
    if result.returncode != 0:
        raise CommandFailed(f"{result.output}\n\nStatus Code: {result.returncode}")
    return result.output


class ShellResult:
    def __init__(self, status: int, stdout: Optional[bytes], stderr: Optional[bytes]):
        self.status = status
        self.stdout = stdout.decode(encoding="utf-8", errors="replace") if stdout is not None else None
        self.stderr = stderr.decode(encoding="utf-8", errors="replace") if stderr is not None else None


def _log_missing_git(git_dir: str) -> None:
    global _warned_no_git
    if _warned_no_git:
        logger.debug("updatelog: não há repositório git em %s.", git_dir)
        return
    _warned_no_git = True
    logger.warning("updatelog: não há repositório git em %s.", git_dir)  # o caminho fica só no log


async def _load_running_section(version: Version) -> str:
    key = str(version)
    try:
        async with ClientSession() as session:
            raw = await fetch_changelog(session, f"v{key}")
    except UpdateError:
        _changelog_failed_at[key] = time.monotonic()
        logger.warning("updatelog: não consegui obter o CHANGELOG da versão %s.", key)
        return ""
    finally:
        _changelog_loading.pop(key, None)
    # O CHANGELOG de uma tag rc usa a seção da versão estável (o release.yml extrai as notas por X.Y.Z); a busca usa a tag inteira.
    stable = Version(version.major, version.minor, version.patch)
    section = changelog_section(raw, stable, MANUAL_CHANGELOG_CHARS)
    _changelog_cache[key] = section
    return section


async def _running_version_section(version: Version) -> str:
    """Seção do CHANGELOG da versão em execução (``""`` se a consulta falhou ou a seção não existe), com cache e uma consulta só."""
    key = str(version)
    if key in _changelog_cache:
        return _changelog_cache[key]
    failed_at = _changelog_failed_at.get(key)
    if failed_at is not None:
        if time.monotonic() - failed_at < CHANGELOG_FAILURE_SECONDS:
            return ""
        del _changelog_failed_at[key]
    task = _changelog_loading.get(key)
    if task is None:
        task = _changelog_loading[key] = asyncio.ensure_future(_load_running_section(version))
    return await asyncio.shield(task)


class Owner(commands.Cog):
    git_format = "--pretty=format:%H*****%h*****%s*****%ct"

    def __init__(self, bot: BotCore):
        self.bot = bot
        self.owner_view: Optional[PanelView] = None
        self.extra_hints = bot.config["EXTRA_HINTS"].split("||")

    def secret_values(self) -> frozenset:
        """Valores de segredo conhecidos (configuração do bot e ambiente do processo) que nunca saem em claro."""
        return self.bot.pool.secret_values()

    def log_owner_command(self, ctx, name: str, detail: str = ""):
        """Registra quem executou qual comando de owner (o horário vem do formato do log), sem a saída e sem segredos."""
        detail = mask_secrets(detail, self.secret_values())[:MAX_LOGGED_COMMAND_LENGTH]
        # warning: a trilha de auditoria precisa sair mesmo com ENABLE_LOGGER desligado (o nível padrão é WARNING)
        # ``empiremuse_emit``: o agregador junta WARNING de mesmo template; sem isto a trilha de auditoria do dono perderia linhas
        logger.warning(
            "comando de owner: usuário=%s comando=%s detalhe=%r",
            ctx.author.id,
            name,
            detail,
            extra={"empiremuse_emit": True},
        )

    @commands.Cog.listener("on_command")
    async def log_jishaku_command(self, ctx: CustomContext):
        command = ctx.command
        if command is None or (command.root_parent or command).qualified_name != "jishaku":
            return
        if await self.bot.is_owner(ctx.author):
            self.log_owner_command(ctx, "jishaku", ctx.message.content)

    @commands.Cog.listener("on_message")
    async def erasure_request_dm(self, message: disnake.Message) -> None:
        """``apagardados <id>`` (§3.4): SÓ no privado do dono com o bot. O clique do dono nunca apaga: ele só envia a DM de concessão ao titular."""
        if not isinstance(message.channel, disnake.DMChannel) or message.author.id not in self.bot.env_owner_ids:
            return  # primeira linha: qualquer outra coisa é ignorada em silêncio, sem resposta a ninguém
        argument = parse_owner_request(message.content, self.bot.default_prefix)
        if argument is None:
            return
        tr = self.bot.pool.i18n.for_locale(None)
        target_id = owner_target_id(argument)
        if target_id is None:
            await message.channel.send(tr("erasure.owner.invalid_id"))
            return
        try:
            target = await self.bot.fetch_user(target_id)
        except disnake.NotFound:
            await message.channel.send(tr("erasure.owner.not_found"))
            return
        except disnake.HTTPException as error:
            logger.warning("apagardados: fetch_user falhou (%s)", type(error).__name__)
            await message.channel.send(tr("erasure.owner.lookup_failed"))
            return
        if target.bot:
            await message.channel.send(tr("erasure.owner.not_found"))
            return
        self.log_owner_command(message, OWNER_COMMAND_NAME, f"pedido de apagamento id={target.id}")
        name = disnake.utils.escape_markdown(disnake.utils.escape_mentions(target.display_name))
        embed = disnake.Embed(description=tr("erasure.owner.ask", name=name, id=target.id))
        embed.set_thumbnail(url=target.display_avatar.url)
        await message.channel.send(
            embed=embed,
            view=OwnerRequestView(self.bot, tr, message.author.id, target),
            allowed_mentions=disnake.AllowedMentions.none(),
        )

    def format_log(self, data: list):
        return "\n".join(
            f"{md_link(c['abbreviated_commit'], f'{self.bot.pool.remote_git_url}/commit/{c["commit"]}')} `- "
            f"{(c['subject'][:40].replace('`', '') + '...') if len(c['subject']) > 39 else c['subject']}` "
            f"(<t:{c['timestamp']}:R>)"
            for c in data
        )

    @commands.cooldown(1, 10, commands.BucketType.user)
    @commands.is_owner()
    @commands.command(
        hidden=True,
        aliases=["gls", "lavalink", "lllist", "lavalinkservers"],
        description="Baixar um arquivo com lista de servidores lavalink para usá-los no sistema de música.",
    )
    async def getlavaservers(self, ctx: CustomContext):
        await ctx.defer()

        await self.download_lavalink_serverlist()

        await ctx.send(
            embed=disnake.Embed(
                description="**O arquivo lavalink.ini foi baixado com sucesso!\n"
                "Será necessário me reiniciar para usar os servidores deste arquivo.**"
            )
        )

    updatelavalink_flags = CommandArgparse()
    updatelavalink_flags.add_argument("-yml", "--yml", action="store_true", help="legacy.flag.yml")
    updatelavalink_flags.add_argument(
        "-resetids",
        "-reset",
        "--resetids",
        "--reset",
        help="legacy.flag.resetids",
        action="store_true",
    )

    def _local_lavalink_enabled(self) -> bool:
        """Lavalink local ligado na config, ou com Java vivo (o boot também o sobe com a lista de servidores vazia).

        Olhar só ``lavalink_instance`` impedia uma nova tentativa depois de uma falha no reinício (instância ``None``).
        """
        return bool(self.bot.config["RUN_LOCAL_LAVALINK"] or self.bot.pool.lavalink_instance)

    @commands.is_owner()
    @commands.max_concurrency(1, commands.BucketType.default)
    @commands.command(hidden=True, aliases=["restartll", "rtll", "rll"])
    async def restartlavalink(self, ctx: CustomContext):
        if not self._local_lavalink_enabled():
            raise GenericError("**O servidor LOCAL não está sendo usado!**")

        await self.bot.pool.start_lavalink()

        await ctx.send(
            embed=disnake.Embed(
                description="**Reiniciando o servidor lavalink LOCAL.**", color=self.bot.get_color(ctx.guild.me)
            )
        )

    @commands.is_owner()
    @commands.max_concurrency(1, commands.BucketType.default)
    @commands.command(
        hidden=True, aliases=["ull", "updatell", "llupdate", "llu"], extras={"flags": updatelavalink_flags}
    )
    async def updatelavalink(self, ctx: CustomContext, flags: str = ""):
        if not self._local_lavalink_enabled():
            raise GenericError("**O servidor LOCAL não está sendo usado!**")

        args, unknown = ctx.command.extras["flags"].parse_known_args(flags.split())

        self.bot.pool.stop_lavalink()

        async with ctx.typing():
            await asyncio.sleep(1.5)

            jar_file = paths.RUNTIME_DIR / "Lavalink.jar"
            if jar_file.is_file():
                jar_file.unlink()

            yml_file = paths.RUNTIME_DIR / "application.yml"
            if args.yml and yml_file.is_file():
                yml_file.unlink()
                txt = "Os arquivos Lavalink.jar e application.yml serão atualizados"
            else:
                txt = "O arquivo Lavalink.jar será atualizado"

            await self.bot.pool.start_lavalink()

        if args.resetids:
            for b in self.bot.pool.bots:
                try:
                    node = b.music.nodes["LOCAL"]
                except KeyError:
                    continue
                for p in node.players.values():
                    for t in p.queue:
                        t.id = None
                        t.info["id"] = None
                for p in node.players.values():
                    for t in p.played:
                        t.id = None
                        t.info["id"] = None
                for p in node.players.values():
                    for t in p.queue_autoplay:
                        t.id = None
                        t.info["id"] = None

        await ctx.send(
            embed=disnake.Embed(
                description=f"**{txt} e o servidor lavalink LOCAL será reiniciado.**",
                color=self.bot.get_color(ctx.guild.me),
            )
        )

    @commands.is_owner()
    @panel_command(
        aliases=["rcfg"],
        description="Recarregar as configs do bot.",
        emoji="⚙",
        alt_name="Recarregar as configs do bot.",
    )
    async def reloadconfig(self, ctx: Union[CustomContext, disnake.MessageInteraction]):
        self.bot.pool.config = self.bot.pool.load_cfg()

        txt = "**As configurações do bot foram recarregadas com sucesso!**"

        if isinstance(ctx, CustomContext):
            embed = disnake.Embed(colour=self.bot.get_color(ctx.me), description=txt)
            await ctx.send(embed=embed, view=self.owner_view)
        else:
            return txt

    @commands.is_owner()
    @panel_command(aliases=["rds", "recarregarskins"], description="Recarregar skins.", emoji="🎨")
    async def reloadskins(self, ctx: Union[CustomContext, disnake.MessageInteraction]):
        for m in list(sys.modules):
            if not m.startswith("empiremuse.utils.music.skins."):
                continue
            try:
                del sys.modules[m]
            except KeyError:
                continue

        self.bot.pool.load_skins()

        txt = "**As skins foram recarregadas com sucesso!**"

        if isinstance(ctx, CustomContext):
            embed = disnake.Embed(colour=self.bot.get_color(ctx.me), description=txt)
            await ctx.send(embed=embed, view=self.owner_view)
        else:
            return txt

    @commands.is_owner()
    @panel_command(
        aliases=["recarregar"],
        description="Recarregar módulos.",
        emoji="🔄",
        alt_name="Carregar/Recarregar os módulos.",
    )
    async def reload(self, ctx: Union[CustomContext, disnake.MessageInteraction], *modules):
        modules = [f"{m.lower()}.py" for m in modules]

        modules_list = set()

        for bot in (allbots := set(self.bot.pool.get_all_bots())):
            for m in list(bot.extensions):
                for m_name in modules:
                    if m_name[:-3].lower() == m.split(".")[-1]:
                        modules_list.add(m)

        # unload_sys_list = set()

        # for m in modules_list:
        #    for cog_name, cog in self.bot.cogs.items():
        #        if cog.__module__ == m and (cog_modules:=getattr(cog, "modules", None)):
        #            unload_sys_list.update(cog_modules)

        for bot in allbots:
            for m in modules_list:
                bot.unload_extension(m)

        # for m in unload_sys_list:
        #    try:
        #        del sys.modules[m]
        #    except KeyError:
        #        pass

        data = {}

        if isinstance(ctx, CustomContext):
            await ctx.trigger_typing()

        for bot in allbots:
            data = bot.load_modules(modules)
            bot.sync_command_cooldowns(force=True)
            if not data["failed"]:
                await bot.sync_app_commands(force=True)

        txt = ""

        if loaded := data["loaded"] + data["reloaded"]:
            txt += f"**Módulos carregados/recarregados:** ```ansi\n[0;34m{' [0;37m| [0;34m'.join(loaded)}```\n"

        if data["failed"]:
            txt += f"**Módulos que falharam:** ```ansi\n[0;31m{' [0;37m| [0;31m'.join(data['failed'])}```\n"

        if not txt:
            raise GenericError("**Nenhum módulo encontrado...**")

        self.bot.pool.config = load_config()

        if isinstance(ctx, CustomContext):
            await ctx.send(embed=disnake.Embed(colour=self.bot.get_color(ctx.me), description=txt))
        else:
            return txt

    def _release_update_lock(self) -> None:
        lock = self.bot.pool.update_lock
        if lock.locked():
            lock.release()

    @commands.is_owner()
    @commands.max_concurrency(1, commands.BucketType.default)
    @panel_command(
        aliases=["up", "atualizar"],
        description="Atualizar o bot para a versão oficial mais nova.",
        emoji="🛠️",
        alt_name="Atualizar Bot",
    )
    async def update(self, ctx: Union[CustomContext, disnake.MessageInteraction], *, opts: str = ""):
        # O decorador vale por bot; a conferência de dono e a trava valem aqui, no fluxo (e para o botão do painel).
        if not await self.bot.is_owner(ctx.author):
            raise GenericError("**Apenas meu(s) desenvolvedor(es) pode(m) atualizar o bot.**")
        pool = self.bot.pool
        if pool.update_lock.locked():
            raise self._in_progress_error()

        await pool.update_lock.acquire()
        handed_over = False  # a trava passa para a janela de confirmação, que a solta ao cancelar, expirar ou terminar
        try:
            if not isinstance(ctx, CustomContext):
                await (
                    ctx.response.defer()
                )  # a consulta ao remoto passa de 3 s; o botão do painel expiraria antes da resposta
            if (pending := read_pending(paths.get_runtime_dir(), time.time())) is not None:
                await self._resolve_stale_pending(
                    ctx, pending
                )  # recusa (GenericError) ou libera um pendente Docker parado
            mode = update_mode(self.bot.config, os.environ, paths.PROJECT_DIR)
            if mode == MODE_GIT:
                handed_over, text = await self._offer_git_update(ctx)
            elif mode == MODE_DOCKER:
                handed_over, text = await self._offer_docker_update(ctx)
            else:
                text = await self._describe_manual_update(ctx, mode)
        finally:
            if not handed_over:
                self._release_update_lock()
        return text

    @staticmethod
    def _in_progress_error() -> GenericError:
        return GenericError("**Já há uma atualização em andamento.** Aguarde o aviso de conclusão.")

    async def _resolve_stale_pending(self, ctx, pending) -> None:
        """Pendente existente: recusa, a não ser que seja um pendente Docker cujo auxiliar já parou (ou sumiu).

        Só tenta resolver com ``status == "pending"`` e sem nenhum acompanhamento deste processo vivo: com ``ready`` quem
        resolve é o aviso pós-boot (resolver aqui entregaria o resultado duas vezes). Auxiliar rodando ou proxy
        inacessível: recusa. Parado ou ausente: conta o resultado da atualização anterior no canal, manda o log mascarado
        por DM quando ela falhou, remove o auxiliar e só com a remoção confirmada apaga o pendente (D12).
        """
        socket = self.bot.config.get("UPDATE_PROXY_SOCKET")
        watching = any(not task.done() for task in _pool_watch_tasks(self.bot.pool))
        if pending.flow != "docker" or pending.status != "pending" or watching or not socket:
            raise self._in_progress_error()
        client = ProxyClient(socket)
        try:
            state = await client.helper_state()
        except UpdateError:
            raise self._in_progress_error() from None
        if state is not None and state.running:
            raise self._in_progress_error()
        # Só ``exited`` tem código de saída de verdade: um auxiliar criado e nunca iniciado (created), morto (dead) ou em
        # remoção também não roda, mas o ExitCode dele é 0 e não quer dizer "concluída".
        finished = state is not None and state.status == "exited"
        code = state.exit_code if finished else None
        await self._say(
            ctx, f"ℹ️ **Resultado da atualização anterior (para {pending.to_version}):** {describe_exit(code)}."
        )
        if finished and code != 0:
            await send_helper_log(client, DiscordReport(self.bot, pending), pending, self.secret_values())
        try:
            await client.remove_helper()
        except UpdateError:
            raise self._in_progress_error() from None
        clear_pending(paths.get_runtime_dir())

    async def _say(self, ctx, text: str) -> None:
        """Mensagem solta no canal do comando (no painel, só para quem clicou)."""
        embed = disnake.Embed(description=text, colour=self.bot.get_color())
        if isinstance(ctx, CustomContext):
            await ctx.send(embed=embed)
        else:
            await ctx.followup.send(embed=embed, ephemeral=True)

    async def _reply_update(self, ctx, text: str) -> Optional[str]:
        """No prefixo manda o embed e não devolve nada; no painel devolve o texto para o painel mostrar."""
        if isinstance(ctx, CustomContext):
            await ctx.send(embed=disnake.Embed(description=text, colour=self.bot.get_color()))
            return None
        return text

    async def _describe_manual_update(self, ctx, mode: str) -> Optional[str]:
        """Modos em que o bot não se atualiza sozinho (imagem gerenciada ou código solto): informa a versão e a ação do modo.

        A imagem Docker não tem ``git``: no modo ``panel`` a versão vem das releases do GitHub (mesma fonte e elegibilidade
        do fluxo Docker); no ``manual`` vem do ``ls-remote``. Sem a versão a resposta traz a ação do modo mesmo assim.
        """
        latest = await (self._latest_release_version() if mode == MODE_PANEL else latest_official_version())
        installed = image_version() if mode == MODE_PANEL else None
        self.log_owner_command(ctx, "update", f"{mode} {installed or '?'}->{latest or '?'}")
        if mode == MODE_PANEL:
            action = "Atualize pelo painel da hospedagem: este bot roda numa imagem gerenciada por ela."
        else:
            target = f"da versão {latest}" if latest else "da versão mais recente"
            action = f"Reimplante o código {target}: este bot não tem um clone git para se atualizar sozinho."
        if latest is None:
            text = (
                "**Não consegui consultar a versão disponível agora.**\n\n"
                f"{action}\n{md_link('Versões publicadas', f'{branding.REPO_URL}/releases', code=False)}"
            )
            return await self._reply_update(ctx, text)
        if installed is not None and latest <= installed:
            if installed.is_prerelease:
                return await self._reply_update(
                    ctx, f"✅ **Você está na versão de teste {installed}; ainda não há versão estável mais nova.**"
                )
            return await self._reply_update(ctx, f"✅ **Você já está na versão mais recente ({installed}).**")
        if mode == MODE_PANEL:
            raw = await self._release_changelog(f"v{latest}")
        else:
            raw = await asyncio.get_running_loop().run_in_executor(None, fetch_remote_changelog, f"v{latest}")
        notes = changelog_sections(raw, installed or Version(0, 0, 0), latest, MANUAL_CHANGELOG_CHARS)
        link = f"{branding.REPO_URL}/blob/v{latest}/CHANGELOG.md"
        title = f"🆕 **Versão disponível: {latest}**" + (f" (instalada: {installed})" if installed else "")
        parts = [title, notes, f"{action}\n{md_link('CHANGELOG completo', link, code=False)}"]
        return await self._reply_update(ctx, "\n\n".join(part for part in parts if part))

    async def _latest_release_version(self) -> Optional[Version]:
        """Maior versão publicada nas releases do GitHub (elegíveis); ``None`` se a consulta falhar ou não houver release."""
        try:
            async with ClientSession() as session:
                available = await list_releases(session)
        except UpdateError:
            return None
        return max((release.version for release in available), default=None)

    async def _release_changelog(self, tag: str) -> str:
        """CHANGELOG da tag nas releases do GitHub; vazio se não der (a resposta segue sem "o que mudou")."""
        try:
            async with ClientSession() as session:
                return await fetch_changelog(session, tag)
        except UpdateError:
            return ""

    async def _offer_docker_update(self, ctx) -> tuple[bool, Optional[str]]:
        """Planeja pelas releases do GitHub e pede a confirmação (proxy configurado). Mesma devolução do fluxo git."""
        current = image_version()
        if current is None:
            raise GenericError(
                "**Não consegui identificar a versão desta imagem; atualize pelo painel da hospedagem.**"
            )
        try:
            async with ClientSession() as session:
                plan = await plan_docker_update(session, current)
        except UpdateError as error:
            raise GenericError(f"**{error}**") from error
        if plan is None:
            if current.is_prerelease:
                text = f"✅ **Você está na versão de teste {current}; ainda não há versão estável mais nova.**"
            else:
                text = "✅ **Você já está na versão mais recente.**"
            return False, await self._reply_update(ctx, text)
        self.log_owner_command(ctx, "update", f"{MODE_DOCKER} {plan.current}->{plan.target}")

        channel_id = getattr(getattr(ctx, "channel", None), "id", None) or ctx.channel_id
        request = UpdateRequest(bot_id=self.bot.user.id, channel_id=channel_id, requested_by=ctx.author.id)
        view = ConfirmView(ctx.author.id, partial(self._apply_docker_update, plan, request), self._release_update_lock)
        embed = disnake.Embed(colour=self.bot.get_color(), description=self._confirmation_text(plan, docker=True))
        if isinstance(ctx, CustomContext):
            view.message = await ctx.send(embed=embed, view=view)
            return True, None
        view.message = await ctx.followup.send(embed=embed, view=view, ephemeral=True, wait=True)
        return True, "Confirmação enviada só para você."

    async def _apply_docker_update(self, plan, request: UpdateRequest, interaction: disnake.MessageInteraction) -> None:
        """Roda depois do clique em Confirmar: inicia o auxiliar e passa a acompanhá-lo. Quem troca o container é o auxiliar."""
        self._stop_ready_check()  # o pendente é do processo NOVO; este não pode marcá-lo como pronto
        try:
            progress = disnake.Embed(
                colour=self.bot.get_color(), description=f"⏳ Iniciando a atualização para {plan.target}..."
            )
            await interaction.response.edit_message(embed=progress, view=None)
            client = ProxyClient(self.bot.config["UPDATE_PROXY_SOCKET"])
            try:
                pending = await start_docker_update(client, plan, request, paths.get_runtime_dir())
            except UpdateError as error:
                text = f"❌ {error}"
            except Exception as error:
                text = f"❌ **Falha inesperada na atualização.**{code_suffix(log_unexpected_error(logger, error, 'atualização'))}"
            else:
                text = (
                    f"✅ **Atualização para {plan.target} iniciada.** O bot reinicia sozinho em instantes; "
                    "o resultado chega neste canal."
                )
                self._watch_docker_update(client, pending)
            await interaction.edit_original_message(embed=disnake.Embed(colour=self.bot.get_color(), description=text))
        except disnake.HTTPException:
            logger.warning("update: não consegui editar a mensagem de progresso", exc_info=True)
        finally:
            self._release_update_lock()

    def _watch_docker_update(self, client, pending) -> None:
        """Acompanha o auxiliar em segundo plano enquanto este processo viver (a tarefa não é cancelada por quem confirmou)."""
        report = DiscordReport(self.bot, pending)
        tasks = _pool_watch_tasks(self.bot.pool)
        watcher = asyncio.create_task(
            watch_helper(client, pending, paths.get_runtime_dir(), report, secrets=self.secret_values())
        )
        tasks.add(watcher)
        watcher.add_done_callback(partial(_finish_watch, tasks))

    async def _offer_git_update(self, ctx) -> tuple[bool, Optional[str]]:
        """Planeja e pede a confirmação. Devolve ``(trava_entregue, texto)``; a trava só é entregue se a janela foi enviada."""
        try:
            plan = await plan_git_update(paths.PROJECT_DIR)
        except UpdateError as error:
            raise GenericError(f"**{error}**") from error
        if plan is None:
            return False, await self._reply_update(ctx, "✅ **Você já está na versão mais recente.**")
        self.log_owner_command(ctx, "update", f"{MODE_GIT} {plan.current}->{plan.target}")

        channel_id = getattr(getattr(ctx, "channel", None), "id", None) or ctx.channel_id
        request = UpdateRequest(bot_id=self.bot.user.id, channel_id=channel_id, requested_by=ctx.author.id)
        view = ConfirmView(ctx.author.id, partial(self._apply_git_update, plan, request), self._release_update_lock)
        embed = disnake.Embed(colour=self.bot.get_color(), description=self._confirmation_text(plan))
        if isinstance(ctx, CustomContext):
            view.message = await ctx.send(embed=embed, view=view)
            return True, None
        # Painel (já deferido em ``update``): a mensagem do painel segue como está e a confirmação vai só para quem clicou.
        view.message = await ctx.followup.send(embed=embed, view=view, ephemeral=True, wait=True)
        return True, "Confirmação enviada só para você."

    @staticmethod
    def _confirmation_text(plan, *, docker: bool = False) -> str:
        notes = plan.changelog or "_Esta versão não traz notas no CHANGELOG._"
        warning = "⚠️ O bot **fica indisponível** durante a atualização e reinicia ao fim."
        if not docker and not os.environ.get("INVOCATION_ID"):
            warning += "\n⚠️ Não detectei um supervisor (systemd): o bot **vai parar** e você precisará iniciá-lo à mão."
        return f"🛠️ **Atualizar de {plan.current} para {plan.target}?**\n\n{notes}\n\n{warning}"

    async def _apply_git_update(self, plan, request: UpdateRequest, interaction: disnake.MessageInteraction) -> None:
        """Roda depois do clique em Confirmar: aplica, avisa o resultado e, se deu certo, encerra o processo com 75."""
        restart = False
        self._stop_ready_check()  # o pendente que o apply vai gravar é do processo NOVO; este não pode marcá-lo como pronto
        try:
            progress = disnake.Embed(colour=self.bot.get_color(), description=f"⏳ Atualizando para {plan.target}...")
            await interaction.response.edit_message(embed=progress, view=None)
            try:
                await apply_git_update(plan, request, paths.PROJECT_DIR, paths.get_runtime_dir())
            except UpdateRollbackFailed as error:
                # O ``applying`` ficou no disco: reinicia já, para o boot_guard reverter num processo limpo.
                logger.error("update: a reversão automática falhou e o applying ficou no disco: %s", error)
                text = f"❌ {error}"
                restart = True
            except UpdateError as error:
                text = f"❌ {error}"
            except Exception as error:
                text = f"❌ **Falha inesperada na atualização.**{code_suffix(log_unexpected_error(logger, error, 'atualização'))}"
            else:
                text = f"✅ **Código atualizado para {plan.target}.** Reiniciando; o resultado chega neste canal."
                restart = True
            await interaction.edit_original_message(embed=disnake.Embed(colour=self.bot.get_color(), description=text))
        except disnake.HTTPException:
            logger.warning("update: não consegui editar a mensagem de progresso", exc_info=True)
        finally:
            self._release_update_lock()
        if restart:
            await self._restart_process()

    def _stop_ready_check(self) -> None:
        """Desliga a checagem de ``ready`` deste processo (que vai ser trocado): marca como feita e cancela o watcher."""
        pool = self.bot.pool
        pool.update_ready_checked = True
        if pool.update_watch_task:
            pool.update_watch_task.cancel()

    async def _restart_process(self) -> None:
        """Encerra o pool com o código de reinício (75) e, com o servidor web no ar, também o laço dele (senão o processo não sai)."""
        pool = self.bot.pool
        await pool.shutdown(EXIT_RESTART)
        if pool.config["RUN_RPC_SERVER"]:
            import tornado.ioloop

            loop = tornado.ioloop.IOLoop.current()
            loop.add_callback(loop.stop)

    @commands.max_concurrency(1, commands.BucketType.guild)
    @commands.cooldown(1, 10, commands.BucketType.user)
    @panel_command(
        aliases=["latest", "lastupdate"],
        description="Ver minhas atualizações mais recentes.",
        emoji="📈",
        alt_name="Ultimas atualizações",
        hidden=False,
    )
    async def updatelog(self, ctx: Union[CustomContext, disnake.MessageInteraction], amount: int = 10):
        git_dir = os.environ.get("GIT_DIR") or str(paths.RUNTIME_DIR / ".git")
        if os.path.isdir(git_dir):
            txt = await self._git_updatelog(git_dir, amount)
        else:
            _log_missing_git(git_dir)
            txt = await self._image_updatelog()

        if isinstance(ctx, CustomContext):
            embed = disnake.Embed(description=txt, color=self.bot.get_color(ctx.guild.me))

            await ctx.send(embed=embed)

        else:
            return txt

    async def _git_updatelog(self, git_dir: str, amount: int) -> str:
        if not self.bot.pool.remote_git_url:
            self.bot.pool.remote_git_url = sanitized_repo_url(self.bot.config["SOURCE_REPO"], branding.REPO_URL)

        git_log = []

        count = amount if amount and amount > 0 else 10
        data = (await run_git("log", f"-{count}", self.git_format, git_dir=git_dir)).split("\n")

        git_log += format_git_log(data)

        return (
            f"🔰 ** | {md_link('Atualizações recentes:', f'{self.bot.pool.remote_git_url}/commits/main', code=False)}**\n\n"
            + self.format_log(git_log)
        )

    async def _image_updatelog(self) -> str:
        """Sem ``.git``: na imagem, as novidades da versão em execução; sem versão, só o caminho para as releases."""
        version = image_version()
        if version is None:
            return "**Não há histórico de atualizações neste bot.**\n" + md_link(
                "Versões publicadas", f"{branding.REPO_URL}/releases", code=False
            )
        parts = [
            f"📈 **Novidades da versão {version} (em execução)**",
            md_link("Notas da versão", f"{branding.REPO_URL}/releases/tag/v{version}", code=False),
            await _running_version_section(version),
        ]
        return "\n\n".join(part for part in parts if part)

    @commands.is_owner()
    @commands.command(hidden=True, aliases=["menu"])
    async def panel(self, ctx: CustomContext):
        embed = disnake.Embed(title="PAINEL DE CONTROLE.", color=self.bot.get_color(ctx.guild.me))
        embed.set_footer(text="Clique em uma tarefa que deseja executar.")
        await ctx.send(embed=embed, view=PanelView(self.bot))

    @commands.has_guild_permissions(manage_guild=True)
    @commands.cooldown(1, 10, commands.BucketType.guild)
    @commands.command(
        aliases=["prefix", "mudarprefixo"],
    )
    async def setprefix(self, ctx: CustomContext, prefix: str):
        prefix = validate_prefix(prefix)

        if isinstance(prefix, RuleError):
            raise GenericError(MSG_INVALID_PREFIX)

        guild_data = await self.bot.get_global_data(ctx.guild.id, db_name=DBModel.guilds)

        self.bot.pool.guild_prefix_cache[ctx.guild.id] = prefix
        guild_data["prefix"] = prefix
        await self.bot.update_global_data(ctx.guild.id, guild_data, db_name=DBModel.guilds)

        prefix = disnake.utils.escape_markdown(prefix)

        embed = disnake.Embed(
            description=f"**O meu prefixo no servidor agora é:** `{prefix}`\n"
            f"**Caso queira restaurar o prefixo padrão use o comando:** `{prefix}{self.resetprefix.name}`",
            color=self.bot.get_color(ctx.guild.me),
        )

        await ctx.send(embed=embed)

    @commands.has_guild_permissions(manage_guild=True)
    @commands.cooldown(1, 10, commands.BucketType.guild)
    @commands.command()
    async def resetprefix(self, ctx: CustomContext):
        guild_data = await self.bot.get_global_data(ctx.guild.id, db_name=DBModel.guilds)

        if not guild_data["prefix"]:
            raise GenericError("**Nao há prefixo configurado no servidor.**")

        guild_data["prefix"] = ""
        self.bot.pool.guild_prefix_cache[ctx.guild.id] = ""

        await self.bot.update_global_data(ctx.guild.id, guild_data, db_name=DBModel.guilds)

        embed = disnake.Embed(
            description=f"**O prefixo do servidor foi resetado com sucesso.\n"
            f"O prefixo padrão agora é:** `{disnake.utils.escape_markdown(self.bot.default_prefix)}`",
            color=self.bot.get_color(ctx.guild.me),
        )

        await ctx.send(embed=embed)

    @commands.cooldown(1, 10, commands.BucketType.guild)
    @commands.command(
        aliases=["myprefix", "uprefix", "meuprefixo"],
    )
    async def setuserprefix(self, ctx: CustomContext, prefix: str):
        prefix = validate_prefix(prefix)

        if isinstance(prefix, RuleError):
            raise GenericError(MSG_INVALID_PREFIX)

        user_data = await self.bot.get_global_data(ctx.author.id, db_name=DBModel.users)

        user_data["custom_prefix"] = prefix
        self.bot.pool.user_prefix_cache[ctx.author.id] = prefix
        await self.bot.update_global_data(ctx.author.id, user_data, db_name=DBModel.users)

        prefix = disnake.utils.escape_markdown(prefix)

        embed = disnake.Embed(
            description=f"**O seu prefixo de usuário agora é:** `{prefix}`\n"
            f"**Caso queira remover seu prefixo de usuário use o comando:** `{prefix}{self.resetuserprefix.name}`",
            color=self.bot.get_color(ctx.guild.me),
        )

        await ctx.send(embed=embed)

    @commands.cooldown(1, 10, commands.BucketType.guild)
    @commands.command()
    async def resetuserprefix(self, ctx: CustomContext):
        user_data = await self.bot.get_global_data(ctx.author.id, db_name=DBModel.users)

        if not user_data["custom_prefix"]:
            raise GenericError("**Você não possui prefixo configurado.**")

        user_data["custom_prefix"] = ""
        self.bot.pool.user_prefix_cache[ctx.author.id] = ""
        await self.bot.update_global_data(ctx.author.id, user_data, db_name=DBModel.users)

        embed = disnake.Embed(
            description=f"**O seu prefixo de usuário foi removido com sucesso.**",
            color=self.bot.get_color(ctx.guild.me),
        )

        await ctx.send(embed=embed)

    @commands.is_owner()
    @commands.command(
        aliases=["guildprefix", "sgp", "gp"],
        hidden=True,
        description="Setar um prefixo manualmente pra um server com o id informado (útil para botlists)",
    )
    async def setguildprefix(self, ctx: CustomContext, server_id: int, prefix: str = None):
        if not 17 < len(str(server_id)) < 24:
            raise GenericError("**A quantidade de caracteres do id do servidor tem que estar entre 18 a 23.**")

        guild_data = await self.bot.get_global_data(server_id, db_name=DBModel.guilds)

        embed = disnake.Embed(color=self.bot.get_color(ctx.guild.me))

        prefix = (prefix or "").strip()

        if prefix and isinstance(validate_prefix(prefix), RuleError):
            raise GenericError(MSG_INVALID_PREFIX)

        if not prefix:
            guild_data["prefix"] = ""
            await ctx.bot.update_global_data(server_id, guild_data, db_name=DBModel.guilds)
            embed.description = "**O prefixo antecipado do servidor com o id informado foi resetado com sucesso.**"

        else:
            guild_data["prefix"] = prefix
            await self.bot.update_global_data(server_id, guild_data, db_name=DBModel.guilds)
            embed.description = (
                f"**O prefixo para o servidor com o id informado agora é:** {disnake.utils.escape_markdown(prefix)}"
            )

        self.bot.pool.guild_prefix_cache[server_id] = prefix

        await ctx.send(embed=embed)

    @commands.is_owner()
    @commands.command(hidden=True)
    async def cleardm(self, ctx: CustomContext, amount: int = 20):
        counter = 0

        async with ctx.typing():
            async for msg in ctx.author.history(limit=int(amount)):
                if msg.author.id == self.bot.user.id:
                    await msg.delete()
                    await asyncio.sleep(0.5)
                    counter += 1

        if not counter:
            raise GenericError(f"**Nenhuma mensagem foi deletada de {amount} verificada{'s'[: amount ^ 1]}...**")

        if counter == 1:
            txt = "**Uma mensagem foi deletada do seu DM.**"
        else:
            txt = f"**{counter} mensagens foram deletadas do seu DM.**"

        await ctx.send(embed=disnake.Embed(description=txt, colour=self.bot.get_color(ctx.guild.me)))

    @commands.Cog.listener("on_button_click")
    async def close_shell_result(self, inter: disnake.MessageInteraction):
        if inter.data.custom_id != "close_shell_result":
            return

        if not await self.bot.is_owner(inter.author):
            return await inter.send("**Apenas meu dono pode usar este botão!**", ephemeral=True)

        await inter.response.edit_message(
            content="```ini\n🔒 - [Shell Fechado!] - 🔒```", attachments=None, view=None, embed=None
        )

    @commands.is_owner()
    @commands.command(aliases=["sh"], hidden=True)
    async def shell(self, ctx: CustomContext, *, command: str):
        if command.startswith("```") and command.endswith("```"):
            if command[4] != "\n":
                command = f"```\n{command[3:]}"
            if command[:-4] != "\n":
                command = command[:-3] + "\n```"
            command = "\n".join(command.split("\n")[1:-1])
        else:
            command = command.strip("` \n")

        self.log_owner_command(ctx, "shell", command)

        try:
            async with ctx.typing():
                output, failed = await run_command(command), False
        except CommandFailed as e:
            output, failed = str(e), True

        # A saída só vai por DM, com os segredos conhecidos mascarados; nunca para o canal.
        output = mask_shell_output(output, self.secret_values())
        content = f"```py\n{output}```"
        kwargs = {}
        if len(content) > 2000:
            kwargs["file"] = string_to_file(output, filename=f"shell_result_{ctx.message.id}.txt")
        else:
            kwargs["content"] = content
        if not failed:
            kwargs["components"] = [disnake.ui.Button(label="Fechar Shell", custom_id="close_shell_result", emoji="♻️")]

        try:
            await ctx.author.send(**kwargs)
        except disnake.Forbidden:
            raise GenericError(
                "**Não consegui enviar o resultado no seu DM: libere suas mensagens diretas e execute o comando de novo.**"
            )

        await ctx.message.add_reaction("⚠️" if failed else "✅")

    @check_voice()
    @commands.cooldown(1, 15, commands.BucketType.guild)
    @commands.command(aliases=["spawn", "smn"])
    async def summon(self, ctx: CustomContext):
        try:
            ctx.bot.music.players[ctx.guild.id]  # type ignore
            raise GenericError("**Já há um player iniciado no servidor.**")
        except KeyError:
            pass

        bot = ctx.bot
        guild = ctx.guild
        channel = ctx.channel
        msg = None

        if bot.user.id not in ctx.author.voice.channel.voice_states:
            free_bots = []

            for b in self.bot.pool.get_all_bots():
                if not b.bot_ready:
                    continue

                g = b.get_guild(ctx.guild_id)

                if not g:
                    continue

                p = b.music.players.get(ctx.guild_id)

                if p and ctx.author.id not in p.last_channel.voice_states:
                    continue

                free_bots.append(b)

            if len(free_bots) > 1:
                v = SelectBotVoice(ctx, guild, free_bots)

                msg = await ctx.send(
                    embed=disnake.Embed(
                        description=f"**Escolha qual bot você deseja usar no canal {ctx.author.voice.channel.mention}**",
                        color=self.bot.get_color(guild.me),
                    ),
                    view=v,
                )

                ctx.store_message = msg

                await v.wait()

                if v.status is None:
                    await msg.edit(
                        embed=disnake.Embed(description="### Tempo esgotado...", color=self.bot.get_color(guild.me)),
                        view=None,
                    )
                    return

                if v.status is False:
                    await msg.edit(
                        embed=disnake.Embed(description="### Operação cancelada.", color=self.bot.get_color(guild.me)),
                        view=None,
                    )
                    return

                if not v.inter.author.voice:
                    await msg.edit(
                        embed=disnake.Embed(
                            description="### Você não está conectado em um canal de voz...",
                            color=self.bot.get_color(guild.me),
                        ),
                        view=None,
                    )
                    return

                if not v.inter.author.voice:
                    raise NoVoice()

                bot = v.bot
                ctx = v.inter
                guild = v.guild
                channel = bot.get_channel(ctx.channel.id)

        can_connect(channel=ctx.author.voice.channel, guild=guild)

        node: wavelink.Node = bot.music.get_best_node()

        if not node:
            raise GenericError("**Não há servidores de música disponível!**")

        player: LavalinkPlayer = await bot.get_cog("Music").create_player(
            inter=ctx, bot=bot, guild=guild, channel=channel
        )

        await player.connect(ctx.author.voice.channel.id)

        if msg:
            await msg.edit(
                f"Sessão de música iniciada no canal {ctx.author.voice.channel.mention}\nVia: {bot.user.mention}{player.controller_link}",
                components=None,
                embed=None,
            )
        else:
            self.bot.loop.create_task(ctx.message.add_reaction("👍"))

        while not ctx.guild.me.voice:
            await asyncio.sleep(1)

        if isinstance(ctx.author.voice.channel, disnake.StageChannel):
            stage_perms = ctx.author.voice.channel.permissions_for(guild.me)
            if stage_perms.manage_permissions:
                await guild.me.edit(suppress=False)

            await asyncio.sleep(1.5)

        await player.process_next()

    @commands.is_owner()
    @commands.command(
        hidden=True,
        aliases=["setbotbanner"],
        description="Alterar o banner do bot usando anexo ou link direto de uma imagem jpg ou gif.",
    )
    async def setbanner(self, ctx: CustomContext, url: str = ""):
        await self.setavatar.callback(self=self, ctx=ctx, url=url, mode="banner")

    @commands.is_owner()
    @commands.command(
        hidden=True,
        aliases=["setbotavatar"],
        description="Alterar o avatar do bot usando anexo ou link direto de uma imagem jpg ou gif.",
    )
    async def setavatar(self, ctx: CustomContext, url: str = "", mode="avatar"):
        use_hyperlink = False

        if re.match(r"^<.*>$", url):
            use_hyperlink = True
            url = url.strip("<>")

        if not url:
            if not ctx.message.attachments:
                raise GenericError("Você deve informar o link de uma imagem ou gif (ou anexar uma) no comando.")

            url = ctx.message.attachments[0].url

            if not url.split("?ex=")[0].endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")):
                raise GenericError("Você deve anexar um arquivo válido: png, jpg, jpeg, webp, gif, bmp.")

        elif not URL_REG.match(url):
            raise GenericError("Você informou um link inválido.")

        inter, bot = await select_bot_pool(ctx, return_new=True)

        if not bot:
            return

        if isinstance(inter, CustomContext):
            try:
                func = inter.store_message.edit
            except AttributeError:
                func = inter.send
        else:
            await inter.response.defer(ephemeral=True)
            func = inter.edit_original_message

        await func(
            f"O novo {mode} do bot {bot.user.mention} está sendo processado. Por favor aguarde...",
            embed=None,
            view=None,
        )

        async with ctx.bot.session.get(url) as r:
            if r.status != 200:
                raise GenericError(f"Erro {r.status}: {await r.text()}")
            image_bytes = await r.read()

        payload = {mode: await disnake.utils._assetbytes_to_base64_data(image_bytes)}
        await bot.http.edit_profile(payload)

        if mode == "avatar":
            await bot.http.request(
                Route("PATCH", "/applications/@me"), json={"icon": disnake.utils._bytes_to_base64_data(image_bytes)}
            )

        try:
            func = inter.store_message.edit
        except AttributeError:
            try:
                func = inter.edit_original_message
            except AttributeError:
                try:
                    func = inter.response.edit_message
                except AttributeError:
                    func = inter.send

        avatar_txt = mode if not use_hyperlink else md_link(mode, url, code=False)

        await func(f"O {avatar_txt} do bot {bot.user.mention} foi alterado com sucesso.", view=None, embed=None)

    async def cog_check(self, ctx: CustomContext) -> bool:
        return await check_requester_channel(ctx)

    async def cog_load(self) -> None:
        self.owner_view = PanelView(self.bot)

    async def download_lavalink_serverlist(self):
        try:
            async with ClientSession(timeout=ClientTimeout(total=30)) as session:
                async with session.get(self.bot.config["LAVALINK_SERVER_LIST"], allow_redirects=False) as r:
                    ini_file = validated_server_list(r.status, await r.read())
        except ClientError, asyncio.TimeoutError:
            logger.warning(
                "Falha ao baixar a lista de servidores lavalink, usando o arquivo local do repositório.", exc_info=True
            )
            ini_file = validated_server_list(None, None)

        with open(paths.RUNTIME_DIR / "lavalink.ini", "wb") as f:
            f.write(ini_file)


def setup(bot: BotCore):
    bot.add_cog(Owner(bot))
