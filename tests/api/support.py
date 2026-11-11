"""Servidor da API em teste: ``AsyncHTTPTestCase`` com relógios falsos, OAuth falso e pool de dublê (nada de rede real)."""

import asyncio
import json
from types import SimpleNamespace

from tornado.testing import AsyncHTTPTestCase

from empiremuse.api.context import ApiContext
from empiremuse.api.idempotency import IdempotencyStore
from empiremuse.api.keys import ServiceKeys
from empiremuse.api.limits import ApiLimits
from empiremuse.api.server import API_PREFIX, ApiHTTPServer, build_api_app
from empiremuse.api.settings import load_external_panel_settings
from empiremuse.api.signing import PURPOSE_ERASURE, derive_key
from empiremuse.panel.authz import GuildAuthorizer
from empiremuse.panel.oauth import DiscordOAuthClient, DiscordOAuthError
from empiremuse.panel.services.tokens import SignedTokens
from empiremuse.panel.settings import load_session_limits
from empiremuse.panel.state import ensure_panel_state
from tests.api.helpers import NOW, SERVICE_KEY, WEB_PORT, external_config

CLIENT_ID = "123456789012345678"
CODE = "codigo-do-discord-segredo-1"
VERIFIER = "v" * 50
ACCESS_TOKEN = "token-do-discord-segredo-9"
USER = {"id": "1001", "username": "alice", "global_name": "Alice", "avatar": "av1"}
GUILDS = [
    {"id": "1", "name": "Dono", "icon": None, "owner": True, "permissions": "0"},
    {"id": "2", "name": "Admin", "icon": "ic", "owner": False, "permissions": str(0x8)},
    {"id": "3", "name": "Membro", "icon": None, "owner": False, "permissions": "0"},  # sem gerência: filtrado
]


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeOAuth(DiscordOAuthClient):
    """Cliente OAuth sem rede: `identities` mapeia o ``code`` ao usuário; `fail_at` ∈ {exchange, user, guilds, revoke}."""

    def __init__(self, settings):
        super().__init__(settings, CLIENT_ID)
        self.calls: list[str] = []
        self.identities: dict[str, dict] = {CODE: USER}
        self.fail_at: str | None = None
        self.fail_status: int | None = None
        self.issued: dict[str, dict] = {}
        self.guilds: list[dict] = GUILDS  # os servidores que o Discord devolve (um teste pode trocar)
        self.before_exchange = (
            None  # chamado (sem await) durante a troca, para simular o que acontece enquanto o Discord responde
        )
        self.hold_until = 0  # segura a troca até chegarem tantas requisições (prova a concorrência de verdade)
        self._arrived = 0
        self._gate: asyncio.Event | None = None

    async def exchange_code(self, code, code_verifier):
        self.calls.append("exchange")
        if self.hold_until:
            self._gate = self._gate or asyncio.Event()
            self._arrived += 1
            if self._arrived >= self.hold_until:
                self._gate.set()
            await self._gate.wait()
        if self.before_exchange is not None:
            self.before_exchange()
        if code_verifier != VERIFIER:  # o verifier tem de chegar ao Discord igual ao do corpo
            raise DiscordOAuthError("verifier errado", 400)
        if self.fail_at == "exchange" or code not in self.identities:
            raise DiscordOAuthError("recusado", self.fail_status if self.fail_at == "exchange" else 400)
        token = f"{ACCESS_TOKEN}-{len(self.issued)}"
        self.issued[token] = self.identities.pop(code)  # o código é de uso único
        return token

    async def fetch_user(self, access_token):
        self.calls.append("user")
        if self.fail_at == "user":
            raise DiscordOAuthError("falha", 500)
        return dict(self.issued[access_token])

    async def fetch_guilds(self, access_token):
        self.calls.append("guilds")
        if self.fail_at == "guilds":
            raise DiscordOAuthError("falha", 500)
        return list(self.guilds)

    async def revoke(self, access_token):
        self.calls.append("revoke")
        if self.fail_at == "revoke":
            raise DiscordOAuthError("falha", 500)


class ApiTestCase(AsyncHTTPTestCase):
    """Base: ``self.api(...)`` manda a chave de serviço (e o token de sessão quando dado)."""

    config_overrides: dict = {}
    networks: tuple = ()

    def get_app(self):
        self.clock = FakeClock()
        self.wall = FakeClock(NOW)
        self.pool = self.make_pool()
        config = external_config(**self.config_overrides)
        self.settings = load_external_panel_settings(config, web_port=WEB_PORT, now=NOW)
        state = ensure_panel_state(self.pool, load_session_limits(config), clock=self.clock)
        self.store = state.session_store
        self.oauth = FakeOAuth(self.settings)
        self.ctx = ApiContext(
            settings=self.settings,
            keys=ServiceKeys(self.settings.service_key, self.settings.previous_key, self.settings.previous_expires_at),
            store=self.store,
            limits=ApiLimits(self.clock),
            oauth_provider=lambda: self.oauth,
            pool=self.pool,
            clock=self.clock,
            wall_clock=self.wall,
            bot_version=lambda: "9.9.9",
            authorizer=GuildAuthorizer(self.pool, clock=self.clock),
            settings_service=state.settings_service,
            account_service=state.account_service,
            idempotency=IdempotencyStore(self.clock),
            erase_tokens=SignedTokens(derive_key(self.settings.signing_secret, PURPOSE_ERASURE), self.clock),
        )
        self.store.add_user_listener(self.ctx.idempotency.purge_user)
        return build_api_app(self.ctx)

    def make_pool(self):
        return SimpleNamespace(bots=[SimpleNamespace(application_id=CLIENT_ID)], database=None)

    def advance(self, seconds: float) -> None:
        """Avança o relógio monótono do store e o de parede (as datas da resposta seguem os dois)."""
        self.clock.advance(seconds)
        self.wall.advance(seconds)

    def get_http_server(self):
        return ApiHTTPServer(self._app, self.networks, clock=self.clock)

    def api(self, method, path, *, key=SERVICE_KEY, session=None, body=None, headers=None, raw_body=None):
        hdrs = {"Accept": "application/json"}
        if key is not None:
            hdrs["Authorization"] = f"Bearer {key}"
        if session is not None:
            hdrs["X-EM-Session"] = session
        payload = raw_body
        if body is not None:
            payload = json.dumps(body)
            hdrs["Content-Type"] = "application/json"
        if payload is None and method in ("POST", "PUT", "PATCH"):
            payload = ""
        hdrs.update(headers or {})
        return self.fetch(API_PREFIX + path, method=method, headers=hdrs, body=payload, raise_error=False)

    def login(self, code=CODE, *, key=SERVICE_KEY, **extra):
        response = self.api("POST", "/sessions", key=key, body={"code": code, "code_verifier": VERIFIER, **extra})
        return response

    def new_session(self, user_id="1001", key=SERVICE_KEY):
        code = f"codigo-{user_id}-{len(self.oauth.issued)}"
        self.oauth.identities[code] = {**USER, "id": user_id, "username": f"u{user_id}"}
        response = self.login(code, key=key)
        assert response.code == 201, response.body
        return json.loads(response.body)["session_token"]


def body_of(response) -> dict:
    return json.loads(response.body)


def error_code(response) -> str:
    return body_of(response)["error"]["code"]
