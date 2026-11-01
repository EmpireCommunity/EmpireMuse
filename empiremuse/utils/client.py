# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import dataclasses
import datetime
import gc
import json
import logging
import os
import subprocess
import threading
import time
import traceback
from asyncio import get_running_loop
from functools import partial
from configparser import ConfigParser
from copy import deepcopy
from importlib import import_module
from pathlib import Path
from subprocess import check_output
from typing import Optional, Union, List, Dict

import aiohttp
import disnake
import requests
from aiohttp import ClientSession
from async_timeout import timeout
from cachetools import TTLCache
from disnake.ext import commands
from disnake.http import Route
from user_agent import generate_user_agent

import empiremuse.wavelink as wavelink
from empiremuse.serialization.cache import PLAYLIST_MAX_BYTES
from empiremuse import paths
from empiremuse.config_loader import load_config, resolve_port
from empiremuse.health import (
    HEALTH_FILE_ENV,
    HEARTBEAT_INTERVAL,
    clear_heartbeat,
    is_healthy,
    write_heartbeat,
)
from empiremuse.logs import configure_logging, flush_logging, parse_log_settings, register_secret, tick_logging
from empiremuse.logs.aggregate import AGGREGATE_WINDOW
from empiremuse.logs.boot import BootReport, check_new_version
from empiremuse.logs.context import guild_extra
from empiremuse.logs.errors import command_context, install_exception_handlers
from empiremuse.logs.metrics import SUMMARY_PREFIX, MetricsCollector, NodeSample, format_summary
from empiremuse.logs.metrics import process_stats as metrics_process_stats
from empiremuse.rules.bot_roles import NOTICE_WINDOW_SECONDS, main_missing_hint
from empiremuse.rules.git_remote import parse_remote_web_url, sanitized_repo_url
from empiremuse.rules.owner_shell import collect_secret_values
from empiremuse.rules.servers import is_main_identifier
from empiremuse.updater.notice import on_pool_ready
from empiremuse.updater.state import read_pending
from empiremuse.i18n import get_i18n
from empiremuse.utils import branding
from empiremuse.version import image_version
from empiremuse.utils.safe_error import HTTPStatusError, safe_error
from empiremuse.utils.db import MongoDatabase, LocalDatabase, get_prefix, DBModel, global_db_models
from empiremuse.utils.app_profile import sync_app_profile
from empiremuse.utils.guild_ban import GuildBans
from empiremuse.utils.radio import RadioHub
from empiremuse.utils.radio.notice import refuse_radio_only
from empiremuse.utils.request_panel import RequestPanel
from empiremuse.utils.servers import ServerStore
from empiremuse.rules.usage import KIND_PREFIX, KIND_SLASH
from empiremuse.utils.usage import UsageRecorder, usage_of
from empiremuse.utils.music.audio_sources.deezer import DeezerClient
from empiremuse.utils.music.audio_sources.spotify import SpotifyClient
from empiremuse.utils.music.checks import check_pool_bots
from empiremuse.utils.music.errors import GenericError, code_suffix, log_unexpected_error
from empiremuse.utils.music.lastfm_tools import LastFM
from empiremuse.utils.music.json_cache import load_json_cache, save_json_cache
from empiremuse.utils.music.legacy_notice import log_ignored_legacy_data
from empiremuse.utils.music.lavalink_server_list import validated_server_list
from empiremuse.utils.music.local_lavalink import LOCAL_LAVALINK_PASSWORD, run_lavalink, watch_lavalink_process
from empiremuse.utils.music.models import (
    music_mode,
    LavalinkPlayer,
    LavalinkPlaylist,
    LavalinkTrack,
    PartialTrack,
    native_sources,
    CustomYTDL,
)
from empiremuse.utils.music.remote_lavalink_serverlist import get_lavalink_servers
from empiremuse.utils.others import CustomContext, token_regex, sort_dict_recursively
from empiremuse.utils.owner_panel import PanelView
from empiremuse.web_app import WSClient, start

logger = logging.getLogger(__name__)

MAIN_PREFIX_WAIT = (
    5.0  # s que a reserva espera o principal assumir o comando de prefixo antes de ignorá-lo (ele pode ir ao banco)
)
node_logger = logging.getLogger("empiremuse.wavelink")
commands_logger = logging.getLogger("empiremuse.commands")

UPDATE_WATCH_INTERVAL = 5  # segundos entre as checagens de saúde do pool enquanto há uma atualização pendente
PLAYLIST_REFRESH_READY_WAIT = 300  # s que a migração dos ícones espera os bots ficarem prontos para redesenhar o painel


def _playlist_cache_file():
    return paths.RUNTIME_DIR / "local_database" / "playlist_cache.json"


def read_guild_bots() -> Dict[str, List[str]]:
    """Tokens dos bots exclusivos de servidor (``guild_bots.json`` no RUNTIME_DIR); vazio se o arquivo não existe."""
    try:
        with open(paths.RUNTIME_DIR / "guild_bots.json") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("Falha ao ler o guild_bots.json")
        return {}


def _node_sample(identifier: str, node) -> NodeSample:
    """Amostra de um nó do Lavalink a partir do ``stats`` já recebido; sem ``stats`` (ou indisponível) é tudo ``None``."""
    stats = getattr(node, "stats", None)
    if not stats or not getattr(node, "is_available", False):
        return NodeSample(identifier, None, None, None)
    return NodeSample(
        identifier,
        stats.players,
        round(stats.lavalink_load * 100, 1),
        round(stats.memory_used / 1048576, 1),
        *((stats.frames_sent, stats.frames_nulled, stats.frames_deficit) if stats.has_frame_stats else (None,) * 3),
    )


def _read_process_stats(process_stats, java_pid):
    """Lê o ``psutil`` (bloqueia): ``(figuras do Python, figuras do Java ou None)``; roda numa thread."""
    return process_stats(), (process_stats(java_pid) if java_pid else None)


def _with_java_figures(nodes: list[NodeSample], java_cpu, java_rss) -> list[NodeSample]:
    """Completa o nó ``LOCAL`` com CPU e memória do processo Java onde o ``stats`` do nó não trouxe."""
    return [
        dataclasses.replace(
            node,
            cpu_percent=node.cpu_percent if node.cpu_percent is not None else java_cpu,
            memory_mib=node.memory_mib if node.memory_mib is not None else java_rss,
        )
        if node.identifier == "LOCAL"
        else node
        for node in nodes
    ]


class ModuleLoadError(Exception):
    """Um módulo (cog) falhou na subida: o aviso (módulo + tipo) e o traceback em DEBUG já foram logados quando isto é levantado."""


def _report_module_failure(bot_name, filename: str, error: BaseException) -> None:
    """Falha de cog: módulo e tipo da exceção em WARNING (sem traceback); o traceback só em DEBUG."""
    BootReport().step(f"Cog {filename}.py de {bot_name}", "warn", f"falhou ao carregar ({type(error).__name__})")
    logger.debug("Detalhe da falha do módulo %s", filename, exc_info=(type(error), error, error.__traceback__))


def parse_owner_ids(raw: Optional[str]) -> list[int]:
    """IDs numéricos de ``OWNER_IDS`` (separados por ``||``); vazios são ignorados e inválidos, descartados com aviso."""
    ids = []
    for item in (raw or "").split("||"):
        item = item.strip()
        if not item:
            continue
        try:
            ids.append(int(item))
        except ValueError:
            logger.warning("Owner_ID inválido: %s", item)
    return ids


def load_jishaku_if_enabled(bot) -> bool:
    """Carrega a extensão jishaku (eval e shell do owner) só com ``ENABLE_JISHAKU=true``; desligada por padrão."""
    if not bot.config["ENABLE_JISHAKU"]:
        return False
    # Sem OWNER_IDS o fork cairia no is_owner da aplicação e liberaria a equipe do app: nesse caso não carrega.
    owner_ids = parse_owner_ids(bot.config.get("OWNER_IDS"))
    if not owner_ids:
        logger.warning(
            "ENABLE_JISHAKU=true, mas OWNER_IDS não tem nenhum ID válido: o jishaku não foi carregado. "
            "Defina OWNER_IDS (inclusive o seu ID) para restringir quem pode usá-lo."
        )
        return False
    # Endurecimento do fork empiremuse-jishaku; setdefault preserva o que o operador definiu no ambiente.
    os.environ.setdefault("JISHAKU_MASK_SECRETS", "true")
    os.environ.setdefault("JISHAKU_DM_ONLY", "true")
    os.environ["JISHAKU_OWNER_IDS"] = ",".join(str(i) for i in owner_ids)
    bot.load_extension("jishaku")
    return True


class BotPool:
    killing_state = False
    # Defaults de classe (como o killing_state): fakes de BotPool criados com __new__ não passam pelo __init__.
    update_ready_checked = False  # on_pool_ready roda uma vez, na primeira vez em que o pool fica saudável
    update_watch_task: Optional[asyncio.Task] = None
    usage: Optional[UsageRecorder] = None
    guild_bans: Optional[GuildBans] = None
    servers: Optional[ServerStore] = None
    request_panel: Optional[RequestPanel] = None
    radio: RadioHub
    join_notices: Optional[TTLCache] = None
    logs_task: Optional[asyncio.Task] = None
    version_check_task: Optional[asyncio.Task] = None
    playlist_icons_task: Optional[asyncio.Task] = None
    helper_watch_tasks: tuple[asyncio.Task, ...] | set[asyncio.Task] = ()
    command_sync_config = commands.CommandSyncFlags(
        allow_command_deletion=True,
        sync_commands=True,
        sync_commands_debug=True,
        sync_global_commands=True,
        sync_guild_commands=True,
    )

    add_fav_embed_cooldown = commands.CooldownMapping.from_cooldown(rate=1, per=15, type=commands.BucketType.user)

    enqueue_track_embed_cooldown = commands.CooldownMapping.from_cooldown(rate=2, per=9, type=commands.BucketType.user)

    enqueue_playlist_embed_cooldown = commands.CooldownMapping.from_cooldown(
        rate=1, per=14, type=commands.BucketType.user
    )

    song_select_cooldown = commands.CooldownMapping.from_cooldown(rate=2, per=15, type=commands.BucketType.member)

    def __init__(self):
        self.bots: List[BotCore] = []
        self.guild_bots: Dict[str, List[BotCore]] = {}
        self.exit_code: int = 0
        self.user_prefix_cache = {}
        self.guild_prefix_cache = {}
        self.mongo_database: Optional[MongoDatabase] = None
        self.local_database: Optional[LocalDatabase] = None
        self.ws_client: Optional[WSClient] = None
        self.emoji_data = {}
        self.config = self.load_cfg()
        self.playlist_cache = TTLCache(
            maxsize=self.config["PLAYLIST_CACHE_SIZE"], ttl=self.config["PLAYLIST_CACHE_TTL"]
        )
        self.partial_track_cache = TTLCache(maxsize=1000, ttl=80400)
        self.integration_cache = TTLCache(maxsize=500, ttl=7200)
        self.spotify: Optional[SpotifyClient] = None
        self.spotify_token_task: Optional[asyncio.Task] = None
        self.deezer = DeezerClient(self.playlist_cache)
        self.lavalink_instance: Optional[subprocess.Popen] = None
        self.lavalink_watcher: Optional[threading.Thread] = None
        self.lavalink_exit_future: Optional[asyncio.Future] = None
        self.commit = ""
        self.remote_git_url = ""
        self.max_counter: int = 0
        self.message_ids = TTLCache(ttl=30, maxsize=20000)
        self.bot_mentions = set()
        self.single_bot = True
        self.loop: Optional[asyncio.EventLoop] = None
        self.failed_bots: dict = {}
        self.current_useragent = self.reset_useragent()
        self.processing_gc: bool = False
        self.lavalink_connect_queue = {}
        self.last_fm: Optional[LastFM] = None
        self.lastfm_sessions = {}
        self.player_skins = {}
        self.player_static_skins = {}
        self.default_skin = self.config.get("DEFAULT_SKIN", "default")
        self.default_static_skin = self.config.get("DEFAULT_STATIC_SKIN", "default")
        self.default_controllerless_skin = self.config.get("DEFAULT_CONTROLLERLESS_SKIN", "default")
        self.default_idling_skin = self.config.get("DEFAULT_IDLING_SKIN", "default")
        self.cache_updater_task: Optional[asyncio.Task] = None
        self.heartbeat_task: Optional[asyncio.Task] = None
        self.metrics = MetricsCollector()
        self.usage = UsageRecorder(self)
        self.guild_bans = GuildBans(self)
        self.i18n = get_i18n()
        self.i18n.pool = self
        self.servers = ServerStore(self)
        self.request_panel = RequestPanel(self)
        self.radio = RadioHub(self)
        # servidor -> aviso de "sem o principal" já enviado; a validade de 24 h é o limitador único (um aviso por servidor no pool)
        self.join_notices = TTLCache(maxsize=5000, ttl=NOTICE_WINDOW_SECONDS)
        self.logs_task: Optional[asyncio.Task] = None
        self.helper_watch_tasks: set[asyncio.Task] = set()  # referências fortes das tarefas do watch_helper
        # Uma atualização por vez no processo todo: o max_concurrency do comando vale só por bot (V10).
        self.update_lock = asyncio.Lock()
        self.nodes_expected = False  # vira True quando o pool configura ao menos um nó (local ou da lista)
        self.lyric_data_cache = TTLCache(maxsize=30000, ttl=600 * 10)
        self.ytdl = CustomYTDL(
            {
                "format": "webm[abr>0]/bestaudio/best",
                "extract_flat": True,
                "quiet": True,
                "no_warnings": True,
                "lazy_playlist": True,
                "playlist_items": "1-700",
                "simulate": True,
                "download": False,
                "cachedir": False,
                "allowed_extractors": [
                    r".*youtube.*",
                    r".*soundcloud.*",
                ],
                "extractor_args": {
                    "youtube": {
                        "player_client": [
                            "web",
                            "android",
                            "android_creator",
                            "web_creator",
                        ],
                        "max_comments": [0],
                    },
                    "youtubetab": {"skip": ["webpage", "authcheck"]},
                },
            }
        )

        self.load_cache()

    def reset_useragent(self) -> str:
        self.current_useragent = generate_user_agent()
        return self.current_useragent

    def load_cache(self):
        """O cache de playlist volta do JSON; os caches do formato antigo não são lidos."""
        self.playlist_cache.update(
            load_json_cache(_playlist_cache_file(), max_bytes=PLAYLIST_MAX_BYTES, max_age=self.playlist_cache.ttl)
        )

    async def cache_updater(self):
        while True:
            await asyncio.sleep(300)
            items = list(self.playlist_cache.items())  # a cópia sai no laço; a serialização e a escrita, fora dele
            try:
                await get_running_loop().run_in_executor(
                    None, partial(save_json_cache, _playlist_cache_file(), items, max_bytes=PLAYLIST_MAX_BYTES)
                )
            except OSError as e:
                logger.warning("Cache de playlists não gravado: %s", type(e).__name__)

    def pool_is_healthy(self) -> bool:
        """Bots ativos todos prontos e, havendo nós configurados, ao menos um nó disponível em qualquer bot."""
        nodes_available = sum(
            1 for bot in self.get_all_bots() for node in bot.music.nodes.values() if node.is_available
        )
        return is_healthy([bot.bot_ready for bot in self.bots], self.nodes_expected, nodes_available)

    async def check_pool_ready(self) -> None:
        """Na primeira vez em que o pool fica saudável chama ``on_pool_ready`` (update: pendente vira ``ready`` e o aviso sai)."""
        if self.update_ready_checked or self.killing_state is True or not self.pool_is_healthy():
            return
        self.update_ready_checked = True
        try:
            await on_pool_ready(self)
        except Exception:
            logger.warning("Falha ao entregar o aviso de atualização", exc_info=True)

    async def _watch_pool_ready(self) -> None:
        """Reavalia o pool até ele ficar saudável: os nós conectam depois do ``initial_setup``; o evento é por bot e a disponibilidade pode oscilar, então o polling é mais simples."""
        while not self.update_ready_checked:
            await asyncio.sleep(UPDATE_WATCH_INTERVAL)
            await self.check_pool_ready()

    def start_update_watch(self) -> None:
        """Só com um pendente de atualização em disco: acompanha o pool até a subida ser dada como boa."""
        if read_pending(paths.get_runtime_dir(), time.time()) is not None:
            self.update_watch_task = self.loop.create_task(self._watch_pool_ready())

    def start_heartbeat(self) -> None:
        """Com ``EMPIREMUSE_HEALTH_FILE`` definida: apaga o heartbeat herdado (se não conseguir, encerra) e inicia a tarefa que o renova."""
        path = os.environ.get(HEALTH_FILE_ENV)
        if not path:
            return
        try:
            clear_heartbeat(path)
        except OSError as e:
            # Falha fechada: um heartbeat herdado com menos de 60 s passaria no HEALTHCHECK antes de qualquer bot ficar pronto.
            raise SystemExit(
                f"Não foi possível apagar o heartbeat anterior ({path}): {e.strerror}. "
                "Corrija as permissões do arquivo ou da pasta, ou apague-o à mão."
            ) from e
        self.heartbeat_task = self.loop.create_task(self.heartbeat_loop(path))

    async def heartbeat_loop(self, path: str, *, clock=time.time, sleep=asyncio.sleep):
        """Renova o heartbeat a cada ``HEARTBEAT_INTERVAL`` s, só com o pool saudável (o relógio e o sleep são injetáveis)."""
        while True:
            if self.pool_is_healthy():
                try:
                    await asyncio.to_thread(write_heartbeat, path, clock())
                except OSError as e:
                    logger.warning("Heartbeat não gravado: %s", type(e).__name__)
            await sleep(HEARTBEAT_INTERVAL)

    def start_logs_task(self) -> None:
        """Tarefa de manutenção do log (K9): sempre criada; ``METRICS_INTERVAL=0`` só desliga o resumo."""
        self.logs_task = self.loop.create_task(self.logs_loop())

    async def logs_loop(self, *, clock=time.monotonic, sleep=asyncio.sleep, process_stats=metrics_process_stats):
        """A cada ``AGGREGATE_WINDOW`` fecha as janelas do agregador e amostra o pico; a cada ``metrics_interval`` loga o resumo.

        ``process_stats(pid=None)`` devolve ``(cpu_percent, rss_mib)`` do processo Python (sem ``pid``) e do Java do Lavalink local
        (com o ``pid``); injetável como o relógio e o ``sleep``.
        """
        interval = parse_log_settings(self.config, os.environ)[0].metrics_interval
        summary_logger = logging.getLogger("empiremuse.metrics")
        last_summary = clock()
        while True:
            await sleep(AGGREGATE_WINDOW)
            try:
                tick_logging()
                self.metrics.sample_players(self._count_active_players())
                now = clock()
                if interval > 0 and now - last_summary >= interval:
                    last_summary = now
                    snapshot = self.metrics.snapshot_and_reset(now)
                    # As amostras dos bots, players e nós são montadas AQUI, na thread do loop (que é quem os muda); só a
                    # leitura do psutil, que bloqueia, vai para uma thread.
                    inputs = self._collect_summary_inputs()
                    java_pid = inputs.pop("java_pid")
                    process, java = await asyncio.to_thread(_read_process_stats, process_stats, java_pid)
                    inputs["cpu_percent"], inputs["rss_mib"] = process
                    if java is not None:
                        inputs["nodes"] = _with_java_figures(inputs["nodes"], *java)
                    for line in format_summary(snapshot, **inputs):
                        # O 📊 é do formatter do console (emoji da categoria empiremuse.metrics); o arquivo não leva emoji.
                        summary_logger.info("%s", line.removeprefix(SUMMARY_PREFIX))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Falha na tarefa de manutenção do log (%s).", type(e).__name__)

    def _count_active_players(self) -> int:
        return sum(len(bot.music.players) for bot in self.get_all_bots() if getattr(bot, "music", None))

    def _collect_summary_inputs(self) -> dict:
        """Amostras do resumo, só do que já está em memória (``bot.latency``, ``player.ping``, ``node.stats``).

        Roda na thread do loop. O ``cpu_percent`` e o ``rss_mib`` do processo vêm depois, do ``psutil`` (``java_pid`` é o do Lavalink local).
        """
        bots = [bot for bot in self.get_all_bots() if getattr(bot, "music", None)]
        gateway_ms, voice_ms, players_active = [], [], 0
        nodes: dict[str, NodeSample] = {}
        for bot in bots:
            latency = getattr(bot, "latency", None)
            if isinstance(latency, int | float) and latency == latency and latency != float("inf"):
                gateway_ms.append(latency * 1000)
            for player in bot.music.players.values():
                players_active += 1
                if isinstance(getattr(player, "ping", None), int | float):
                    voice_ms.append(float(player.ping))
            for identifier, node in bot.music.nodes.items():
                if identifier not in nodes:
                    nodes[identifier] = _node_sample(identifier, node)
        java_pid = getattr(getattr(self, "lavalink_instance", None), "pid", None)
        return {
            "java_pid": java_pid if java_pid and "LOCAL" in nodes else None,
            "guilds": len({getattr(guild, "id", guild) for bot in bots for guild in bot.guilds}),
            "players_active": players_active,
            "gateway_ms": gateway_ms,
            "voice_ms": voice_ms,
            "cpu_percent": None,
            "rss_mib": None,
            "nodes": list(nodes.values()),
            "radios": tuple(self.radio.manager.counts()) if getattr(self, "radio", None) else None,
        }

    async def connect_lavalink_queue_task(self, identifier: str):
        delay_secs = int(self.config.get("LAVALINK_QUEUE_DELAY", 1.5))

        try:
            while True:
                async with timeout(600):
                    bot, data = await self.lavalink_connect_queue[identifier].get()
                    await bot.get_cog("Music").connect_node(data)
                    await asyncio.sleep(delay_secs)
        except asyncio.TimeoutError:
            pass

    def get_guild_bots(self, guild_id: int) -> list:
        return self.bots + self.guild_bots.get(str(guild_id), [])

    @property
    def main_configured(self) -> bool:
        """Há um token principal configurado (logado, logando ou que falhou)? Sem ele ninguém é reserva: todos agem como sempre."""
        return any(is_main_identifier(bot.identifier) for bot in self.bots) or any(
            is_main_identifier(identifier) for identifier in getattr(self, "failed_bots", {})
        )

    @property
    def main_bot(self) -> Optional[BotCore]:
        """O bot principal (token principal, logado) ou ``None``. Os bots exclusivos de servidor ficam fora."""
        return self.servers.main_bot() if self.servers is not None else None

    def main_can_serve(self, guild_id: int, channel_id: Optional[int] = None) -> bool:
        """O principal está no servidor (e, com ``channel_id``, enxerga e escreve no canal) e pronto para responder?"""
        main = self.main_bot
        if main is None or not main.bot_ready or main.is_closed():
            return False
        guild = main.get_guild(guild_id)
        if guild is None:
            return False
        if channel_id is None:
            return True
        channel = guild.get_channel_or_thread(channel_id)
        if channel is None:
            return False
        if isinstance(channel, disnake.Thread):
            return channel.parent.permissions_for(guild.me).send_messages_in_threads
        return channel.permissions_for(guild.me).send_messages

    async def wait_main_outcome(self, timeout: float = 120.0) -> bool:
        """Espera o principal logar (``True``) ou falhar/não existir (``False``); o fim do prazo conta como ``False``."""
        deadline = self.loop.time() + timeout
        while self.main_bot is None:
            still_logging_in = any(is_main_identifier(bot.identifier) for bot in self.bots)
            if not still_logging_in or self.loop.time() >= deadline:
                return False
            await asyncio.sleep(1)
        return True

    def get_all_bots(self) -> list:
        allbots = set()

        for botlist in self.guild_bots.values():
            allbots.update(botlist)

        allbots.update(self.bots)

        return list(allbots)

    @property
    def database(self) -> Union[LocalDatabase, MongoDatabase]:
        if self.config["MONGO"]:
            return self.mongo_database

        return self.local_database

    def stop_lavalink(self) -> None:
        """Encerra o Java do Lavalink local de forma esperada: o vigia não trata essa saída como queda."""
        process, self.lavalink_instance = self.lavalink_instance, None
        if not process:
            return

        try:
            process.kill()
        except OSError:
            logger.warning("Falha ao finalizar o processo do Lavalink local", exc_info=True)

    async def _handle_lavalink_exit(self, process: subprocess.Popen, returncode: int):
        """Roda no loop: só uma saída inesperada do Java vigente derruba o bot (shutdown com código 1)."""
        if process is not self.lavalink_instance or self.killing_state:
            return

        logger.error("O processo do Lavalink local encerrou inesperadamente (código %s); encerrando o bot.", returncode)
        await self.shutdown(1)

    def _on_lavalink_exit(self, process: subprocess.Popen, returncode: int) -> None:
        """Chamada na thread do vigia: devolve a decisão ao loop do bot e guarda a referência da tarefa."""
        coroutine = self._handle_lavalink_exit(process, returncode)
        try:
            self.lavalink_exit_future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        except RuntimeError:
            coroutine.close()
            logger.warning(
                "O loop do bot já foi encerrado; a saída do Lavalink local (código %s) foi ignorada.", returncode
            )

    def _watch_lavalink(self, process: subprocess.Popen) -> None:
        self.lavalink_watcher = threading.Thread(
            target=watch_lavalink_process,
            args=(process, lambda returncode: self._on_lavalink_exit(process, returncode)),
            name="lavalink-watcher",
            daemon=True,
        )
        self.lavalink_watcher.start()

    async def start_lavalink(self):
        self.stop_lavalink()

        if not self.loop:
            self.loop = asyncio.get_running_loop()

        try:
            process = await self.loop.run_in_executor(
                None,
                lambda: run_lavalink(
                    lavalink_file_url=self.config["LAVALINK_FILE_URL"],
                    lavalink_file_sha256=self.config["LAVALINK_FILE_SHA256"],
                    youtube_oauth_refresh_token=self.config["YOUTUBE_OAUTH_REFRESH_TOKEN"],
                    lavalink_initial_ram=self.config["LAVALINK_INITIAL_RAM"],
                    lavalink_ram_limit=self.config["LAVALINK_RAM_LIMIT"],
                    lavalink_cpu_cores=self.config["LAVALINK_CPU_CORES"],
                    lavalink_additional_sleep=int(self.config["LAVALINK_ADDITIONAL_SLEEP"]),
                    lavalink_java_path=self.config["LAVALINK_JAVA_PATH"],
                    lavalink_home=self.config["LAVALINK_HOME"],
                    lavalink_offline=self.config["LAVALINK_OFFLINE"],
                    report=BootReport(),
                ),
            )
        except Exception:
            logger.exception("Falha ao iniciar o Lavalink local")
            return

        if self.killing_state:  # o bot começou a encerrar enquanto o Java subia: não deixa o processo órfão
            process.kill()
            return

        self.lavalink_instance = process
        self._watch_lavalink(process)

    async def start_bot(self, bot: BotCore):
        e = None

        try:
            await bot.start(bot.http.token)
        except disnake.HTTPException as error:
            if error.status == 429 or "429 Too Many Requests" in str(error):
                if not self.config["KILL_ON_429"]:
                    if self.killing_state == "ratelimit":
                        return

                    self.killing_state = "ratelimit"
                    logger.warning("Aplicação com ratelimit do discord!")
                    await asyncio.sleep(10)
                    raise error

                if self.killing_state is True:
                    return

                logger.warning("Aplicação com ratelimit do discord! Finalizando o processo em 5 segundos...")

                self.killing_state = True

                await asyncio.sleep(5)

                await self.shutdown(1)

                return

            e = error

        except Exception as error:
            e = error

        if e:
            if isinstance(e, disnake.PrivilegedIntentsRequired):
                e = (
                    "Você não ativou as Privileged Intents na sua aplicação<br>"
                    "Acesse o discord developer portal:<br>"
                    "https://discord.com/developers/applications/<br>"
                    'e clique na sua aplicação e depois clique no menu "bot"<br>'
                    "e em seguida ative todas as intents.<br>"
                    f"Print de exemplo: {branding.asset_url('tutorial_intents_guide.png')}<br>"
                    "Após corrigir, reinicie a aplicação."
                )

                logger.error("Falha ao iniciar o bot configurado no %s:\n%s", bot.identifier, e.replace("<br>", "\n"))

            elif isinstance(e, disnake.LoginFailure) and "Improper token" in str(e):
                e = (
                    "Foi utilizado um token inválido.<br>"
                    "Revise se o token informado está correto<br>"
                    "ou se o token foi resetado<br>"
                    f"ou copiado do local correto ( ex: {branding.asset_url('tutorial_token_guide.png')} )<br>"
                    "Após corrigir, reinicie a aplicação."
                )

                logger.error("Falha ao iniciar o bot configurado no %s:\n%s", bot.identifier, e.replace("<br>", "\n"))

            else:
                code = log_unexpected_error(logger, e, f"inicialização do bot {bot.identifier}")
                e = f"Falha ao iniciar o bot (veja o log do servidor{code_suffix(code)})."
            self.failed_bots[bot.identifier] = e
            try:
                self.bots.remove(bot)
            except Exception:
                pass
            await self.check_pool_ready()  # o pool pode ter ficado saudável sem este bot

    async def run_bots(self, bots: List[BotCore]):
        await asyncio.gather(*[self.start_bot(bot) for bot in bots])

    async def shutdown(self, exit_code: int = 1):
        self.exit_code = exit_code
        self.killing_state = True
        if self.heartbeat_task:  # encerrando: o container não deve mais parecer saudável
            self.heartbeat_task.cancel()
        if self.logs_task:
            self.logs_task.cancel()
        if self.update_watch_task:
            self.update_watch_task.cancel()
        if self.version_check_task:
            self.version_check_task.cancel()
        if self.playlist_icons_task:
            self.playlist_icons_task.cancel()
        for task in list(self.helper_watch_tasks):
            task.cancel()
        if self.request_panel:
            self.request_panel.close()
        self.i18n.close()  # leituras de idioma que ficaram pendentes
        for bot in self.get_all_bots():
            try:
                if not bot.is_closed():
                    await bot.close()
            except Exception:
                logger.warning(
                    "Falha ao fechar o bot %s durante o encerramento",
                    getattr(bot, "identifier", bot),
                    exc_info=True,
                )

        if self.lavalink_instance:
            try:
                self.lavalink_instance.kill()
            except Exception:
                logger.warning(
                    "Falha ao finalizar o processo do Lavalink local durante o encerramento",
                    exc_info=True,
                )

        if self.usage is not None:
            await self.usage.close()  # grava o que sobrou no buffer do registro de uso

        flush_logging()  # por último: sai a contagem dos repetidos que ainda estava pendente

    async def connect_node(self, bot: BotCore, data: dict):
        while not bot.bot_ready:
            await asyncio.sleep(3)

        music_cog = bot.get_cog("Music")
        if music_cog:
            await music_cog.connect_node(data)

    async def check_node(self, data: dict):
        data = deepcopy(data)

        data["rest_uri"] = ("https" if data.get("secure") else "http") + f"://{data['host']}:{data['port']}"

        try:
            max_retries = int(data.get("retries")) or 1
        except TypeError, KeyError:
            max_retries = 1

        headers = {"Authorization": data["password"]}

        backoff = 9
        retries = 0
        exception = None

        node_logger.info("Verificando se o servidor de música [%s] está disponível.", data["identifier"])

        while True:
            if retries >= max_retries:
                node_logger.error(
                    "Todas as tentativas de verificar o servidor [%s] falharam. Causa: %s",
                    data["identifier"],
                    safe_error(exception) if exception else "desconhecida",
                )
                return
            else:
                await asyncio.sleep(backoff)
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(f"{data['rest_uri']}/v4/info", timeout=45, headers=headers) as r:
                            if r.status == 200:
                                data["info"] = await r.json()
                                data["info"]["check_version"] = 4
                            elif r.status == 403:
                                node_logger.error("Lavalink Server [%s] - Senha incorreta!", data["identifier"])
                                return
                            elif r.status != 404:
                                raise HTTPStatusError(r.status)
                            else:
                                data["info"] = {"check_version": 3, "sourceManagers": ["youtube", "soundcloud", "http"]}
                            break
                except Exception as e:
                    exception = e
                    if data["identifier"] != "LOCAL":
                        node_logger.warning(
                            "Falha ao verificar o servidor [%s], nova tentativa [%s/%s] em %s segundos.",
                            data["identifier"],
                            retries,
                            max_retries,
                            backoff,
                        )
                    backoff += 2
                    retries += 1

        if (
            data["identifier"] == "LOCAL"
            and self.mongo_database
            and data["info"]["check_version"] > 3
            and [i for i in data["info"].get("plugins", {}) if i["name"] == "youtube-plugin"]
        ):
            try:
                mongo_data = (
                    await self.mongo_database._connect["global"]["global"].find_one({"_id": "youtube_data"}) or {}
                )
            except Exception:
                node_logger.warning("Falha ao ler os refresh tokens do Youtube no banco de dados.", exc_info=True)
            else:
                if tokens := mongo_data.get("refresh_tokens"):
                    for v in tokens.values():
                        if isinstance(v, str):
                            register_secret(v)
                        try:
                            async with ClientSession() as session:
                                resp = await session.post(
                                    f"{data['rest_uri']}/youtube", headers=headers, json={"refreshToken": v}
                                )
                                if resp.status != 204:
                                    resp.raise_for_status()
                        except Exception as e:
                            node_logger.warning(
                                "Falha ao aplicar o Youtube refreshToken no servidor lavalink: %s - %s",
                                data["identifier"],
                                safe_error(e),
                            )
                            break
                        else:
                            node_logger.info(
                                "Youtube refreshToken aplicado no servidor lavalink: %s", data["identifier"]
                            )
                            break

        for bot in self.get_all_bots():
            self.loop.create_task(self.connect_node(bot, data))
            await asyncio.sleep(1)

    def node_check(self, lavalink_servers: dict, start_local=True):
        self.nodes_expected = bool(start_local or lavalink_servers)

        if start_local and "LOCAL" not in lavalink_servers:
            localnode = {
                "host": "127.0.0.1",
                "port": os.environ.get("SERVER_PORT") or 8090,
                "password": LOCAL_LAVALINK_PASSWORD,
                "identifier": "LOCAL",
                "retries": 120,
                "prefer_youtube_native_playback": self.config["PREFER_YOUTUBE_NATIVE_PLAYBACK"],
                "only_use_native_search_providers": self.config["ONLY_USE_NATIVE_SEARCH_PROVIDERS"],
                "search_providers": self.config["SEARCH_PROVIDERS"].strip().split()
                or ["ytsearch", "ytmsearch", "scsearch"],
            }
            self.loop.create_task(self.check_node(localnode))

        for data in lavalink_servers.values():
            self.loop.create_task(self.check_node(data))

    def process_track_cls(self, data: list, playlists: dict = None):
        if not playlists:
            playlists = {}

        tracks = []

        for info in data:
            if playlist := info.pop("playlist", None):
                try:
                    playlist = playlists[playlist["url"]]
                except KeyError:
                    playlist_cls = LavalinkPlaylist(
                        {
                            "loadType": "PLAYLIST_LOADED",
                            "playlistInfo": {"name": playlist["name"], "selectedTrack": -1},
                            "tracks": [],
                        },
                        url=playlist["url"],
                    )
                    playlists[playlist["url"]] = playlist_cls
                    playlist = playlist_cls

            if info["sourceName"] not in native_sources:
                try:
                    del info["id"]
                except KeyError:
                    pass

            if info.get("is_partial"):
                track = PartialTrack(info=info)
            else:
                track = LavalinkTrack(
                    id_=info.get("id", ""),
                    info=info,
                    playlist=playlist,
                    requester=info["extra"]["requester"],
                    pluginInfo=info.get("pluginInfo", {}),
                )
            tracks.append(track)

        return tracks, playlists

    async def connect_rpc_ws(self):
        if not self.config["RUN_RPC_SERVER"] and (
            not self.config["RPC_SERVER"]
            or self.config["RPC_SERVER"].replace("$PORT", port := str(resolve_port(self.config.get("PORT"))))
            == f"ws://localhost:{port}/ws"
        ):
            pass
        else:
            await self.ws_client.ws_loop()

    def load_cfg(self):
        config = load_config()

        emojis_path = paths.RUNTIME_DIR / "emojis.json"
        if not emojis_path.is_file():
            emojis_path = paths.PROJECT_DIR / "emojis.json"
        try:
            with open(emojis_path) as f:
                self.emoji_data = json.load(f)
        except FileNotFoundError:
            pass
        except Exception:
            logger.exception("Falha ao ler o emojis.json")

        if not config["DEFAULT_PREFIX"]:
            config["DEFAULT_PREFIX"] = "!!"

        return config

    def load_skins(self):
        skins_dir = Path(__file__).resolve().parent / "music" / "skins"
        for skin in os.listdir(skins_dir / "normal_player"):
            if not skin.endswith(".py"):
                continue

            skin = skin[:-3]

            if skin in self.config["IGNORE_SKINS"].split() and skin != "default":
                logger.info("Skin %s.py ignorada", skin)
                continue

            try:
                skin_file = import_module(f"empiremuse.utils.music.skins.normal_player.{skin}")
                if not hasattr(skin_file, "load"):
                    logger.warning("Skin ignorada: %s.py | Função load() não configurada/encontrada...", skin)
                    continue
                self.player_skins[skin] = skin_file.load()
            except Exception as e:
                logger.warning("Falha ao carregar skin [normal_player] %s (%s)", skin, type(e).__name__)
                logger.debug("Detalhe da falha da skin %s", skin, exc_info=True)

        if self.default_skin not in self.player_skins:
            self.default_skin = "default"

        for skin in os.listdir(skins_dir / "static_player"):
            if not skin.endswith(".py"):
                continue

            skin = skin[:-3]

            if skin in self.config["IGNORE_STATIC_SKINS"].split() and skin != "default":
                logger.info("Skin %s.py ignorada", skin)
                continue

            try:
                skin_file = import_module(f"empiremuse.utils.music.skins.static_player.{skin}")
                if not hasattr(skin_file, "load"):
                    logger.warning("Skin ignorada: %s.py | Função load() não configurada/encontrada...", skin)
                    continue
                self.player_static_skins[skin] = skin_file.load()
            except Exception as e:
                logger.warning("Falha ao carregar skin [static_player] %s (%s)", skin, type(e).__name__)
                logger.debug("Detalhe da falha da skin %s", skin, exc_info=True)
        if self.default_static_skin not in self.player_static_skins:
            self.default_static_skin = "default"

    def check_skin(self, skin: str):
        if skin is None:
            return self.default_skin

        if skin.startswith("> custom_skin: "):
            return skin

        if skin not in self.player_skins:
            return self.default_skin

        return skin

    def check_static_skin(self, skin: str):
        if skin is None:
            return self.default_static_skin

        if skin.startswith("> custom_skin: "):
            return skin

        if skin is None or skin not in self.player_static_skins:
            return self.default_static_skin

        return skin

    async def refresh_spotify_token(self) -> None:
        """Tarefa de partida: obtém o token do Spotify e registra a falha em vez de deixá-la solta na task."""
        try:
            await self.spotify.get_access_token()
        except GenericError as e:
            logger.error("Spotify: não foi possível obter o token de acesso na partida: %s", e.text)
        except (aiohttp.ClientError, OSError, KeyError, ValueError) as e:
            logger.error("Spotify: falha ao obter o token de acesso na partida (%s).", type(e).__name__, exc_info=True)

    async def migrate_playlist_icons(self):
        """Migração de boot das playlists já gravadas: o emoji do início do nome vira ``icon`` (idempotente; falha só vai ao log).

        Os servidores alterados têm o painel do canal de pedidos redesenhado quando os bots ficam prontos (a mensagem montada antes
        da migração guarda o nome antigo no valor do select); antes disso não há bot do painel, e o redesenho seria perdido.
        """
        try:
            *_, changed = await self.servers.playlists.migrate_icons()
            if changed:
                ready = asyncio.gather(*(bot.wait_until_ready() for bot in self.get_all_bots()))
                try:
                    await asyncio.wait_for(ready, PLAYLIST_REFRESH_READY_WAIT)
                except asyncio.TimeoutError:
                    pass  # o que não ficou pronto é pulado pelo próprio painel
                for gid in changed:
                    self.servers.playlists.refresh_idle(gid)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Falha ao migrar os ícones das playlists")

    async def setup_pool_extras(self):
        try:
            from dev.pool_dev import PoolDev

            await PoolDev(self).run()
        except ImportError:
            pass
        except Exception:
            logger.exception("Falha ao executar o pool de desenvolvimento")

    def setup(self):
        # As etapas que aconteceram antes do log (pasta de execução, Python) vêm do __main__ e saem logo depois do configure_logging.
        boot = getattr(self, "boot_report", None) or BootReport()

        log_settings, log_warnings = parse_log_settings(self.config, os.environ)
        configure_logging(log_settings, collect_secret_values(self.config, os.environ))
        boot.flush_deferred()
        installed_version = image_version()
        boot.step("Versão do bot", "ok", str(installed_version) if installed_version else "fora da imagem Docker")
        for log_warning in log_warnings:  # logados só agora, já com o log configurado
            boot.step("Configuração do log", "warn", log_warning)

        self.load_skins()
        # O collect_secret_values só vê o valor INTEIRO de variáveis com nome de segredo: os tokens dos bots de servidor
        # (guild_bots.json) são registrados à parte, e as senhas dos nós mais abaixo, depois de montada a lista.
        for guild_bot_tokens in read_guild_bots().values():
            for guild_bot_token in guild_bot_tokens:
                register_secret(guild_bot_token)

        log_ignored_legacy_data()  # depois do arquivo de log, para o aviso constar nele

        LAVALINK_SERVERS = {}

        if self.config["AUTO_DOWNLOAD_LAVALINK_SERVERLIST"]:
            try:
                LAVALINK_SERVERS = get_lavalink_servers()
                ini_file = None
            except Exception:
                logger.exception("Falha ao obter a lista de servidores lavalink")
                ini_file = str(paths.RUNTIME_DIR / "auto_lavalink.ini")
                logger.info("Baixando lista de servidores lavalink (arquivo: %s)", ini_file)
                try:
                    r = requests.get(self.config["LAVALINK_SERVER_LIST"], allow_redirects=False, timeout=30)
                    server_list = validated_server_list(r.status_code, r.content)
                    if server_list is not r.content:
                        logger.warning(
                            "Lista de servidores lavalink baixada é inválida, usando o arquivo local do repositório."
                        )
                    r.close()
                except Exception:
                    logger.exception(
                        "Falha ao baixar a lista de servidores lavalink, usando o arquivo local do repositório."
                    )
                    server_list = validated_server_list(None, None)
                with open(paths.RUNTIME_DIR / "auto_lavalink.ini", "wb") as f:
                    f.write(server_list)
        else:
            ini_file = str(paths.RUNTIME_DIR / "lavalink.ini")

        for key, value in self.config.items():
            if key.lower().startswith("lavalink_node_"):
                try:
                    LAVALINK_SERVERS[key] = json.loads(value)
                except Exception as e:
                    logger.warning("Falha ao adicionar o nó %s (%s)", key, type(e).__name__)

        if ini_file:
            config = ConfigParser()
            try:
                config.read(ini_file)
            except FileNotFoundError:
                pass
            except Exception:
                logger.exception("Falha ao ler o arquivo de servidores lavalink")
            else:
                for key, value in {section: dict(config.items(section)) for section in config.sections()}.items():
                    value["identifier"] = key
                    value["secure"] = value.get("secure", "").lower() == "true"
                    value["port"] = value["port"].replace("{SERVER_PORT}", os.environ.get("SERVER_PORT") or "8090")
                    value["search"] = value.get("search", "").lower() != "false"
                    value["prefer_youtube_native_playback"] = (
                        value.get("prefer_youtube_native_playback", "").lower() == "true"
                    )
                    value["only_use_native_search_providers"] = (
                        value.get("only_use_native_search_providers", "").lower() == "true"
                    )
                    value["search_providers"] = value.get("search_providers", "").strip().split()
                    LAVALINK_SERVERS[key] = value

        # As 3 fontes (JSON do ambiente, lavalink.ini e lista baixada) já estão em LAVALINK_SERVERS neste ponto.
        for node in LAVALINK_SERVERS.values():
            if isinstance(node, dict) and isinstance(node.get("password"), str):
                register_secret(node["password"])

        if start_local := (self.config["RUN_LOCAL_LAVALINK"] is True or not LAVALINK_SERVERS):
            pass
        else:
            start_local = False

        intents = disnake.Intents(
            **{i[:-7].lower(): v for i, v in self.config.items() if i.lower().endswith("_intent")}
        )
        intents.members = True
        intents.guilds = True

        mongo_key = self.config.get("MONGO")

        if mongo_key:
            self.mongo_database = MongoDatabase(
                mongo_key,
                timeout=self.config["MONGO_TIMEOUT"],
                cache_maxsize=self.config["DBCACHE_SIZE"],
                cache_ttl=self.config["DBCACHE_TTL"],
            )
            boot.step("Banco de dados", "ok", "MongoDB")
        else:
            boot.step("Banco de dados", "ok", "TinyMongo (os arquivos da database ficam na pasta local_database)")

        self.local_database = LocalDatabase(
            cache_maxsize=self.config["DBCACHE_SIZE"], cache_ttl=self.config["DBCACHE_TTL"]
        )

        os.environ.update(
            {
                "GIT_DIR": self.config["GIT_DIR"],
                "JISHAKU_HIDE": "true",
                "JISHAKU_NO_UNDERSCORE": "true",
            }
        )

        # stderr descartado: fora de um repositório (ex.: modo .runtime) o git imprimiria "fatal: not a git repository".
        try:
            self.commit = check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL).decode("ascii").strip()
            boot.step("Commit", "ok", self.commit)
        except OSError, subprocess.CalledProcessError, UnicodeDecodeError:
            self.commit = None

        try:
            remotes = check_output(["git", "remote", "-v"], stderr=subprocess.DEVNULL).decode("utf-8", "replace")
            self.remote_git_url = parse_remote_web_url(remotes)
        except OSError, subprocess.CalledProcessError:
            pass  # sem git ou fora de um repositório: cai na configuração SOURCE_REPO logo abaixo

        if not self.remote_git_url:
            self.remote_git_url = sanitized_repo_url(self.config["SOURCE_REPO"], branding.REPO_URL)

        prefix = get_prefix if intents.message_content else commands.when_mentioned

        self.ws_client = WSClient(self.config["RPC_SERVER"], pool=self)

        try:
            spotify_client = SpotifyClient(
                client_id=self.config["SPOTIFY_CLIENT_ID"],
                client_secret=self.config["SPOTIFY_CLIENT_SECRET"],
                playlist_extra_page_limit=self.config["SPOTIFY_PLAYLIST_EXTRA_PAGE_LIMIT"],
            )
        except Exception as e:
            boot.step("Spotify interno", "warn", f"desativado ({type(e).__name__})")
            spotify_client = None

        self.spotify = spotify_client

        if self.config["LASTFM_KEY"] and self.config["LASTFM_SECRET"]:
            self.last_fm = LastFM(api_key=self.config["LASTFM_KEY"], api_secret=self.config["LASTFM_SECRET"])

        all_tokens = {}

        for k, v in dict(os.environ, **self.config).items():
            if not isinstance(v, str):
                continue

            if not (tokens := token_regex.findall(v)):
                continue

            if len(tokens) > 1:
                counter = 1
                for t in tokens:
                    if t in all_tokens.values():
                        continue

                    all_tokens[f"{k}_{counter}"] = t
                    counter += 1

            elif (token := tokens.pop()) not in all_tokens.values():
                all_tokens[k] = token

        def load_bot(bot_name: str, token: str, guild_id: str = None, load_modules_log: bool = False):
            try:
                token = token.split().pop()
            except AttributeError, IndexError:
                pass

            if not token:
                boot.step(f"Bot {bot_name}", "warn", "ignorado (token não informado)")
                return

            try:
                test_guilds = list([int(i) for i in self.config[f"TEST_GUILDS_{bot_name}"].split("||")])
            except Exception:
                test_guilds = None

            bot = BotCore(
                command_prefix=prefix,
                case_insensitive=True,
                intents=intents,
                identifier=bot_name,
                test_guilds=test_guilds,
                command_sync_flags=commands.CommandSyncFlags.none(),
                embed_color=self.config["EMBED_COLOR"],
                default_prefix=self.config["DEFAULT_PREFIX"],
                pool=self,
                number=int(self.max_counter),
                localization_provider=self.i18n.provider,
            )

            bot.http.token = token

            load_jishaku_if_enabled(bot)

            if bot.config["INTERACTION_COMMAND_ONLY"]:

                @bot.check
                async def check_commands(ctx: CustomContext):
                    # reserva num canal onde o principal não atende: não há comando de barra por perto, o prefixo é o único caminho
                    if bot.is_reserve and ctx.guild and not self.main_can_serve(ctx.guild.id, ctx.channel.id):
                        return True

                    if not (await bot.is_owner(ctx.author)):
                        raise GenericError(
                            "**Os comandos de texto estão desativados!\nUse os comandos de barra /**",
                            self_delete=True,
                            delete_original=15,
                        )

                    return True

            @bot.listen("on_resumed")
            async def clear_gc():
                if self.processing_gc:
                    return

                self.processing_gc = True
                await asyncio.sleep(2)
                gc.collect()
                self.processing_gc = False

            @bot.application_command_check(slash_commands=True, message_commands=True, user_commands=True)
            async def check(inter: disnake.ApplicationCommandInteraction):
                kwargs = {}

                try:
                    kwargs["only_voiced"] = inter.application_command.extras["only_voiced"]
                except KeyError:
                    pass

                try:
                    kwargs["check_player"] = inter.application_command.extras["check_player"]
                except KeyError:
                    pass

                try:
                    kwargs["return_first"] = inter.application_command.extras["return_first"]
                except KeyError:
                    pass

                try:
                    allow_private = inter.application_command.extras["allow_private"]
                except KeyError:
                    allow_private = False

                if inter.bot.exclusive_guild_id and inter.guild_id != inter.bot.exclusive_guild_id:
                    raise GenericError("Esse servidor não está autorizado para usar meus comandos...")

                # Um só gancho para o COMMAND_LOG (opt-in, com IDs e opções resumidas) e para o registro de uso (sempre ligado, só
                # o nome do comando e onde foi usado: nunca o usuário nem os valores das opções).
                usage = usage_of(inter.bot) if inter.guild else None
                log_command = bool(
                    self.config["COMMAND_LOG"] and inter.guild and not (await inter.bot.is_owner(inter.author))
                )
                if usage or log_command:
                    try:
                        # LGPD (B23): só IDs, o comando e as opções resumidas por tipo; nada de nome, conteúdo nem URL de anexo.
                        info = command_context(inter, level=logging.INFO)
                        if usage:
                            usage.command_started(KIND_SLASH, inter, info["command"], bot=inter.bot)
                        if log_command:
                            commands_logger.info(
                                "cmd log: [user: %s] - [guild: %s] - [cmd: %s] - %s",
                                info["user_id"],
                                info["guild_id"],
                                info["command"],
                                info["params"],
                            )
                    except Exception:
                        logger.exception("Falha ao registrar o log do comando")

                if not inter.guild_id:
                    if allow_private:
                        return True

                    raise GenericError(
                        "Esse comando não pode ser executado em mensagens privadas.\n"
                        "Use em algum servidor onde há bot compatível adicionado."
                    )

                if not allow_private and not isinstance(inter.guild, disnake.Guild):
                    available_bot = False

                    for b in inter.bot.pool.get_guild_bots(inter.guild_id):
                        if (
                            b.appinfo
                            and (b.appinfo.bot_public or await b.is_owner(inter.author))
                            and b.get_guild(inter.guild_id)
                        ):
                            available_bot = True
                            break

                    if not available_bot:
                        raise GenericError(
                            "**Não há bots disponíveis no servidor, Adicione pelo menos um clicando no botão abaixo.**",
                            components=[disnake.ui.Button(custom_id="bot_invite", label="Adicionar bots")],
                        )

                if not kwargs:
                    kwargs["return_first"] = True

                try:
                    await check_pool_bots(inter, **kwargs)
                except Exception as e:
                    if not inter.guild_id:
                        bot.dispatch("custom_slash_command_error", inter, e, no_log=True)
                    raise e

                return True

            @bot.listen("on_command")
            async def command_hook(ctx: CustomContext):
                # Mesmo gancho do COMMAND_LOG (opt-in) e do registro de uso (sempre ligado, sem usuário nem opções).
                usage = usage_of(bot) if ctx.guild else None
                log_command = bool(self.config["COMMAND_LOG"] and not (await bot.is_owner(ctx.author)))
                if not (usage or log_command):
                    return

                info = command_context(ctx, level=logging.INFO)
                if usage:
                    usage.command_started(KIND_PREFIX, ctx, info["command"], bot=bot)
                if log_command:
                    commands_logger.info(
                        "cmd (prefix) log: [user: %s] - [guild: %s] - [cmd: %s] - %s",
                        info["user_id"],
                        info["guild_id"],
                        info["command"],
                        info["params"],
                    )

            @bot.event
            async def on_ready():
                boot.step("Bot online", "ok", f"{bot.user} [{bot.user.id}]")

            async def initial_setup():
                await bot.wait_until_ready()

                if bot.session is None:
                    bot.session = aiohttp.ClientSession()

                bot.music.session = bot.session

                try:
                    bot.interaction_id = bot.user.id

                    bot.load_modules(load_modules_log=load_modules_log)

                    bot.sync_command_cooldowns()

                    if bot.config["AUTO_SYNC_COMMANDS"]:
                        await bot.sync_app_commands(force=True)

                    bot.add_view(PanelView(bot))

                    self.bot_mentions.update((f"<@!{bot.user.id}>", f"<@{bot.user.id}>"))

                except ModuleLoadError:
                    # O módulo e o tipo da falha já saíram em WARNING e o traceback em DEBUG: sem outro traceback em ERROR.
                    logger.warning(
                        "Configuração inicial do bot %s interrompida pela falha de um módulo.", bot.identifier
                    )
                except Exception:
                    logger.exception("Falha na configuração inicial do bot %s", bot.identifier)

                await bot.update_appinfo()

                await self.guild_bans.enforce_ready(bot)  # servidor banido que continuou na lista de servidores do bot

                bot.bot_ready = True

                await self.check_pool_ready()

                # depois do ready: não atrasa a inicialização, e a função não levanta
                await sync_app_profile(bot, len(self.get_all_bots()))

            bot.loop.create_task(initial_setup())

            if guild_id:
                bot.exclusive_guild_id = int(guild_id)
                self.guild_bots.setdefault(guild_id, []).append(bot)
            else:
                self.bots.append(bot)

        if len(all_tokens) > 1:
            self.single_bot = False

        load_modules_log = True

        if os.name != "nt":
            import uvloop

            self.loop = uvloop.new_event_loop()
        else:
            self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        install_exception_handlers(self.loop)  # exceção esquecida no laço vai ao log de erros, com o traceback

        self.start_heartbeat()
        self.start_logs_task()
        self.usage.start(self.loop)
        self.start_update_watch()

        # Catálogos de idioma: estritos (``en-US``/``pt-BR`` quebrado derruba a subida, release quebrada), e a migração do idioma dos
        # servidores que já existiam roda uma vez, DEPOIS do ``load`` e ANTES dos bots (I-12). Falha da migração fica para a próxima subida.
        self.i18n.load()
        try:
            self.loop.run_until_complete(self.servers.migrate_language())
        except Exception:
            logger.exception("Falha ao migrar o idioma dos servidores")

        for k, v in all_tokens.items():
            load_bot(k, v, load_modules_log=load_modules_log)
            load_modules_log = False

        for guild_id, guildbotsdata in read_guild_bots().items():
            for n, guildbottoken in enumerate(guildbotsdata):
                load_bot(f"{guild_id}_{n}", guildbottoken, guild_id, load_modules_log=load_modules_log)
                load_modules_log = False

        message = ""

        self.loop.create_task(self.setup_pool_extras())
        self.playlist_icons_task = self.loop.create_task(self.migrate_playlist_icons())
        # Em tarefa de fundo (nunca atrasa a subida) e guardada no pool para o shutdown cancelar como as outras.
        self.version_check_task = self.loop.create_task(check_new_version())

        if not self.bots:
            message = (
                "O token do bot não foi configurado devidamente! Confira se o token foi configurado na "
                "ENV/ENVIRONMENT ou no arquivo .env"
            )
            boot.step("Bots", "fail", message)
            message = f"\n\n{message}"

        else:
            if start_local:
                self.loop.create_task(self.start_lavalink())

            if self.spotify and not self.spotify.spotify_cache:
                self.spotify_token_task = self.loop.create_task(self.refresh_spotify_token())

            self.node_check(LAVALINK_SERVERS, start_local=start_local)

        if self.config["RUN_RPC_SERVER"]:
            self.cache_updater_task = self.loop.create_task(self.cache_updater())

            if not message:
                self.loop.create_task(self.run_bots(self.get_all_bots()))
                self.loop.create_task(self.connect_rpc_ws())

            try:
                start(self, message=message)
            except KeyboardInterrupt:
                return

        elif message:
            raise Exception(message)

        else:
            self.cache_updater_task = self.loop.create_task(self.cache_updater())

            self.loop.create_task(self.connect_rpc_ws())

            try:
                self.loop.run_until_complete(self.run_bots(self.get_all_bots()))
            except KeyboardInterrupt:
                return


class BotCore(commands.AutoShardedBot):
    def __init__(self, *args, **kwargs):
        self.session: Optional[aiohttp.ClientError] = None
        self.pool: BotPool = kwargs.pop("pool")
        self.default_prefix = kwargs.pop("default_prefix", "!!")
        self.session: Optional[aiohttp.ClientSession] = None
        self.color = kwargs.pop("embed_color", None)
        self.identifier = kwargs.pop("identifier", "")
        self.appinfo: Optional[disnake.AppInfo] = None
        self.exclusive_guild_id: Optional[int] = None
        self.bot_ready = False
        self.uptime = disnake.utils.utcnow()
        self.env_owner_ids = set()
        self.dm_cooldown = commands.CooldownMapping.from_cooldown(rate=2, per=30, type=commands.BucketType.member)
        self.number = kwargs.pop("number", 0)
        # Nome externo com @everyone ou cargo (título, servidor, autor) não pode notificar ninguém; menção a usuário e a resposta valem.
        kwargs.setdefault(
            "allowed_mentions", disnake.AllowedMentions(everyone=False, roles=False, users=True, replied_user=True)
        )
        super().__init__(*args, **kwargs)
        self.music: wavelink.Client = music_mode(self)
        self.interaction_id: Optional[int] = None
        self.wavelink_node_reconnect_tasks = {}
        self._clear_commands_task: Optional[asyncio.Task] = None

        self.env_owner_ids.update(parse_owner_ids(self.config["OWNER_IDS"]))

    async def on_error(self, event_method: str, *args, **kwargs):
        """Exceção em um tratador de evento: ERROR com o traceback no log de erros (o padrão do disnake imprimiria no stderr)."""
        logging.getLogger("empiremuse.errors").error("Exceção não tratada no evento %s", event_method, exc_info=True)

    async def edit_voice_channel_status(self, status: Optional[str], *, channel_id: int, reason: Optional[str] = None):
        # Obtido do discord.py: https://github.com/Rapptz/discord.py/blob/9ce733321b445db245924bfd21fedf20a01a570b/discord/http.py#L1166
        r = Route("PUT", "/channels/{channel_id}/voice-status", channel_id=channel_id)
        payload = {"status": status}
        return await self.http.request(r, reason=reason, json=payload)

    @property
    def player_skins(self):
        return self.pool.player_skins

    @property
    def player_static_skins(self):
        return self.pool.player_static_skins

    @property
    def config(self):
        return self.pool.config

    @property
    def last_fm(self):
        return self.pool.last_fm

    @property
    def deezer(self):
        return self.pool.deezer

    @property
    def spotify(self):
        return self.pool.spotify

    @property
    def emoji_data(self):
        return self.pool.emoji_data

    @property
    def ws_client(self):
        return self.pool.ws_client

    @property
    def is_main(self) -> bool:
        """O bot principal (token principal), nunca um bot exclusivo de servidor."""
        return is_main_identifier(self.identifier) and not self.exclusive_guild_id

    @property
    def is_reserve(self) -> bool:
        """Reserva: do pool, não é o principal, não é exclusivo de servidor e há um token principal configurado."""
        return not self.exclusive_guild_id and not is_main_identifier(self.identifier) and self.pool.main_configured

    def main_invite_url(self) -> Optional[str]:
        """Convite do principal montado aqui (nunca de texto de fora); ``None`` se ele não existe ou não é público."""
        main = self.pool.main_bot
        if main is None or main.appinfo is None or not main.appinfo.bot_public:
            return None
        return disnake.utils.oauth_url(
            main.user.id, permissions=disnake.Permissions(main.config["INVITE_PERMISSIONS"]), scopes=("bot",)
        )

    def slash_commands_line(self, guild_id: int) -> Optional[str]:
        """Para uma reserva: uma linha dizendo onde estão os comandos de barra (ela não os tem). ``None`` para os outros bots."""
        main = self.pool.main_bot
        if not self.is_reserve or main is None:
            return None
        if main.get_guild(guild_id) is not None:
            return f"Os comandos de barra (/) ficam no {main.user.mention}."
        url = self.main_invite_url()
        return main_missing_hint(main.user.name, url) if url else None

    async def get_data(self, id_: int, *, db_name: Union[DBModel.guilds, DBModel.users]):
        if db_name == DBModel.guilds:  # configuração do servidor: documento único, devolvido no formato antigo
            return await self.pool.servers.legacy_doc(self, id_)
        return await self.pool.database.get_data(id_=id_, db_name=db_name, collection=str(self.user.id))

    async def update_data(self, id_, data: dict, *, db_name: Union[DBModel.guilds, DBModel.users]):
        if db_name == DBModel.guilds:  # só canal, mensagem e favoritos; settings vai por pool.servers
            return await self.pool.servers.save_legacy(self, id_, data)
        return await self.pool.database.update_data(id_=id_, data=data, db_name=db_name, collection=str(self.user.id))

    async def get_global_data(self, id_: int, *, db_name: Union[DBModel.guilds, DBModel.users]):
        data = await self.pool.database.get_data(
            id_=id_, db_name=db_name, collection="global", default_model=global_db_models
        )

        return data

    async def update_global_data(self, id_, data: dict, *, db_name: Union[DBModel.guilds, DBModel.users]):
        return await self.pool.database.update_data(
            id_=id_, data=data, db_name=db_name, collection="global", default_model=global_db_models
        )

    async def is_owner(self, user: Union[disnake.User, disnake.Member]) -> bool:
        if user.id in self.env_owner_ids:
            return True

        return await super().is_owner(user)

    async def _clear_remote_commands(self) -> None:
        """Reserva: apaga do Discord os comandos globais (e os de ``TEST_GUILDS_*``) da aplicação, uma vez (marca ``[]`` no arquivo de sync).

        Só com o principal logado: sem ele a reserva não mexe em nada (fica com o que tinha). Falha vira aviso e repete no próximo boot.
        """
        if not await self.pool.wait_main_outcome():
            logger.error(
                "Principal não logou: a reserva %s mantém os comandos de barra que já tinha na aplicação",
                self.identifier,
            )
            return

        sync_file = paths.RUNTIME_DIR / ".app_commands_sync_data" / f"{self.user.id}.json"
        try:
            with open(sync_file) as f:
                if await self.loop.run_in_executor(None, lambda: json.load(f)) == []:
                    return
        except FileNotFoundError, ValueError:
            pass

        try:
            await self.http.bulk_upsert_global_commands(self.application_id, [])
            for guild_id in self._test_guilds or ():
                await self.http.bulk_upsert_guild_commands(self.application_id, guild_id, [])
        except Exception as exc:  # noqa: BLE001 - vira aviso; o próximo boot tenta de novo
            logger.warning(
                "Reserva %s: comandos de barra da aplicação %s não removidos (%s)",
                self.identifier,
                self.application_id,
                type(exc).__name__,
            )
            return

        logger.warning(
            "Reserva %s: comandos de barra removidos da aplicação %s (eles ficam no principal)",
            self.identifier,
            self.application_id,
        )
        try:
            sync_file.parent.mkdir(parents=True, exist_ok=True)
            with open(sync_file, "w", encoding="utf-8") as f:
                await self.loop.run_in_executor(None, lambda: json.dump([], f))
        except Exception:
            logger.exception("Falha ao gravar os dados da sincronização dos comandos")

    async def clear_remote_commands(self) -> None:
        try:
            await self._clear_remote_commands()
        except Exception:
            logger.exception("Falha ao remover os comandos de barra da reserva %s", self.identifier)

    async def sync_app_commands(self, force=False):
        await self.wait_until_ready()

        if self.is_reserve:  # só o principal sincroniza comandos; a reserva fica sem nenhum no Discord
            if self._clear_commands_task is None or self._clear_commands_task.done():
                # em tarefa de fundo: esperar o principal logar nunca atrasa a reserva ficar pronta
                self._clear_commands_task = self.loop.create_task(self.clear_remote_commands())
            return

        if not self.command_sync_flags.sync_commands and not force:
            return

        current_cmds = sorted(
            [sort_dict_recursively(cmd.body.to_dict()) for cmd in self.application_commands], key=lambda k: k["name"]
        )

        sync_dir = paths.RUNTIME_DIR / ".app_commands_sync_data"
        try:
            with open(sync_dir / f"{self.user.id}.json") as f:
                synced_cmds = await self.loop.run_in_executor(None, lambda: json.load(f))
        except FileNotFoundError:
            synced_cmds = None

        if current_cmds == synced_cmds:
            if current_cmds:
                logger.debug("%s - Os comandos já estão sincronizados.", self.user)
            return

        self._command_sync_flags = self.pool.command_sync_config
        await self._sync_application_commands()
        self._command_sync_flags = commands.CommandSyncFlags.none()

        try:
            if not sync_dir.is_dir():
                sync_dir.mkdir(parents=True, exist_ok=True)

            with open(sync_dir / f"{self.user.id}.json", "w", encoding="utf-8") as f:
                await self.loop.run_in_executor(None, lambda: json.dump(current_cmds, f, indent=4))

            with open(sync_dir / f"{self.user.id}-old.json", "w", encoding="utf-8") as f:
                await self.loop.run_in_executor(None, lambda: json.dump(synced_cmds, f, indent=4))

        except Exception:
            logger.exception("Falha ao gravar os dados da sincronização dos comandos")

    def sync_command_cooldowns(self, force=False):
        for b in self.pool.get_all_bots():
            if b == self and force is False:
                continue

            for cmd in b.commands:
                cmd.ignore_extra = False
                if cmd.extras.get("exclusive_cooldown"):
                    continue
                c = self.get_command(cmd.name)
                if not c:
                    continue
                c.ignore_extra = False
                if self.pool.config["ENABLE_COMMANDS_COOLDOWN"] is False:
                    c._buckets._cooldown = None
                else:
                    c._buckets = cmd._buckets

            for cmd in b.slash_commands:
                c = self.get_slash_command(cmd.name)
                if not c:
                    continue
                if self.pool.config["ENABLE_COMMANDS_COOLDOWN"] is False:
                    c._buckets._cooldown = None
                else:
                    if c.extras.get("exclusive_cooldown"):
                        continue
                    c._buckets = cmd._buckets

            for cmd in b.user_commands:
                c = self.get_user_command(cmd.name)
                if not c:
                    continue
                if self.pool.config["ENABLE_COMMANDS_COOLDOWN"] is False:
                    c._buckets._cooldown = None
                else:
                    if c.extras.get("exclusive_cooldown"):
                        continue
                    c._buckets = cmd._buckets

            for cmd in b.message_commands:
                c = self.get_message_command(cmd.name)
                if not c:
                    continue
                if self.pool.config["ENABLE_COMMANDS_COOLDOWN"] is False:
                    c._buckets._cooldown = None
                else:
                    if c.extras.get("exclusive_cooldown"):
                        continue
                    c._buckets = cmd._buckets

    async def can_send_message(self, message: disnake.Message):
        if isinstance(message.channel, disnake.Thread):
            perm_check = message.channel.parent.permissions_for(message.guild.me).send_messages_in_threads
        else:
            perm_check = message.channel.permissions_for(message.guild.me).send_messages

        if not perm_check:
            logger.debug(
                "Sem permissão para enviar mensagem no canal %s",
                message.channel.id,
                extra=guild_extra(message.guild.id),
            )
            return

        return True

    async def on_message(self, message: disnake.Message):
        if not self.bot_ready or not self.appinfo or self.is_closed():
            return

        if not message.guild:
            return

        if self.exclusive_guild_id and message.guild.id != self.exclusive_guild_id:
            return

        try:
            player: LavalinkPlayer = self.music.players[message.guild.id]
            if player.text_channel == message.channel and not message.flags.ephemeral:
                player.last_message_id = message.id
        except KeyError, AttributeError:
            pass

        if isinstance(message.channel, disnake.StageChannel):
            pass

        elif message.author.bot:
            return

        elif message.content in (f"<@{self.user.id}>", f"<@!{self.user.id}>"):
            if message.author.bot:
                return

            if not await self.can_send_message(message):
                return

            embed = disnake.Embed(color=self.get_color(message.guild.me))

            prefix = await self.get_prefix(message)

            if not isinstance(prefix, str):
                prefix = prefix[-1]

            embed.description = f"**Olá {message.author.mention}.**"

            if not self.config["INTERACTION_COMMAND_ONLY"]:
                embed.description += (
                    f"\n\nMeu prefixo no servidor é: **{prefix}** `(minha menção também funciona como prefixo).`\n"
                    f"Pra ver todos os meus comandos use **{prefix}help**"
                )

            if self.is_reserve:
                if line := self.slash_commands_line(message.guild.id):
                    embed.description += f"\n\n{line}"
            else:
                embed.description += "\n\n**Pra ver todos os meus comandos use: /**"

            kwargs = {
                "components": [
                    disnake.ui.Button(
                        label="Me adicione no seu servidor.",
                        url=disnake.utils.oauth_url(
                            self.user.id,
                            permissions=disnake.Permissions(self.config["INVITE_PERMISSIONS"]),
                            scopes=("bot",),
                        ),
                    )
                ]
            }

            if message.channel.permissions_for(message.guild.me).read_message_history:
                await message.reply(embed=embed, fail_if_not_exists=False, **kwargs)
            else:
                await message.channel.send(message.author.mention, embed=embed, **kwargs)
            return

        ctx: CustomContext = await self.get_context(message, cls=CustomContext)

        if await refuse_radio_only(self, ctx, message):
            return  # bot só-rádio (item 8): nem pedido, nem menção, nem prefixo; o comando de prefixo ganha um aviso

        try:
            ctx.player = self.music.players[message.guild.id]
        except Exception:
            pass

        if (
            self.config["ENABLE_SONGREQUEST_MENTION"]
            and not ctx.valid
            and message.content.startswith(self.user.mention)
            and message.author.voice
        ):
            query = str(message.content)

            for m in message.mentions:
                query = query.replace(m.mention, "", 1)

            query = query.strip()

            if query:
                play_cmd = self.get_slash_command("play")
                self.dispatch("pool_dispatch", ctx, self.user.id)
                try:
                    await play_cmd.callback(
                        inter=ctx,
                        query=query,
                        self=play_cmd.cog,
                        position=0,
                        options=False,
                        force_play="no",
                        manual_selection=False,
                        server=None,
                    )
                except Exception as e:
                    self.dispatch("command_error", ctx, e)
                return

        self.dispatch("song_request", ctx, message)

        if not ctx.valid:
            return

        if not await self.can_send_message(message):
            return

        if (
            self.is_reserve
            and not message.content.startswith(tuple(self.pool.bot_mentions))  # citada pelo nome, a reserva atende
            and self.pool.main_can_serve(message.guild.id, message.channel.id)
            and not self._owns_forum_post(
                message.channel
            )  # no post de fórum dela só ela atende (o principal recusa em check_forum)
            and not await self._main_took_prefix(ctx)
        ):
            return  # com o principal atendendo aqui, quem recebe o comando de prefixo é ele (S-V6)

        try:
            kwargs = {
                "only_voiced": ctx.command.pool_only_voiced,
                "check_player": ctx.command.pool_check_player,
                "return_first": ctx.command.pool_return_first,
            }
        except AttributeError:
            kwargs = {"return_first": True}

        try:
            await check_pool_bots(ctx, **kwargs)
        except Exception as e:
            self.dispatch("command_error", ctx, e)
            return

        await self.invoke(ctx)

    def _owns_forum_post(self, channel) -> bool:
        return (
            not self.check_bot_forum_post(channel, raise_error=False)
            and getattr(channel, "owner_id", None) == self.user.id
        )

    async def _main_took_prefix(self, ctx: CustomContext) -> bool:
        """A reserva espera o principal assumir o comando (a entrada dele em ``pool.message_ids``); sem ela no prazo, ignora."""
        msg_id = f"{ctx.guild_id}-{ctx.channel.id}-{ctx.message.id}"
        deadline = self.loop.time() + MAIN_PREFIX_WAIT
        while msg_id not in self.pool.message_ids:
            if self.loop.time() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return True

    def check_bot_forum_post(
        self,
        channel: Union[disnake.ForumChannel, disnake.TextChannel, disnake.VoiceChannel, disnake.Thread],
        raise_error=False,
    ):
        try:
            if isinstance(channel.parent, disnake.ForumChannel):
                if channel.owner_id in (
                    bot.user.id for bot in self.pool.get_guild_bots(channel.guild.id) if bot.bot_ready
                ):
                    if raise_error is False:
                        return False

                    raise GenericError(
                        "**Você não pode usar comandos prefixed na postagem atual...**\n"
                        "`Use comando de barra (/) aqui.`",
                        self_delete=True,
                    )
        except AttributeError:
            pass

        return True

    def get_color(self, me: Optional[disnake.Member] = None):
        if not me:
            return self.color or 0x2B2D31

        if self.color:
            return self.color

        if me.color.value == 0:
            return 0x2B2D31

        return me.color

    async def update_appinfo(self):
        await self.wait_until_ready()

        self.appinfo = await self.application_info()

        try:
            self.owner = self.appinfo.team.owner
        except AttributeError:
            self.owner = self.appinfo.owner

    async def on_application_command_autocomplete(self, inter: disnake.ApplicationCommandInteraction):
        if not self.bot_ready or not self.is_ready():
            return []

        if not inter.guild_id:
            return []

        if self.exclusive_guild_id and inter.guild_id != self.exclusive_guild_id:
            return []

        await super().on_application_command_autocomplete(inter)

    async def on_application_command(self, inter: disnake.ApplicationCommandInteraction):
        if not self.bot_ready or self.is_closed():
            await inter.send("Ainda estou inicializando...\nPor favor aguarde mais um pouco...", ephemeral=True)
            return

        await super().on_application_command(inter)

    def load_modules(self, module_list: list = None, load_modules_log=False):
        cogs_dir = Path(__file__).resolve().parent.parent / "cogs"
        cogs_dirs = [(cogs_dir, "empiremuse.cogs")]
        cogs_dev = Path(__file__).resolve().parent.parent / "cogs_dev"
        if cogs_dev.is_dir():
            cogs_dirs.append((cogs_dev, "empiremuse.cogs_dev"))

        load_status = {
            "reloaded": [],
            "loaded": [],
            "failed": [],
        }

        bot_name = self.user or self.identifier

        for module_dir, prefix in cogs_dirs:
            for item in os.walk(module_dir):
                files = filter(lambda f: f.endswith(".py"), item[-1])

                for file in files:
                    if file.startswith("__"):
                        continue
                    if module_list:
                        if not [i for i in module_list if file.endswith(i)]:
                            continue
                    filename, _ = os.path.splitext(file)
                    module_filename = f"{prefix}.{filename}"
                    try:
                        self.unload_extension(module_filename)
                        self.load_extension(module_filename)
                        if not self.bot_ready and load_modules_log:
                            logger.debug("%s - %s.py recarregado.", bot_name, filename)
                        load_status["reloaded"].append(f"{filename}.py")
                    except commands.ExtensionAlreadyLoaded, commands.ExtensionNotLoaded:
                        try:
                            self.load_extension(module_filename)
                            if not self.bot_ready and load_modules_log:
                                logger.debug("%s - %s.py carregado.", bot_name, filename)
                            load_status["loaded"].append(f"{filename}.py")
                        except Exception as e:
                            _report_module_failure(bot_name, filename, e)
                            if not self.bot_ready:
                                raise ModuleLoadError(f"{filename}.py") from e
                            load_status["failed"].append(f"{filename}.py")
                    except Exception as e:
                        _report_module_failure(bot_name, filename, e)
                        if not self.bot_ready:
                            raise ModuleLoadError(f"{filename}.py") from e
                        load_status["failed"].append(f"{filename}.py")

        if load_modules_log and not self.bot_ready:
            BootReport().step(
                f"Cogs de {bot_name}",
                "warn" if load_status["failed"] else "ok",
                f"{len(load_status['loaded']) + len(load_status['reloaded'])} carregadas, {len(load_status['failed'])} falharam",
            )

        if not self.config["ENABLE_DISCORD_URLS_PLAYBACK"]:
            self.remove_slash_command("play_music_file")

        for c in self.slash_commands:
            if (desc := len(c.description)) > 100:
                raise Exception(
                    f"A descrição do comando {c.name} excedeu a quantidade de caracteres permitido "
                    f"no discord (100), quantidade atual: {desc}"
                )

        return load_status
