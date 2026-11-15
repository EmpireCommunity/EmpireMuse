"""Página de status pública `GET /` (IndexHandler de web_app.py): links externos seguros, link do painel só com config válida e cabeçalhos de segurança.

Os testes sobem o servidor real (run_app) numa porta livre e usam HTTP de verdade.
"""

import asyncio
import re

import pytest
from types import SimpleNamespace

import tornado.httpclient
import tornado.httpserver
import tornado.testing
import tornado.websocket

from tests.panel.helpers import local_config, require


class _User:
    def __init__(self, bot_id, name):
        self.id, self._name = bot_id, name
        avatar = SimpleNamespace(url=f"https://cdn.discordapp.com/avatars/{bot_id}/avatar.png")
        self.display_avatar = SimpleNamespace(replace=lambda **kwargs: avatar)

    def __str__(self):
        return self._name


def make_bot(bot_id, name, *, ready=True, guilds=3):
    async def wait_until_ready():
        return None

    return SimpleNamespace(
        user=_User(bot_id, name),
        guilds=[object()] * guilds,
        identifier=f"id-{name}",
        config={"INVITE_PERMISSIONS": 332892794064},
        is_ready=lambda: ready,
        wait_until_ready=wait_until_ready,
    )


def make_pool(config, bots=(), failed=None, killing=False, commit=""):
    config = {"ENABLE_RPC_AUTH": False, "DEFAULT_PREFIX": "!", "INVITE_PERMISSIONS": 332892794064, **config}
    return SimpleNamespace(
        bots=list(bots),
        database=None,
        config=config,
        killing_state=killing,
        failed_bots=dict(failed or {}),
        commit=commit,
        remote_git_url="https://github.com/x/y",
    )


def get_index(
    monkeypatch,
    *,
    bots=(),
    failed=None,
    killing=False,
    commit="",
    path="/",
    ws=False,
    runtime_panel_url=None,
    **overrides,
):
    web_app = require("empiremuse.web_app")
    config = local_config(PORT=0, **overrides)
    pool = make_pool(config, bots, failed, killing, commit)
    real = tornado.httpserver.HTTPServer
    seen = {}

    class Recorder(real):
        def listen(self, port, address=""):
            sock, seen["port"] = tornado.testing.bind_unused_port()
            self.add_socket(sock)

    monkeypatch.setattr(web_app.tornado.httpserver, "HTTPServer", Recorder)

    async def main():
        web_app.run_app(pool, config=pool.config)
        if runtime_panel_url is not None:
            pool.public_panel_url = runtime_panel_url
        if ws:
            connection = await tornado.websocket.websocket_connect(f"ws://127.0.0.1:{seen['port']}/ws")
            connection.close()
            return connection
        client = tornado.httpclient.AsyncHTTPClient()
        try:
            return await client.fetch(
                f"http://127.0.0.1:{seen['port']}{path}", raise_error=False, follow_redirects=False
            )
        finally:
            client.close()

    return asyncio.run(main())


def page(monkeypatch, **kwargs) -> str:
    response = get_index(monkeypatch, **kwargs)
    assert response.code == 200, f"GET / deveria ser 200, veio {response.code}"
    return response.body.decode()


READY = (make_bot(111, "Bot Um", guilds=7),)


class TestPoolStatus:
    def test_pending_and_failed_bots_are_reported(self, monkeypatch):
        pending = make_bot(222, "Bot Dois", ready=False)
        body = page(monkeypatch, bots=(pending,), failed={"TOKEN_RESERVE_9": "Token inválido"})
        assert "id-Bot Dois" in body and "Recarregue a página" in body
        assert "TOKEN_RESERVE_9" in body and "Token inválido" in body


class TestLinks:
    def test_no_panel_link_when_the_panel_config_is_invalid(self, monkeypatch):
        assert "/painel" not in page(monkeypatch, PANEL_ENABLED=True, PANEL_BASE_URL="lixo")

    def test_link_externo_no_status_nao_aponta_ao_generico_nem_ao_favicon_antigo(self, monkeypatch):
        from empiremuse.api.runtime import ExternalApiStatus

        monkeypatch.setattr(
            "empiremuse.api.runtime.start_external_api",
            lambda *args: ExternalApiStatus(server=object(), panel_url="https://muse.empirecommunity.org/panel"),
        )
        body = page(monkeypatch, bots=READY, PANEL_ENABLED=True)
        assert 'href="https://muse.empirecommunity.org/panel"' in body
        assert "/painel" not in body

    @pytest.mark.regression
    @pytest.mark.parametrize("runtime_url", ["https://runtime.example/panel", ""])
    def test_status_usa_url_do_pool_sem_fallback_para_configuracao(self, monkeypatch, runtime_url):
        body = page(monkeypatch, bots=READY, runtime_panel_url=runtime_url)
        if runtime_url:
            assert f'href="{runtime_url}"' in body
        assert 'href="/painel/"' not in body
        assert 'href="http://localhost:8080/painel"' not in body

    def test_external_links_open_safely(self, monkeypatch):
        body = page(monkeypatch, bots=READY)
        for tag in re.findall(r'<a\b[^>]*target="_blank"[^>]*>', body):
            assert "noopener" in tag, f"target=_blank sem rel=noopener: {tag}"


# ============================================================================ BAIXO-1: cabeçalhos de segurança de `/`


class TestSecurityHeadersOnIndex:
    """Contrato do MAESTRO (item 8): nosniff, X-Frame-Options DENY (ou frame-ancestors), Referrer-Policy — sem quebrar
    /ws nem o painel. CSP completa não serve: a página usa <style> e <script> embutidos."""

    def test_nosniff_frame_denial_and_referrer_policy(self, monkeypatch):
        headers = get_index(monkeypatch, bots=READY).headers
        assert headers.get("X-Content-Type-Options") == "nosniff"
        csp = headers.get("Content-Security-Policy", "")
        assert headers.get("X-Frame-Options") == "DENY" or re.search(r"frame-ancestors\s+'none'", csp), (
            "a página não pode ser embutida em iframe (clickjacking)"
        )
        assert headers.get("Referrer-Policy") in {
            "no-referrer",
            "same-origin",
            "strict-origin",
            "strict-origin-when-cross-origin",
            "origin-when-cross-origin",
        }
