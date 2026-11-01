"""Rotas /painel/servidores e /painel/servidores/<gid> (Etapa 3): autorização no servidor, lista, convites, 404 genérico."""

import html
import re
from urllib.parse import parse_qs, urlsplit

import tornado.web

from tests.panel.helpers import COOKIE_SECRET, OAUTH_SECRET, require
from tests.panel.helpers_login import make_fake_oauth
from tests.panel.helpers_servers import USER_ID, FakeBot, FakeGuild, FakePool, raw_guild
from tests.panel.test_login_flow import LoginBase
import pytest

pytestmark = pytest.mark.critical

CATEGORIES = ["Player", "Canal de pedidos", "DJ", "Aparência", "Playlists", "Geral"]
UID = int(USER_ID)


def invites(body: str):
    """Lista de dicts de parâmetros de todos os convites (href para discord.com/oauth2/authorize) da página."""
    found = []
    for href in re.findall(r'href="(https://discord\.com/oauth2/authorize[^"]*)"', body):
        parts = urlsplit(html.unescape(href))
        found.append(
            {
                "base": f"{parts.scheme}://{parts.netloc}{parts.path}",
                "params": {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()},
            }
        )
    return found


class World:
    """Dois bots (Um=1, Dois=2) e servidores: 100 (bot 1), 200 (bot 2), 300 (nenhum), 400 (bot 1; falta o 2),
    600 (bot 1 mas usuário SEM permissão no bot), 700 (nenhum bot), 500 (fora da sessão: OAuth sem permissão)."""

    def __init__(self):
        self.guilds = {
            "100": FakeGuild("100", "Alfa", owner_id=999, icon_hash="icon100"),
            "200": FakeGuild("200", "Beta", owner_id=999),
            "400": FakeGuild("400", "Delta", owner_id=999),
            "600": FakeGuild("600", "Rebaixado", owner_id=999),
            "500": FakeGuild("500", "Fora", owner_id=999),
        }
        for gid in ("100", "200", "400"):
            self.guilds[gid].add_member(UID, administrator=True)
        self.guilds["600"].add_member(UID)  # membro comum no bot (OAuth ainda diz admin)
        self.guilds["500"].add_member(UID, administrator=True)
        self.bot1 = FakeBot(1, "Bot Um", guilds=[self.guilds[g] for g in ("100", "400", "600", "500")])
        self.bot2 = FakeBot(2, "Bot Dois", guilds=[self.guilds["200"]])
        self.pool = FakePool([self.bot1, self.bot2])
        self.raw = [
            raw_guild(100, "Alfa", icon="icon100", permissions="8"),
            raw_guild(200, "Beta", permissions="32"),
            raw_guild(300, "Gama", permissions="8"),
            raw_guild(400, "Delta", permissions="8"),
            raw_guild(600, "Rebaixado", permissions="8"),
            raw_guild(700, "SemBot", permissions="8"),
            raw_guild(500, "Fora", owner=False, permissions="1024"),  # filtrado no login
        ]


class ServersBase(LoginBase):
    raw_override = None

    def get_app(self):
        panel = require("empiremuse.panel")
        settings = require("empiremuse.panel.settings").load_panel_settings(self.config_factory())
        self.settings, self.world = settings, World()
        self.pool = self.world.pool
        raw = self.world.raw if self.raw_override is None else self.raw_override
        self.fake = make_fake_oauth(settings, guilds=raw)
        app = panel.build_panel_app(self.pool, settings, oauth_client=self.fake, clock=self.clock)
        return panel.make_router(tornado.web.Application([]), app)

    def logged_in(self):
        browser = self.browser()
        assert self.login(browser).code == 302
        return browser

    def page(self, path, browser=None):
        return (browser or self.logged_in()).get(path)


# ============================================================================ acesso anônimo


class AnonymousTest(ServersBase):
    def test_login_with_next_returns_to_the_servers_page(self):
        browser = self.browser()
        response = self.login(browser, next_="/painel/servidores")
        assert response.code == 302 and urlsplit(response.headers["Location"]).path == "/painel/servidores"

    def test_anonymous_request_makes_no_discord_calls(self):
        response = self.browser().get("/painel/servidores/100")
        assert response.code == 302, "anônimo deveria ser redirecionado ao login"
        assert self.pool.api_calls() == 0


# ============================================================================ lista


class ServerListTest(ServersBase):
    def test_configure_links_only_for_guilds_with_a_pool_bot(self):
        body = self.page("/painel/servidores").body.decode()
        configure = set(re.findall(r'href="/painel/servidores/(\d+)"', body))
        assert configure == {"100", "200", "400", "600"}, f"links Configurar: {sorted(configure)}"
        assert "Configurar" in body

    def test_list_does_not_leak_secrets(self):
        response = self.page("/painel/servidores")
        assert response.code == 200, "a lista de servidores deve existir"
        for secret in (OAUTH_SECRET, COOKIE_SECRET):
            assert secret not in response.body.decode()


class ServerListEscapingTest(ServersBase):
    raw_override = [
        raw_guild(100, "<script>alert(1)</script>Alfa", permissions="8"),
        raw_guild(300, "<img src=x onerror=alert(2)>", permissions="8"),
    ]

    def test_guild_and_bot_names_are_html_escaped(self):
        self.world_bot_name = "<b>Bot</b>"
        self.pool.bots[1].user.name = "<i>Bot Dois</i>"
        body = self.page("/painel/servidores").body.decode()
        for raw in ("<script>alert(1)</script>", "<img src=x onerror=alert(2)>", "<i>Bot Dois</i>"):
            assert raw not in body, f"nome sem escape: {raw}"
        assert "&lt;script&gt;" in body


# ============================================================================ detalhe / autorização


class ServerDetailTest(ServersBase):
    def test_authorized_detail_shows_guild_bots_links_to_real_categories_and_coming_soon_for_the_rest(self):
        response = self.page("/painel/servidores/100")
        body = response.body.decode()
        assert response.code == 200 and "Alfa" in body and "Bot Um" in body
        for category in CATEGORIES:
            assert category in body, f"categoria ausente: {category}"
        hrefs = set(re.findall(r'href="(/painel/servidores/100/[a-z]+)(?:\?[^"]*)?"', body))
        assert hrefs == {
            f"/painel/servidores/100/{c}" for c in ("player", "dj", "aparencia", "geral", "links", "canal", "radios")
        }
        assert 'aria-disabled="true"' not in body, "nenhuma categoria fica desabilitada depois da E4c"
        assert not re.findall(r"em breve", body, re.I)
        found = invites(body)
        assert [i["params"]["client_id"] for i in found] == ["2"], "convite G-19 só do bot ausente (Bot Dois)"
        assert "Bot Dois" in body, "o convite nomeia o bot ausente"

    def test_detail_lists_every_ready_bot_present_in_the_guild(self):
        self.world.guilds["100"].add_member(UID, administrator=True)
        self.world.bot2.guilds[100] = self.world.guilds["100"]
        body = self.page("/painel/servidores/100").body.decode()
        assert "Bot Um" in body and "Bot Dois" in body

    def test_extra_path_segments_are_a_generic_404(self):
        browser = self.logged_in()
        assert browser.get("/painel/servidores/100").code == 200, "a rota base do detalhe deve existir"
        for path in ("/painel/servidores/100/extra", "/painel/servidores/100/../200", "/painel/servidores/100%2f.."):
            assert browser.get(path).code == 404, path


class NotFoundIsGenericTest(ServersBase):
    CASES = {
        "id válido fora da sessão": "999",
        "id não numérico": "abc",
        "id gigante": "9" * 25,
        "sem permissão no bot (rebaixado)": "600",
        "sem bot do pool": "700",
        "sem bot (convite apenas)": "300",
        "fora da sessão mas com bot e admin": "500",
    }

    def test_denied_ids_outside_the_session_make_no_discord_calls(self):
        browser = self.logged_in()
        assert browser.get("/painel/servidores/100").code == 200, "a rota do detalhe deve existir"
        before = self.pool.api_calls()
        for gid in ("999", "abc", "9" * 25, "500"):
            browser.get(f"/painel/servidores/{gid}")
        assert self.pool.api_calls() == before, "ids fora de session.guilds não podem gerar chamadas ao bot/Discord"


class ServerCacheAndRevocationTest(ServersBase):
    def test_second_read_within_30_seconds_uses_the_decision_cache(self):
        browser = self.logged_in()
        guild = self.world.guilds["100"]
        browser.get("/painel/servidores/100")
        first = guild.calls["get_member"]
        self.clock.advance(10)
        assert browser.get("/painel/servidores/100").code == 200
        assert guild.calls["get_member"] == first

    def test_user_who_left_the_guild_loses_access_after_the_cache_window(self):
        browser = self.logged_in()
        assert browser.get("/painel/servidores/100").code == 200
        del self.world.guilds["100"].members[UID]
        self.clock.advance(31)
        assert browser.get("/painel/servidores/100").code == 404
