"""Invariante anti-SSRF (contrato T-456 §6): o bot nunca faz chamada ao painel, só ao Discord."""

import pytest

from empiremuse.panel.oauth import DISCORD_API, DiscordOAuthClient
from tests.api.support import CLIENT_ID, CODE, USER, ApiTestCase, body_of

pytestmark = pytest.mark.critical


class RecordingSession:
    """Sessão HTTP falsa que só anota o destino de cada chamada."""

    def __init__(self, urls):
        self.urls = urls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def request(self, method, url, **kwargs):
        self.urls.append(url)
        return self

    status = 200

    async def json(self, content_type=None):
        if self.urls[-1].endswith("/oauth2/token"):
            return {"access_token": "tok"}
        if self.urls[-1].endswith("/users/@me"):
            return dict(USER)
        if self.urls[-1].endswith("/oauth2/token/revoke"):
            return {}
        return []


class NoPanelCallsTest(ApiTestCase):
    def test_login_completo_so_fala_com_o_discord(self):
        urls: list[str] = []
        self.oauth = DiscordOAuthClient(self.settings, CLIENT_ID, session_factory=lambda: RecordingSession(urls))

        token = body_of(self.login(CODE))["session_token"]
        self.api("GET", "/meta")
        self.api("GET", "/session", session=token)
        self.api("DELETE", "/session", session=token)

        assert urls and all(url.startswith(DISCORD_API + "/") for url in urls)
        assert not any(self.settings.base_url in url for url in urls)
