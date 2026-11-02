# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Optional

import aiohttp
import disnake
import tornado.httpserver
import tornado.httputil
import tornado.ioloop
import tornado.routing
import tornado.template
import tornado.web
import tornado.websocket
from packaging import version

from empiremuse import paths
from empiremuse.config_loader import load_config, resolve_port
from empiremuse.rules.client_ip import client_ip_from_headers
from empiremuse.rpc_protocol import (
    BOT_OPS,
    BOT_TO_USER_FIELDS,
    CLOSE_MALFORMED,
    CLOSE_RATE_LIMITED,
    CLOSE_REFUSED,
    CLOSE_TRY_AGAIN_LATER,
    CLOSE_UNAUTHORIZED,
    MAX_BOT_IDS,
    MAX_CONNECTIONS,
    MAX_MESSAGE_SIZE,
    WEBSOCKET_PING_INTERVAL,
    WEBSOCKET_PING_TIMEOUT,
    MAX_USER_IDS,
    RPC_KEY_HEADER,
    TOKEN_LENGTHS,
    USER_OP,
    USER_TO_BOT_FIELDS,
    RateLimiter,
    authenticates,
    get_rpc_secret,
    is_valid_id,
    is_valid_id_list,
    normalize_origin,
    parse_allowed_origins,
    parse_app_version,
    pick_fields,
    same_secret,
)
from empiremuse.utils import branding

# Limite de corpo de requisição da porta inteira (painel, relé do RPC e página de status): 64 KiB.
MAX_BODY_SIZE = 64 * 1024

if TYPE_CHECKING:
    from empiremuse.utils.client import BotPool

logging.getLogger("tornado.access").disabled = True

logger = logging.getLogger(__name__)

PUBLIC_BIND_ADDRESSES = ("0.0.0.0", "::")
DEFAULT_BIND_ADDRESS = "0.0.0.0"

users_ws = {}
bots_ws = []


def _logo_url() -> str:
    """URL pública do logo pequeno da marca (nunca embutido na página); vazia se o arquivo não existir, e a página sai sem o logo."""
    try:
        return branding.asset_url("logo-256.png")
    except ValueError:
        return ""


minimal_version = version.parse("2.6.1")

_status_loader_cache = None


def _status_loader() -> tornado.template.Loader:
    """Loader dos templates da página de status (autoescape ligado por padrão no Tornado)."""
    global _status_loader_cache
    if _status_loader_cache is None:
        _status_loader_cache = tornado.template.Loader(str(paths.PACKAGE_DIR / "templates"))
    return _status_loader_cache


class IndexHandler(tornado.web.RequestHandler):
    def set_default_headers(self):
        # Sem CSP: a página usa <style>/<script> embutidos. Estes três bastam contra sniffing, iframe e vazamento de URL.
        self.set_header("X-Content-Type-Options", "nosniff")
        self.set_header("X-Frame-Options", "DENY")
        self.set_header("Referrer-Policy", "same-origin")

    def initialize(
        self, pool: Optional[BotPool] = None, message: str = "", config: dict = None, panel_enabled: bool = False
    ):
        self.message = message
        self.panel_enabled = panel_enabled
        self.pool = pool
        self.config = config

    async def prepare(self):
        bots = [asyncio.create_task(bot.wait_until_ready()) for bot in self.pool.bots if not bot.is_ready()]

        if bots:
            self.write("")
            await self.flush()
            await asyncio.wait(bots, timeout=7)

    async def get(self):
        try:
            killing_state = self.pool.killing_state
        except Exception:
            killing_state = False

        ready_bots = []
        pending_bots = []

        if killing_state is not True:
            for bot in sorted(self.pool.bots, key=lambda b: len(b.guilds)):
                if bot.is_ready():
                    ready_bots.append(
                        {
                            "nome": str(bot.user),
                            "avatar": bot.user.display_avatar.replace(size=256, static_format="png").url,
                            "servidores": len(bot.guilds),
                            "convite": disnake.utils.oauth_url(
                                bot.user.id,
                                permissions=disnake.Permissions(bot.config["INVITE_PERMISSIONS"]),
                                scopes=("bot", "applications.commands"),
                            ),
                        }
                    )
                else:
                    pending_bots.append(bot.identifier)

        commit = self.pool.commit or ""

        self.write(
            _status_loader()
            .load("status.html")
            .generate(
                reiniciando=killing_state is True,
                mensagem=self.message.split("\n") if self.message else [],
                prontos=ready_bots,
                pendentes=pending_bots,
                falhas=[(str(k), str(v)) for k, v in self.pool.failed_bots.items()],
                rpc_auth=bool(self.config["ENABLE_RPC_AUTH"]),
                rpc_app_url=branding.RPC_APP_RELEASES_URL,
                prefixo=self.pool.config["DEFAULT_PREFIX"],
                commit=commit,
                commit_url=f"{self.pool.remote_git_url}/commit/{commit}" if commit else "",
                painel_url="/painel/" if self.panel_enabled else "",
                suporte_url=branding.SUPPORT_SERVER_URL,
                nome_bot=branding.BOT_NAME,
                repo_url=branding.REPO_URL,
                upstream_nome=branding.UPSTREAM_NAME,
                upstream_autor=branding.UPSTREAM_AUTHOR,
                upstream_url=branding.UPSTREAM_URL,
                logo_src=_logo_url(),
            )
        )


class WebSocketHandler(tornado.websocket.WebSocketHandler):
    """Relé entre o app de Rich Presence (usuários) e os bots.

    Só uma conexão autenticada (``X-RPC-Key``) e registrada com ``"bot": true`` é tratada como bot e pode enviar
    dados aos usuários. A autenticação por token (``ENABLE_RPC_AUTH``) é decidida pela configuração do servidor.
    """

    def initialize(self, config: Optional[dict] = None, connections: Optional[set] = None):
        config = config or {}
        self.rpc_secret = get_rpc_secret(config)
        self.server_auth = bool(config.get("ENABLE_RPC_AUTH"))
        self.allowed_origins = parse_allowed_origins(config.get("RPC_ALLOWED_ORIGINS"))
        self.connections = connections if connections is not None else set()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user_ids: list = []
        self.bot_ids: list = []
        self.token = ""
        self.blocked = False
        self.authenticated = False
        self.is_bot = False
        self.rejected = False
        self.rate_limiter = RateLimiter()

    def check_origin(self, origin: str) -> bool:
        # Clientes nativos (app e bot) não enviam Origin e nem chegam aqui; navegadores só passam na mesma origem
        # ou se a origem estiver em RPC_ALLOWED_ORIGINS.
        return super().check_origin(origin) or normalize_origin(origin) in self.allowed_origins

    def open(self, *args, **kwargs):
        if len(self.connections) >= MAX_CONNECTIONS:
            self._reject(CLOSE_TRY_AGAIN_LATER, "Servidor ocupado, tente novamente mais tarde.")
            return

        self.connections.add(self)
        self.authenticated = same_secret(self.request.headers.get(RPC_KEY_HEADER), self.rpc_secret)

    def _reject(self, code: int, reason: str) -> None:
        self.rejected = True
        self.close(code=code, reason=reason)

    def _disconnect(self, reason: str) -> None:
        self.write_message(json.dumps({"op": "disconnect", "reason": reason}))
        self._reject(CLOSE_REFUSED, "Desconectado.")

    def _parse(self, message) -> Optional[dict]:
        try:
            data = json.loads(message)
        except (ValueError, RecursionError) as e:
            logger.warning("Relé RPC: mensagem descartada, JSON inválido (%s).", type(e).__name__)
        else:
            if isinstance(data, dict):
                return data
            logger.warning("Relé RPC: mensagem descartada, o JSON não é um objeto (ValueError).")

        self._reject(CLOSE_MALFORMED, "Mensagem inválida.")
        return None

    def on_message(self, message):
        if self.rejected:
            return

        if not self.is_bot and not self.rate_limiter.allow():
            self._reject(CLOSE_RATE_LIMITED, "Mensagens demais em pouco tempo.")
            return

        data = self._parse(message)
        if data is None:
            return

        if self.is_bot:
            self._on_bot_message(data)
        elif "user_ids" in data:
            self._on_registration(data)
        else:
            self._on_message_without_ids(data)

    def _on_message_without_ids(self, data: dict) -> None:
        if data.get("bot_id") or "user" in data:
            # Dado bot -> usuário de uma conexão que não é um bot autenticado e registrado.
            self._reject(CLOSE_UNAUTHORIZED, "Não autorizado.")
            return

        logger.info("Relé RPC: desconectando %s por falta de ids de usuário.", self.request.remote_ip)
        self._disconnect("Desconectando por falta de ids de usuário")

    def _on_registration(self, data: dict) -> None:
        if data.get("bot"):
            self._register_bot(data)
        else:
            self._register_user(data)

    def _register_bot(self, data: dict) -> None:
        if not self.authenticated:
            self._reject(CLOSE_UNAUTHORIZED, "Não autorizado.")
            return

        ids = data["user_ids"]
        if not is_valid_id_list(ids, MAX_BOT_IDS):
            self._reject(CLOSE_MALFORMED, "Lista de ids inválida.")
            return

        logger.debug("Nova conexão - Bot: %s %s", ids, self.request.remote_ip)
        self.bot_ids = ids
        self.is_bot = True
        bots_ws.append(self)

    def _register_user(self, data: dict) -> None:
        ids = data["user_ids"]
        if not is_valid_id_list(ids):
            self._reject(CLOSE_MALFORMED, "Lista de ids inválida.")
            return

        if parse_app_version(data.get("version")) < minimal_version:
            self._disconnect(
                "Versão do app não suportado! Certifique-se de que está usando "
                f"a versão mais recente do app ({minimal_version} ou superior)."
            )
            return

        if len(ids) > MAX_USER_IDS:
            self._disconnect(f"Você está tentando conectar mais de {MAX_USER_IDS} usuários consecutivamente...")
            return

        token = data.get("token") or ""
        if not isinstance(token, str) or len(token) not in TOKEN_LENGTHS:
            self._disconnect("O token precisa ter 50 caracteres...")
            return

        self._forget_users()
        self.user_ids = ids
        self.token = token
        for u in ids:
            logger.debug("Nova conexão - User: %s", u)

        for u_id in ids:
            self._replace_session(u_id)

        self._relay_registration(data)

    def _replace_session(self, user_id: int) -> None:
        previous = users_ws.get(user_id)
        users_ws[user_id] = self
        if previous is None or previous is self:
            return

        try:
            previous.write_message(json.dumps({"op": "disconnect", "reason": "Nova sessão iniciada em outro local..."}))
            previous.close(code=CLOSE_REFUSED)
        except tornado.websocket.WebSocketClosedError:
            pass

    def _relay_registration(self, data: dict) -> None:
        payload = {"op": USER_OP, "user_ids": self.user_ids}
        if isinstance(data.get("version"), str):
            payload["version"] = data["version"]

        encoded = json.dumps(pick_fields(payload, USER_TO_BOT_FIELDS))
        for w in list(bots_ws):
            try:
                w.write_message(encoded)
            except Exception as e:
                logger.warning("Relé RPC: falha ao repassar o registro ao bot %s (%s).", w.bot_ids, type(e).__name__)

    def _on_bot_message(self, data: dict) -> None:
        if "user_ids" in data:
            self._reject(CLOSE_MALFORMED, "Registro duplicado.")
            return

        user_id = data.get("user")
        if data.get("op") not in BOT_OPS or not is_valid_id(user_id):
            logger.warning("Relé RPC: mensagem do bot %s ignorada, formato inesperado.", self.bot_ids)
            return

        target = users_ws.get(user_id)
        if target is None:
            return

        payload = pick_fields(data, BOT_TO_USER_FIELDS)

        if self.server_auth:
            if authenticates(data.get("token"), target.token):
                target.blocked = False
            elif target.blocked:
                return
            else:
                target.blocked = True
                payload = self._token_error(payload)

        try:
            target.write_message(json.dumps(payload))
        except Exception as e:
            logger.warning("Relé RPC: falha ao entregar dados ao usuário %s (%s).", user_id, type(e).__name__)

    @staticmethod
    def _token_error(payload: dict) -> dict:
        error = {k: v for k, v in payload.items() if k not in ("track",)}
        error.update(
            {
                "op": "exception",
                "message": "token inválido! Por via das dúvidas gere um novo token usando o comando no "
                "bot: /rich_presence.",
            }
        )
        return error

    def _forget_users(self) -> None:
        """Remove só as sessões que ainda pertencem a esta conexão (outra pode ter assumido o mesmo id)."""
        for u_id in self.user_ids:
            if users_ws.get(u_id) is self:
                del users_ws[u_id]
        self.user_ids = []

    def on_close(self):
        self.connections.discard(self)

        if self.user_ids:
            for u in self.user_ids:
                logger.debug("Conexão Finalizada - User: %s", u)
            self._forget_users()
        elif self.is_bot:
            self._on_bot_close()
        else:
            logger.debug("Conexão Finalizada - IP: %s", self.request.remote_ip)

    def _on_bot_close(self) -> None:
        logger.debug("Conexão Finalizada - Bot ID's: %s", self.bot_ids)

        if self in bots_ws:
            bots_ws.remove(self)

        data = {"op": "close", "bot_id": self.bot_ids}

        for w in list(users_ws.values()):
            if w.blocked:
                continue

            try:
                w.write_message(data)
            except Exception as e:
                logger.warning(
                    "Relé RPC: falha ao avisar os usuários %s sobre o fim da conexão do bot (%s).",
                    w.user_ids,
                    type(e).__name__,
                )


class WSClient:
    def __init__(self, url: str, pool: BotPool):
        self.url: str = url
        self.pool = pool
        self.all_bots = None
        self.connection = None
        self.backoff: int = 7
        self.data: dict = {}
        self.session: Optional[aiohttp.ClientSession] = None
        self.connect_task = []
        self.registered = False

    @property
    def rpc_headers(self) -> dict:
        return {RPC_KEY_HEADER: get_rpc_secret(self.pool.config)}

    async def connect(self):
        for t in self.connect_task:
            try:
                t.cancel()
            except Exception:
                continue

        if not self.session:
            self.session = aiohttp.ClientSession()

        self.registered = False

        self.connection = await self.session.ws_connect(self.url, headers=self.rpc_headers, heartbeat=30)

        self.backoff = 7

        logger.debug("RPC client conectado, sincronizando rpc dos bots...")

        if not self.all_bots:
            self.all_bots = self.pool.get_all_bots()

        self.connect_task = [asyncio.create_task(self.connect_bot_rpc())]

    @property
    def is_connected(self):
        return self.connection and not self.connection.closed

    async def connect_bot_rpc(self):
        bot_ids = set()

        for bot in self.all_bots:
            await bot.wait_until_ready()
            bot_ids.add(bot.user.id)

        if not bot_ids:
            logger.debug("Conexão com servidor RPC ignorada: lista de bots vazia...")
            return

        await self._send_json(
            {"user_ids": list(bot_ids), "bot": True, "auth_enabled": self.pool.config["ENABLE_RPC_AUTH"]}
        )
        self.registered = True

        await asyncio.sleep(1)

        for bot in self.all_bots:
            for player in bot.music.players.values():
                if not player.guild.me.voice:
                    continue

                if player.guild.me.voice.channel.voice_states:
                    bot.loop.create_task(player.process_rpc(player.last_channel))

        logger.debug("[RPC client] Os dados de rpc foram sincronizados com sucesso.")

    async def send(self, data: dict):
        # O servidor só aceita dados do bot depois do registro; o que for gerado antes é reenviado pela
        # sincronização de ``connect_bot_rpc``, que roda logo após o registro.
        if not self.registered:
            return

        await self._send_json(data)

    async def _send_json(self, data: dict):
        if not self.is_connected:
            return

        try:
            await self.connection.send_json(data)
        except Exception as e:
            logger.warning("Cliente RPC: falha ao enviar dados ao servidor (%s).", type(e).__name__)

    def clear_tasks(self):
        for t in self.connect_task:
            try:
                t.cancel()
            except Exception:
                continue

        self.connect_task.clear()

    async def ws_loop(self):
        while True:
            try:
                if not self.is_connected:
                    self.clear_tasks()
                    await self.connect()

            except Exception as e:
                if isinstance(e, aiohttp.WSServerHandshakeError):
                    logger.warning(
                        "Falha ao conectar no servidor RPC, tentando novamente em %s segundo(s).", int(self.backoff)
                    )
                else:
                    logger.warning(
                        "Conexão com servidor RPC perdida - Reconectando em %s segundo(s).", int(self.backoff)
                    )

                await asyncio.sleep(self.backoff)
                self.backoff *= 2.5
                continue

            message = await self.connection.receive()

            if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                logger.warning("RPC Websocket Closed: %s - reconectando em %ss", message.extra, self.backoff)
                await asyncio.sleep(self.backoff)
                continue

            elif message.type in (aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSE):
                if self.connection.close_code == CLOSE_UNAUTHORIZED:
                    logger.warning(
                        "Cliente RPC: o servidor recusou o registro (confira se RPC_SERVER_SECRET é o mesmo nos dois lados)."
                    )
                logger.debug("RPC Websocket Finalizado: %s", message.extra)
                return

            try:
                data = json.loads(message.data)
            except (ValueError, RecursionError) as e:
                logger.warning("Cliente RPC: mensagem descartada, JSON inválido (%s).", type(e).__name__)
                continue

            users = data.get("user_ids") if isinstance(data, dict) else None

            if not users or not isinstance(users, list):
                continue

            op = data.get("op")

            if op == USER_OP:
                for bot in self.all_bots:
                    for player in bot.music.players.values():
                        if not player.guild.me.voice:
                            continue
                        vc = player.guild.me.voice.channel
                        vc_user_ids = [i for i in vc.voice_states if i in users]
                        if vc_user_ids:
                            bot.loop.create_task(player.process_rpc(vc))
                            for i in vc_user_ids:
                                users.remove(i)


def make_router(main_app: tornado.web.Application, panel_app: Optional[tornado.web.Application]):
    """``empiremuse.panel.make_router``, com o painel importado só aqui: importar o bot não carrega o painel."""
    from empiremuse.panel import make_router as panel_make_router

    return panel_make_router(main_app, panel_app)


class ClientIpRouter(tornado.routing.Router):
    """Ponto único do IP do visitante para o painel, a página de status e o relé ``/ws`` (tudo lê ``request.remote_ip``).

    Com proxy confiável, ``CF-Connecting-IP`` (válido) vira o IP da requisição; sem ele vale o que o ``xheaders`` do
    servidor já resolveu. Sem proxy confiável o cabeçalho é ignorado, porque um cliente direto poderia forjá-lo.
    """

    def __init__(self, inner: tornado.routing.Router, trust_proxy: bool) -> None:
        self.inner = inner
        self.trust_proxy = trust_proxy

    def find_handler(self, request: tornado.httputil.HTTPServerRequest, **kwargs):
        if self.trust_proxy:
            request.remote_ip = client_ip_from_headers(request.headers, True, request.remote_ip)
        return self.inner.find_handler(request, **kwargs)


def _build_panel(pool: BotPool, config: dict):
    """Cria a aplicação do painel (None se desligado ou com configuração inválida; o servidor segue sem ele)."""
    from empiremuse.panel import build_panel_app
    from empiremuse.panel.settings import PanelConfigError, load_panel_settings

    try:
        settings = load_panel_settings(config)
    except PanelConfigError as e:
        logging.getLogger("empiremuse.panel").warning("Painel web desativado por configuração inválida: %s", e)
        return None, False

    panel_app = build_panel_app(pool, settings)
    if panel_app:
        logging.getLogger("empiremuse.panel").info("Painel web ativo em %s/painel/", settings.base_url)
    return panel_app, settings.trust_proxy


def _warn_if_exposed_without_auth(config: dict, bind_address: str) -> None:
    if bind_address in PUBLIC_BIND_ADDRESSES and not config.get("ENABLE_RPC_AUTH"):
        logger.warning(
            "Servidor RPC escutando em %s com ENABLE_RPC_AUTH desativado: qualquer cliente que alcance a porta pode "
            "registrar usuários. Ative ENABLE_RPC_AUTH ou restrinja WEB_BIND_ADDRESS (ex.: 127.0.0.1).",
            bind_address,
        )


def run_app(pool: BotPool, message: str = "", config: dict = None):
    if not config:
        try:
            config = pool.config
        except IndexError:
            pass

    panel_app, trust_proxy = _build_panel(pool, config)

    app = tornado.web.Application(
        [
            (
                r"/",
                IndexHandler,
                {"pool": pool, "message": message, "config": config, "panel_enabled": panel_app is not None},
            ),
            (r"/ws", WebSocketHandler, {"config": config, "connections": set()}),
        ],
        websocket_max_message_size=MAX_MESSAGE_SIZE,
        websocket_ping_interval=WEBSOCKET_PING_INTERVAL,
        websocket_ping_timeout=WEBSOCKET_PING_TIMEOUT,
    )

    trusted = bool(panel_app and trust_proxy)
    server = tornado.httpserver.HTTPServer(
        ClientIpRouter(make_router(app, panel_app), trusted), xheaders=trusted, max_body_size=MAX_BODY_SIZE
    )
    bind_address = config.get("WEB_BIND_ADDRESS") or DEFAULT_BIND_ADDRESS
    _warn_if_exposed_without_auth(config, bind_address)
    server.listen(resolve_port(config.get("PORT")), address=bind_address)
    return panel_app


def _shutdown_panel(panel_app) -> None:
    """Cancela as tarefas do painel em andamento (setup/reset da Jukebox) ao encerrar o servidor."""
    if panel_app is None:
        return
    try:
        tornado.ioloop.IOLoop.current().run_sync(panel_app.settings["task_runner"].shutdown, timeout=5)
    except Exception as exc:
        logging.getLogger(__name__).warning("Falha ao encerrar as tarefas do painel (%s)", type(exc).__name__)


def start(pool: BotPool, message="", config: dict = None):
    if not config:
        config = load_config()
    panel_app = run_app(pool, message, config)
    try:
        tornado.ioloop.IOLoop.instance().start()
    finally:
        _shutdown_panel(panel_app)


if __name__ == "__main__":
    start(BotPool())
