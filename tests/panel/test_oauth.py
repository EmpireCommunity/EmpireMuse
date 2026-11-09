"""panel/oauth.py — PKCE, URL de autorização e cliente HTTP (com sessão aiohttp simulada)."""

import asyncio
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, parse_qsl, urlsplit

import aiohttp
import pytest

from tests.panel.helpers import OAUTH_SECRET, https_config, load_settings, local_config, require
from tests.panel.helpers_login import ACCESS_TOKEN, CLIENT_ID, CODE, GATE, AUTHORIZE_PARAM_KEYS, s256

pytestmark = pytest.mark.critical

UNRESERVED = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")


def _oauth():
    return require("empiremuse.panel.oauth")


# --------------------------------------------------------------------------- PKCE


def test_pkce_pair_follows_rfc7636_s256():
    verifier, challenge = _oauth().make_pkce_pair()
    assert UNRESERVED.match(verifier), f"code_verifier fora de RFC 7636: {verifier!r}"
    assert challenge == s256(verifier), "code_challenge deve ser BASE64URL(SHA256(verifier)) sem padding"
    assert "=" not in challenge


def test_pkce_pairs_are_unique():
    pairs = {_oauth().make_pkce_pair() for _ in range(50)}
    assert len(pairs) == 50


# --------------------------------------------------------------------------- authorize_url


def _client(config=None):
    settings = load_settings(config or https_config())
    return settings, _oauth().DiscordOAuthClient(settings, CLIENT_ID)


@pytest.mark.smoke
def test_authorize_url_has_exact_parameters():
    settings, client = _client()
    url = client.authorize_url("state-123", "challenge-456")
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == GATE
    params = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
    assert set(params) == AUTHORIZE_PARAM_KEYS, f"parâmetros diferentes do contrato: {sorted(params)}"
    assert params == {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "scope": "identify guilds",
        "state": "state-123",
        "redirect_uri": settings.redirect_uri,
        "code_challenge": "challenge-456",
        "code_challenge_method": "S256",
        "prompt": "none",
    }


def test_authorize_url_prompt_is_none_by_default_and_accepts_only_known_values():
    _, client = _client()
    prompt = lambda url: parse_qs(urlsplit(url).query)["prompt"][0]  # noqa: E731
    assert prompt(client.authorize_url("s", "c")) == "none"
    assert prompt(client.authorize_url("s", "c", "consent")) == "consent"
    with pytest.raises(ValueError):
        client.authorize_url("s", "c", "login")


def test_authorize_url_redirect_uri_comes_from_settings_only():
    _, client = _client(local_config())
    url = client.authorize_url("s", "c")
    assert parse_qs(urlsplit(url).query)["redirect_uri"] == ["http://localhost:8080/painel/callback"]


def test_authorize_url_encodes_special_characters_in_state():
    _, client = _client()
    url = client.authorize_url("a&b=c d", "x")
    assert parse_qs(urlsplit(url).query)["state"] == ["a&b=c d"], "state deve ser codificado na query"


# --------------------------------------------------------------------------- cliente HTTP com sessão simulada


class _Response:
    def __init__(self, status, payload, url):
        self.status, self._payload, self.url, self.headers = status, payload, url, {}

    @property
    def ok(self):
        return self.status < 400

    async def json(self, *args, **kwargs):
        return self._payload

    async def text(self, *args, **kwargs):
        return str(self._payload)

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(SimpleNamespace(real_url=self.url), (), status=self.status)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __await__(self):
        async def _self():
            return self

        return _self().__await__()


class _Session:
    """Imita o trecho de aiohttp.ClientSession usado pelo cliente (post/get/request, async with)."""

    def __init__(self, responder):
        self.responder, self.requests = responder, []

    def request(self, method, url, **kwargs):
        self.requests.append((method.upper(), str(url), kwargs))
        outcome = self.responder(method.upper(), str(url), kwargs)
        if isinstance(outcome, BaseException):
            raise outcome
        status, payload = outcome
        return _Response(status, payload, str(url))

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    async def close(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _real_client(responder):
    settings = load_settings(https_config())
    session = _Session(responder)
    client = _oauth().DiscordOAuthClient(settings, CLIENT_ID, session_factory=lambda: session)
    return client, session


def _form(kwargs) -> dict:
    data = kwargs.get("data")
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return {str(k): str(v) for k, v in data}
    if isinstance(data, (str, bytes)):
        return dict(parse_qsl(data.decode() if isinstance(data, bytes) else data))
    return {}


def _run(coro):
    return asyncio.run(coro)


def test_exchange_code_posts_form_with_pkce_and_returns_access_token():
    client, session = _real_client(lambda m, u, k: (200, {"access_token": ACCESS_TOKEN, "token_type": "Bearer"}))
    token = _run(client.exchange_code(CODE, "verifier-123"))
    method, url, kwargs = session.requests[0]
    form = _form(kwargs)
    assert token == ACCESS_TOKEN
    assert method == "POST" and urlsplit(url).netloc == "discord.com" and urlsplit(url).path == "/api/v10/oauth2/token"
    assert form["grant_type"] == "authorization_code" and form["code"] == CODE
    assert form["code_verifier"] == "verifier-123" and form["client_id"] == CLIENT_ID
    assert form["client_secret"] == OAUTH_SECRET
    assert form["redirect_uri"] == "https://painel.example.com/painel/callback"
    assert OAUTH_SECRET not in url and CODE not in url, "segredo/código não podem ir na query string"


@pytest.mark.parametrize(
    "outcome",
    [
        (400, {"error": "invalid_grant"}),
        OSError("rede"),
    ],
)
def test_exchange_code_failures_raise_discord_oauth_error_without_leaking(outcome):
    client, _ = _real_client(lambda m, u, k: outcome)
    with pytest.raises(_oauth().DiscordOAuthError) as excinfo:
        _run(client.exchange_code(CODE, "verifier-123"))
    message = str(excinfo.value)
    for secret in (OAUTH_SECRET, CODE, "verifier-123"):
        assert secret not in message, f"a mensagem do erro vaza {secret!r}"


def test_fetch_user_sends_bearer_token_and_returns_dict():
    client, session = _real_client(lambda m, u, k: (200, {"id": "1", "username": "tester"}))
    user = _run(client.fetch_user(ACCESS_TOKEN))
    method, url, kwargs = session.requests[0]
    assert user == {"id": "1", "username": "tester"}
    assert method == "GET" and urlsplit(url).path == "/api/v10/users/@me"
    assert kwargs.get("headers", {}).get("Authorization") == f"Bearer {ACCESS_TOKEN}"
    assert ACCESS_TOKEN not in url


def test_fetch_guilds_requests_limit_200_and_returns_list():
    client, session = _real_client(lambda m, u, k: (200, [{"id": "9"}]))
    guilds = _run(client.fetch_guilds(ACCESS_TOKEN))
    method, url, kwargs = session.requests[0]
    assert guilds == [{"id": "9"}]
    assert method == "GET" and urlsplit(url).path == "/api/v10/users/@me/guilds"
    query = dict(parse_qsl(urlsplit(url).query)) | {str(k): str(v) for k, v in (kwargs.get("params") or {}).items()}
    assert query.get("limit") == "200"
    assert kwargs.get("headers", {}).get("Authorization") == f"Bearer {ACCESS_TOKEN}"


@pytest.mark.parametrize(
    "method_name,outcome",
    [
        ("fetch_user", (401, {})),
        ("fetch_guilds", asyncio.TimeoutError()),
    ],
)
def test_fetch_failures_raise_discord_oauth_error_without_token(method_name, outcome):
    client, _ = _real_client(lambda m, u, k: outcome)
    with pytest.raises(_oauth().DiscordOAuthError) as excinfo:
        _run(getattr(client, method_name)(ACCESS_TOKEN))
    assert ACCESS_TOKEN not in str(excinfo.value)


def test_revoke_posts_token_to_revoke_endpoint():
    client, session = _real_client(lambda m, u, k: (200, {}))
    _run(client.revoke(ACCESS_TOKEN))
    method, url, kwargs = session.requests[0]
    form = _form(kwargs)
    assert method == "POST" and urlsplit(url).path == "/api/v10/oauth2/token/revoke"
    assert form.get("token") == ACCESS_TOKEN
    assert form.get("client_id") == CLIENT_ID and form.get("client_secret") == OAUTH_SECRET
    assert ACCESS_TOKEN not in url
