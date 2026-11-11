# -*- coding: utf-8 -*-
import logging
import re
from json import load
from os import environ

import disnake
from dotenv import dotenv_values

from empiremuse import paths
from empiremuse.rules.app_profile import DEFAULT_DESCRIPTION, DEFAULT_RESERVE_DESCRIPTION, DEFAULT_TAGS
from empiremuse.rules.usage import clamp_retention_days
from empiremuse.utils import branding

logger = logging.getLogger(__name__)

_HEX_COLOR_REGEX = re.compile(r"#?([0-9a-fA-F]{6})")
_EMPTY_COLOR_VALUES = ("", "none")


def parse_embed_color(value: str | None) -> disnake.Color | None:
    """Converte um hex de 6 dígitos (com ou sem '#') em `disnake.Color`.

    `None`, vazio e 'none' retornam `None` sem aviso (usa a cor do cargo do bot);
    valor inválido retorna `None` e registra um aviso, sem incluir o valor no log.
    """
    if value is None or str(value).strip().lower() in _EMPTY_COLOR_VALUES:
        return None

    match = _HEX_COLOR_REGEX.fullmatch(str(value).strip())
    if not match:
        logger.warning("EMBED_COLOR inválido: use um hex de 6 dígitos (ex.: 8B5CF6). Usando a cor do cargo do bot.")
        return None

    return disnake.Color(int(match.group(1), 16))


bools = {"true": True, "false": False}

DEFAULT_CONFIG = {
    "DEFAULT_PREFIX": "!!",
    "AUTO_SYNC_COMMANDS": True,
    "OWNER_IDS": "",
    "COMMAND_LOG": False,
    "EMBED_COLOR": branding.EMBED_COLOR_HEX,
    "BOT_ADD_REMOVE_LOG": "",
    "BOT_ADD_REMOVE_LOG_CHANNEL_ID": 0,
    "ERROR_REPORT_WEBHOOK": "",
    "AUTO_ERROR_REPORT_WEBHOOK": "",
    "INTERACTION_COMMAND_ONLY": False,
    "ENABLE_JISHAKU": False,
    "PRESENCE_INTERVAL": 900,
    "SOURCE_REPO": branding.SOURCE_REPO_GIT,
    "SUPPORT_SERVER": branding.SUPPORT_SERVER_URL,
    # Perfil da aplicação no Discord, gravado por cada bot ao iniciar. Vazio = não mexer no campo. A descrição aceita {prefix},
    # {bots}, {panel_url} e {support_url} (linha cujo marcador fica vazio sai) e \n escrito à mão como quebra de linha.
    "APP_DESCRIPTION": DEFAULT_DESCRIPTION,
    "APP_DESCRIPTION_RESERVE": DEFAULT_RESERVE_DESCRIPTION,  # só o bot reserva; vazio = não mexer
    "APP_TAGS": DEFAULT_TAGS,
    "INVITE_PERMISSIONS": 2533601240804560,
    "ENABLE_LOGGER": False,
    "LOG_LEVEL": "INFO",
    "LOG_COLOR": "on",
    "LOG_FILE": "auto",
    "LOG_DEBUG_GUILDS": "",
    "LOG_RETENTION_DAYS": 183,
    "LOG_RETENTION_HOLD": False,
    "METRICS_INTERVAL": 300,
    "USAGE_RETENTION_DAYS": 30,
    "KILL_ON_429": True,
    "PREFIXED_POOL_TIMEOUT": 4,
    "ENABLE_COMMANDS_COOLDOWN": True,
    "GIT_DIR": "./.git",
    "UPDATE_PROXY_SOCKET": "",
    ################
    ### Database ###
    ################
    "MONGO": "",
    "MONGO_TIMEOUT": 30,
    "SENSITIVE_INFO_WARN": True,
    #########################
    ### Sistema de música ###
    #########################
    "AUTO_DOWNLOAD_LAVALINK_SERVERLIST": False,
    "LAVALINK_SERVER_LIST": branding.LAVALINK_SERVER_LIST_URL,
    "LAVALINK_RECONNECT_RETRIES": 30,
    "DEFAULT_SKIN": "default",
    "DEFAULT_STATIC_SKIN": "default",
    "DEFAULT_IDLING_SKIN": "default",
    "VOTE_SKIP_AMOUNT": 3,
    "IDLE_TIMEOUT": 180,
    "WAIT_FOR_MEMBERS_TIMEOUT": 180,
    "MAX_USER_FAVS": 10,
    "USER_FAV_MAX_NAME_LENGTH": 35,
    "USER_FAV_MAX_URL_LENGTH": 90,
    "MAX_USER_INTEGRATIONS": 10,
    "EXTRA_HINTS": "",
    "HINT_RATE": 4,
    "IGNORE_SKINS": "",
    "IGNORE_STATIC_SKINS": "",
    "GUILD_DEAFEN_WARN": True,
    "ENABLE_DISCORD_URLS_PLAYBACK": True,
    "PLAYER_INFO_BACKUP_INTERVAL": 45,
    "PLAYER_INFO_BACKUP_INTERVAL_MONGO": 300,
    "PLAYER_SESSIONS_MONGODB": False,
    "QUEUE_MAX_ENTRIES": 0,
    "ENABLE_DEFER_TYPING": True,
    "VOICE_CHANNEL_LATENCY_RECONNECT": 200,
    "PLAYLIST_CACHE_SIZE": 500,
    "PLAYLIST_CACHE_TTL": 1800,
    "USE_YTM_TRACKINFO_SCROBBLE": False,
    "ENABLE_SONGREQUEST_MENTION": True,
    ##############################################
    ### Sistema de música - Suporte ao spotify ###
    ##############################################
    "SPOTIFY_CLIENT_ID": "",
    "SPOTIFY_CLIENT_SECRET": "",
    "SPOTIFY_PLAYLIST_EXTRA_PAGE_LIMIT": 0,
    ################################################
    ### Sistema de música - RPC (Rich Presence): ###
    ################################################
    "RUN_RPC_SERVER": False,
    "RPC_SERVER": "ws://localhost:$PORT/ws",
    "PORT": None,
    "RPC_PUBLIC_URL": "",
    "ENABLE_RPC_COMMAND": False,
    "ENABLE_RPC_AUTH": True,
    "RPC_SERVER_SECRET": "",
    "RPC_ALLOWED_ORIGINS": "",
    "WEB_BIND_ADDRESS": "0.0.0.0",
    ##################################################
    ### Painel web (OAuth2 Discord, sob /painel):  ###
    ##################################################
    "PANEL_ENABLED": False,
    "PANEL_BASE_URL": "",
    "PANEL_OAUTH_CLIENT_ID": "",
    "PANEL_OAUTH_CLIENT_SECRET": "",
    "PANEL_COOKIE_SECRET": "",
    "PANEL_SESSION_TTL_HOURS": 12,
    "PANEL_SESSION_IDLE_MINUTES": 60,
    "PANEL_TRUST_PROXY": False,
    "PANEL_TRUSTED_PROXY_CIDRS": "",
    "PANEL_INSECURE_COOKIES": False,
    ##################################################
    ### Painel externo (API em /api/v1, listener próprio): ###
    ##################################################
    "EXTERNAL_PANEL_URL": "",
    "EXTERNAL_PANEL_API_SECRET": "",
    "EXTERNAL_PANEL_API_SECRET_PREVIOUS": "",
    "EXTERNAL_PANEL_API_PREVIOUS_UNTIL": "",
    "EXTERNAL_PANEL_SIGNING_SECRET": "",
    "EXTERNAL_PANEL_API_PORT": "",
    "EXTERNAL_PANEL_API_BIND": "",
    "EXTERNAL_PANEL_API_CIDRS": "",
    ##################################################
    ### Sistema de música - Local lavalink stuffs: ###
    ##################################################
    "RUN_LOCAL_LAVALINK": False,
    "CONNECT_LOCAL_LAVALINK": True,
    "LAVALINK_JAVA_PATH": "",
    "LAVALINK_HOME": "",
    "LAVALINK_OFFLINE": False,
    "LAVALINK_ADDITIONAL_SLEEP": 0,
    "LAVALINK_INITIAL_RAM": 30,
    "LAVALINK_RAM_LIMIT": 120,
    "LAVALINK_CPU_CORES": 2,
    "LAVALINK_FILE_URL": branding.LAVALINK_JAR_URL,
    "LAVALINK_FILE_SHA256": "",
    "YOUTUBE_OAUTH_REFRESH_TOKEN": "",
    "SEARCH_PROVIDERS": "ytsearch ytmsearch scsearch",
    "PREFER_YOUTUBE_NATIVE_PLAYBACK": True,
    "ONLY_USE_NATIVE_SEARCH_PROVIDERS": True,
    ##################################################
    ### Sistema de música - Integração com Last.fm ###
    ##################################################
    "LASTFM_KEY": "",
    "LASTFM_SECRET": "",
    ##########################
    ##### Bot presences: #####
    ##########################
    "LISTENING_PRESENCES": "",
    "WATCHING_PRESENCES": "",
    "PLAYING_PRESENCES": "",
    "STREAMING_PRESENCES": "",
    "CUSTOM_STATUS_PRESENCES": "🎵 /play ou !play | ajuda: !help||⚙️ Painel: muse.empirecommunity.org/painel||🎧 Tocando em {players_count} calls",
    ###############
    ### Intents ###
    ###############
    "BANS_INTENT": False,
    "DM_MESSAGES_INTENT": False,
    "DM_REACTIONS_INTENT": False,
    "DM_TYPING_INTENT": False,
    "GUILD_MESSAGES_INTENT": True,
    "GUILD_REACTIONS_INTENT": False,
    "GUILD_SCHEDULED_EVENTS_INTENT": False,
    "GUILD_TYPING_INTENT": False,
    "EMOJIS_AND_STICKERS_INTENT": True,
    "GUILDS_INTENT": True,
    "INTEGRATIONS_INTENT": True,
    "INVITES_INTENT": True,
    "VOICE_STATES_INTENT": True,
    "WEBHOOKS_INTENT": False,
    ##########################
    ### Privileged Intents ###
    ##########################
    "MEMBERS_INTENT": False,
    "PRESENCES_INTENT": False,
    "MESSAGE_CONTENT_INTENT": True,
    ##############
    ### Tests ####
    ##############
    "FORCE_USE_DEEZER_CLIENT": False,
    "YOUTUBE_TRACK_COOLDOWN": 20,
    "DBCACHE_SIZE": 1000,
    "DBCACHE_TTL": 300,
}


DEFAULT_PORT = 6000
MAX_PORT = 65535


def normalize_update_proxy_socket(value: object) -> str:
    """Caminho ABSOLUTO do socket do proxy de atualização, ou ``""``. Relativo ou de outro tipo é ignorado com um aviso
    que cita a chave e não o valor (o caminho pode revelar a estrutura do host)."""
    if value is None:
        return ""
    if isinstance(value, str):
        path = value.strip()
        if path == "" or path.startswith("/"):
            return path
    logger.warning("UPDATE_PROXY_SOCKET ignorado: use um caminho absoluto (começando com /) ou deixe vazio.")
    return ""


def resolve_port(configured: str | int | None = None) -> int:
    """Porta do servidor HTTP/RPC: `PORT` (config ou ambiente), depois `SERVER_PORT` (Pterodactyl) e, por fim, 6000.

    Valores vazios contam como não definidos. Valor não numérico ou fora de 0-65535 levanta `ValueError`
    citando a variável de origem.
    """
    for origem, valor in (
        ("PORT", configured),
        ("PORT", environ.get("PORT")),
        ("SERVER_PORT", environ.get("SERVER_PORT")),
    ):
        if valor is None or str(valor).strip() == "":
            continue
        try:
            porta = int(str(valor).strip())
        except ValueError:
            raise ValueError(f"A variável {origem} deve ser um número de porta, mas recebeu {str(valor)!r}.") from None
        if not 0 <= porta <= MAX_PORT:
            raise ValueError(f"A variável {origem} deve estar entre 0 e {MAX_PORT}, mas recebeu {porta}.")
        return porta
    return DEFAULT_PORT


def load_config():
    CONFIG = dict(DEFAULT_CONFIG)

    for cfg in list(CONFIG) + ["TOKEN", "MONGO"]:
        try:
            CONFIG[cfg] = environ[cfg]
        except KeyError:
            continue

    for env in environ:
        if env.lower().startswith(("token_reserve_", "token_radio_", "test_guilds_", "lavalink_node_")):
            CONFIG[env] = environ[env]

    try:
        with open(paths.RUNTIME_DIR / "config.json") as f:
            CONFIG.update(load(f))
    except FileNotFoundError:
        pass

    try:
        CONFIG.update(dotenv_values(paths.RUNTIME_DIR / ".env"))
    except (OSError, UnicodeError) as e:
        # só o tipo e o caminho: o conteúdo do .env pode ter segredos
        logger.warning(
            "Não foi possível ler o arquivo %s (%s); seguindo só com o ambiente e o config.json.",
            paths.RUNTIME_DIR / ".env",
            type(e).__name__,
        )

    # o padrão relativo ao cwd vira o repositório do RUNTIME_DIR; um GIT_DIR configurado pelo usuário é respeitado.
    # Não usa PROJECT_DIR/.git: o repositório acompanha o diretório de execução.
    if CONFIG["GIT_DIR"] == DEFAULT_CONFIG["GIT_DIR"]:
        CONFIG["GIT_DIR"] = str(paths.RUNTIME_DIR / ".git")

    CONFIG["EMBED_COLOR"] = parse_embed_color(CONFIG["EMBED_COLOR"])

    # converter strings que requer número int.
    for i in [
        "MAX_USER_FAVS",
        "IDLE_TIMEOUT",
        "WAIT_FOR_MEMBERS_TIMEOUT",
        "VOTE_SKIP_AMOUNT",
        "LAVALINK_ADDITIONAL_SLEEP",
        "LAVALINK_INITIAL_RAM",
        "LAVALINK_RAM_LIMIT",
        "LAVALINK_CPU_CORES",
        "USER_FAV_MAX_NAME_LENGTH",
        "USER_FAV_MAX_URL_LENGTH",
        "MAX_USER_INTEGRATIONS",
        "PRESENCE_INTERVAL",
        "HINT_RATE",
        "MONGO_TIMEOUT",
        "INVITE_PERMISSIONS",
        "PREFIXED_POOL_TIMEOUT",
        "PLAYER_INFO_BACKUP_INTERVAL",
        "PLAYER_INFO_BACKUP_INTERVAL_MONGO",
        "LAVALINK_RECONNECT_RETRIES",
        "QUEUE_MAX_ENTRIES",
        "VOICE_CHANNEL_LATENCY_RECONNECT",
        "DBCACHE_SIZE",
        "DBCACHE_TTL",
        "PLAYLIST_CACHE_SIZE",
        "PLAYLIST_CACHE_TTL",
        "SPOTIFY_PLAYLIST_EXTRA_PAGE_LIMIT",
        "BOT_ADD_REMOVE_LOG_CHANNEL_ID",
        "YOUTUBE_TRACK_COOLDOWN",
        "PANEL_SESSION_TTL_HOURS",
        "PANEL_SESSION_IDLE_MINUTES",
        "METRICS_INTERVAL",
    ]:
        if CONFIG[i] in ("", None):  # só o ausente e o vazio usam o padrão; o 0 inteiro do config.json vale
            CONFIG[i] = DEFAULT_CONFIG[i]
            continue

        try:
            CONFIG[i] = int(CONFIG[i])
        except (ValueError, TypeError, OverflowError) as e:  # texto, lista/objeto do config.json e Infinity
            raise Exception(f"Você usou uma configuração inválida! {i}: {CONFIG[i]}\n{repr(e)}")

    # Retenção do registro de uso: valor inválido ou menor que 1 usa o padrão (30), nunca derruba a inicialização.
    CONFIG["USAGE_RETENTION_DAYS"] = clamp_retention_days(CONFIG["USAGE_RETENTION_DAYS"])

    # converter strings que requer valor bool/nulo.
    for i in [
        "AUTO_SYNC_COMMANDS",
        "INTERACTION_COMMAND_ONLY",
        "ENABLE_JISHAKU",
        "RUN_LOCAL_LAVALINK",
        "LAVALINK_OFFLINE",
        "CONNECT_LOCAL_LAVALINK",
        "COMMAND_LOG",
        "RUN_RPC_SERVER",
        "ENABLE_RPC_COMMAND",
        "ENABLE_RPC_AUTH",
        "PANEL_ENABLED",
        "PANEL_TRUST_PROXY",
        "PANEL_INSECURE_COOKIES",
        "AUTO_DOWNLOAD_LAVALINK_SERVERLIST",
        "ENABLE_LOGGER",
        "LOG_RETENTION_HOLD",
        "GUILD_DEAFEN_WARN",
        "ENABLE_DISCORD_URLS_PLAYBACK",
        "PLAYER_SESSIONS_MONGODB",
        "SENSITIVE_INFO_WARN",
        "ENABLE_DEFER_TYPING",
        "ENABLE_COMMANDS_COOLDOWN",
        "BANS_INTENT",
        "DM_MESSAGES_INTENT",
        "DM_REACTIONS_INTENT",
        "DM_TYPING_INTENT",
        "GUILD_MESSAGES_INTENT",
        "GUILD_REACTIONS_INTENT",
        "GUILD_SCHEDULED_EVENTS_INTENT",
        "GUILD_TYPING_INTENT",
        "EMOJIS_AND_STICKERS_INTENT",
        "GUILDS_INTENT",
        "INTEGRATIONS_INTENT",
        "INVITES_INTENT",
        "VOICE_STATES_INTENT",
        "WEBHOOKS_INTENT",
        "MEMBERS_INTENT",
        "PRESENCES_INTENT",
        "MESSAGE_CONTENT_INTENT",
        "PREFER_YOUTUBE_NATIVE_PLAYBACK",
        "ONLY_USE_NATIVE_SEARCH_PROVIDERS",
        "USE_YTM_TRACKINFO_SCROBBLE",
        "FORCE_USE_DEEZER_CLIENT",
        "ENABLE_SONGREQUEST_MENTION",
    ]:
        if isinstance(
            CONFIG[i], bool
        ):  # true/false nativos do config.json valem como estão (False == 0 não é "ausente")
            continue

        if CONFIG[i] is None or str(CONFIG[i]).lower() in ("", "none"):  # "none" é ausente, nunca False: vale o padrão
            CONFIG[i] = DEFAULT_CONFIG[i]
            continue

        try:
            CONFIG[i] = bools[str(CONFIG[i]).lower()]
        except KeyError as e:
            raise Exception(f"Você usou uma configuração inválida! {i}: {CONFIG[i]}\n{repr(e)}")

    CONFIG["RPC_SERVER"] = CONFIG["RPC_SERVER"].replace("$PORT", str(resolve_port(CONFIG.get("PORT"))))

    CONFIG["UPDATE_PROXY_SOCKET"] = normalize_update_proxy_socket(CONFIG["UPDATE_PROXY_SOCKET"])

    if CONFIG["WAIT_FOR_MEMBERS_TIMEOUT"] < 60:
        CONFIG["WAIT_FOR_MEMBERS_TIMEOUT"] = 60

    if CONFIG["PRESENCE_INTERVAL"] < 300:
        CONFIG["PRESENCE_INTERVAL"] = 300

    if CONFIG["IDLE_TIMEOUT"] < 10:
        CONFIG["IDLE_TIMEOUT"] = 10

    if CONFIG["YOUTUBE_TRACK_COOLDOWN"] < 7:
        CONFIG["YOUTUBE_TRACK_COOLDOWN"] = 7

    if CONFIG["PLAYER_INFO_BACKUP_INTERVAL"] < 30:
        CONFIG["PLAYER_INFO_BACKUP_INTERVAL"] = 30

    if CONFIG["PLAYER_INFO_BACKUP_INTERVAL_MONGO"] < 120:
        CONFIG["PLAYER_INFO_BACKUP_INTERVAL_MONGO"] = 120

    # 0 ou negativo não tem significado válido nestas chaves: 0 no voteskip faria o primeiro voto pular, 0 nos caches
    # levanta ValueError na primeira gravação, 0 nas dicas quebra o player e 0 no mongo é conexão sem timeout
    for i in ["VOTE_SKIP_AMOUNT", "HINT_RATE", "DBCACHE_SIZE", "PLAYLIST_CACHE_SIZE", "MONGO_TIMEOUT"]:
        if CONFIG[i] < 1:
            CONFIG[i] = 1

    if CONFIG["LAVALINK_RECONNECT_RETRIES"] < 0:
        raise Exception(
            "Você usou uma configuração inválida! LAVALINK_RECONNECT_RETRIES: "
            f"{CONFIG['LAVALINK_RECONNECT_RETRIES']} (use 0 para tentar sem limite ou um número positivo)"
        )

    return CONFIG
