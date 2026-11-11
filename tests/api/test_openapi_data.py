"""Contrato das rotas de dados (B3, ``docs/api/openapi.yaml``): as respostas reais validam contra o arquivo e todo par
rota+status documentado é observado (o arquivo não promete resposta que o bot não dá)."""

import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from empiremuse.api.data import DataHandler
from empiremuse.utils.servers import ServerConfigUnavailable
from tests.api.support import body_of
from tests.api.support_data import NODE, OLD_VERSION, OTHER, STATION, USER, DataApiCase
from tests.api.test_openapi_contract import FOUNDATION_PATHS, SpecChecker, documented, load_spec

pytestmark = pytest.mark.critical

BASE = "/api/v1"
FAILURE = RuntimeError("falha inesperada")
PLAYER = "/guilds/100/settings/player"
SETTINGS = BASE + "/guilds/{guild_id}/settings/{category}"
PLAYER_VALUES = {
    "default_player_volume": 60,
    "autoplay": True,
    "check_other_bots_in_vc": False,
    "enable_restrict_mode": False,
}


class OpenApiDataContractTest(DataApiCase):
    spec = load_spec()
    seen: set[tuple[str, str, str]] = set()
    ran: set[str] = set()

    def setUp(self):
        super().setUp()
        self.ran.add(self._testMethodName)

    def expect(self, method, template, response, status, code=None):
        assert response.code == status, response.body
        if code is not None:
            assert body_of(response)["error"]["code"] == code
        self.seen.add(
            SpecChecker(self.spec).check(
                method, BASE + template if not template.startswith(BASE) else template, response
            )
        )

    # ------------------------------------------------------------------ o que toda rota de dados responde

    @contextmanager
    def bots_not_ready(self):
        for bot in self.world.pool.bots:
            bot.ready = False
        try:
            yield
        finally:
            for bot in self.world.pool.bots:
                bot.ready = True

    @contextmanager
    def config_unavailable(self):
        with patch.object(self.world.pool.servers, "settings", side_effect=ServerConfigUnavailable()):
            yield

    def crash_account(self):
        return patch.object(DataHandler, "account_context", side_effect=FAILURE)

    def crash_guild(self):
        return patch.object(DataHandler, "guild_access", side_effect=FAILURE)

    def cover_common(self, method, template, path, *, crash, body=None, unavailable=None):
        """401 sem sessão, 503 (se a rota tem), 500 (``crash`` faz o contexto da rota falhar) e, por último, 429 (o balde do
        usuário fica esgotado)."""
        write = method in ("PUT", "PATCH", "DELETE")
        no_session = self.api(method, path, body=body)
        self.expect(method, template, no_session, 401, "session_expired")
        if unavailable is not None:
            with unavailable():
                self.expect(method, template, self.call(method, path, body), 503, "bot_unavailable")
        with crash():
            self.expect(method, template, self.call(method, path, body), 500, "bot_unavailable")
        limiter = self.ctx.limits.user_write if write else self.ctx.limits.user_read
        while limiter.allow(USER):
            pass
        self.expect(method, template, self.call(method, path, body), 429, "rate_limited")
        self.advance(61)

    def version(self, path, form):
        return self.version_of(path, form)

    # ------------------------------------------------------------------ conta

    def test_account_read(self):
        self.expect("GET", "/account", self.call("GET", "/account"), 200)
        self.cover_common("GET", "/account", "/account", crash=self.crash_account, unavailable=self.bots_not_ready)

    def test_account_prefix(self):
        body = {"expected_version": self.version("/account", "prefixo"), "values": {"custom_prefix": "%"}}
        self.expect("PATCH", "/account/prefix", self.call("PATCH", "/account/prefix", body), 200)
        stale = self.call("PATCH", "/account/prefix", {**body, "expected_version": OLD_VERSION})
        self.expect("PATCH", "/account/prefix", stale, 409, "conflict")
        self.expect(
            "PATCH", "/account/prefix", self.call("PATCH", "/account/prefix", {"values": {}}), 400, "validation_failed"
        )
        plain = self.call("PATCH", "/account/prefix", raw_body="x", headers={"Content-Type": "text/plain"})
        self.expect("PATCH", "/account/prefix", plain, 415, "validation_failed")
        self.cover_common(
            "PATCH",
            "/account/prefix",
            "/account/prefix",
            body=body,
            crash=self.crash_account,
            unavailable=self.bots_not_ready,
        )

    def test_account_rpc(self):
        path = "/account/rpc"
        issued = self.call("PATCH", path, {"expected_version": self.version("/account", "rpc")})
        self.expect("PATCH", path, issued, 200)
        stale = self.call("PATCH", path, {"expected_version": OLD_VERSION})
        self.expect("PATCH", path, stale, 409, "conflict")
        self.expect("PATCH", path, self.call("PATCH", path, {}), 400, "validation_failed")
        plain = self.call("PATCH", path, raw_body="x", headers={"Content-Type": "text/plain"})
        self.expect("PATCH", path, plain, 415, "validation_failed")
        body = {"expected_version": self.version("/account", "rpc")}
        self.world.config["ENABLE_RPC_AUTH"] = False
        self.expect("PATCH", path, self.call("PATCH", path, body), 403, "forbidden")
        self.world.config["ENABLE_RPC_AUTH"] = True
        self.cover_common("PATCH", path, path, body=body, crash=self.crash_account, unavailable=self.bots_not_ready)

    def test_account_integrations(self):
        path = "/account/integrations"
        body = {
            "expected_version": self.version("/account", "integracoes"),
            "action": "remove",
            "values": {"nome": "Meu canal"},
        }
        self.expect("PATCH", path, self.call("PATCH", path, body), 200)
        self.expect("PATCH", path, self.call("PATCH", path, {**body, "expected_version": OLD_VERSION}), 409, "conflict")
        self.expect("PATCH", path, self.call("PATCH", path, {**body, "action": "x"}), 400, "validation_failed")
        plain = self.call("PATCH", path, raw_body="x", headers={"Content-Type": "text/plain"})
        self.expect("PATCH", path, plain, 415, "validation_failed")
        again = {**body, "expected_version": self.version("/account", "integracoes"), "values": {"nome": "Outro"}}
        self.cover_common("PATCH", path, path, body=again, crash=self.crash_account, unavailable=self.bots_not_ready)

    def test_account_lastfm(self):
        path = "/account/lastfm"
        self.world.link_lastfm()
        body = {"expected_version": self.version("/account", "lastfm"), "values": {"scrobble": True}}
        self.expect("PATCH", path, self.call("PATCH", path, body), 200)
        self.expect("PATCH", path, self.call("PATCH", path, {**body, "expected_version": OLD_VERSION}), 409, "conflict")
        self.expect(
            "PATCH", path, self.call("PATCH", path, {**body, "values": {"scrobble": 1}}), 400, "validation_failed"
        )
        plain = self.call("PATCH", path, raw_body="x", headers={"Content-Type": "text/plain"})
        self.expect("PATCH", path, plain, 415, "validation_failed")
        again = {**body, "expected_version": self.version("/account", "lastfm"), "values": {"scrobble": False}}
        self.cover_common("PATCH", path, path, body=again, crash=self.crash_account, unavailable=self.bots_not_ready)

    def test_account_favorites(self):
        path = "/account/favorites"
        self.expect("GET", path, self.call("GET", path), 200)
        self.cover_common("GET", path, path, crash=self.crash_account, unavailable=self.bots_not_ready)
        self.advance(61)
        add = {
            "expected_version": self.version(path, "favoritos"),
            "action": "add",
            "values": {"nome": "Pop", "url": "https://example.com/pop"},
        }
        self.expect("PUT", path, self.call("PUT", path, add), 200)
        self.expect("PUT", path, self.call("PUT", path, {**add, "expected_version": OLD_VERSION}), 409, "conflict")
        self.expect("PUT", path, self.call("PUT", path, {**add, "values": {}}), 400, "validation_failed")
        plain = self.call("PUT", path, raw_body="x", headers={"Content-Type": "text/plain"})
        self.expect("PUT", path, plain, 415, "validation_failed")
        again = {**add, "values": {"nome": "Rap", "url": "https://example.com/rap"}}
        again["expected_version"] = self.version(path, "favoritos")
        self.cover_common("PUT", path, path, body=again, crash=self.crash_account, unavailable=self.bots_not_ready)

    def test_account_favorite_removal(self):
        path = "/account/favorites"

        def query(version=None, name="Rock"):
            return f"{path}?name={name}&expected_version={version or self.version(path, 'favoritos')}"

        self.expect("DELETE", path, self.call("DELETE", query()), 200)
        self.expect("DELETE", path, self.call("DELETE", query(OLD_VERSION)), 409, "conflict")
        self.expect("DELETE", path, self.call("DELETE", f"{path}?name=x"), 400, "validation_failed")
        self.cover_common("DELETE", path, query(name="Jazz"), crash=self.crash_account, unavailable=self.bots_not_ready)

    def test_account_favorites_export(self):
        path = "/account/favorites/export"
        self.expect("GET", path, self.call("GET", path), 200)
        self.cover_common("GET", path, path, crash=self.crash_account, unavailable=self.bots_not_ready)

    def test_account_radio_favorites(self):
        path = "/account/radio-favorites"
        self.expect("GET", path, self.call("GET", path), 200)
        self.cover_common("GET", path, path, crash=self.crash_account, unavailable=self.bots_not_ready)
        self.advance(61)
        query = f"{path}?kind=station&id={STATION}"
        self.expect("DELETE", path, self.call("DELETE", query), 200)
        self.expect("DELETE", path, self.call("DELETE", f"{path}?kind=x&id=1"), 400, "validation_failed")
        self.call("DELETE", f"{path}?kind=filter&id={NODE}", key="chave-0001")
        reused = self.call("DELETE", f"{path}?kind=station&id={STATION}", key="chave-0001")
        self.expect("DELETE", path, reused, 409, "conflict")  # a mesma chave com outro pedido
        self.cover_common(
            "DELETE", path, f"{path}?kind=filter&id={NODE}", crash=self.crash_account, unavailable=self.bots_not_ready
        )

    # ------------------------------------------------------------------ servidores

    def test_guilds(self):
        self.expect("GET", "/guilds", self.call("GET", "/guilds"), 200)
        crash = lambda: patch("empiremuse.api.handlers_guilds.missing_invites", side_effect=FAILURE)  # noqa: E731
        self.cover_common("GET", "/guilds", "/guilds", crash=crash)

    def test_guild_detail(self):
        template = "/guilds/{guild_id}"
        self.expect("GET", template, self.call("GET", "/guilds/100"), 200)
        self.expect("GET", template, self.call("GET", "/guilds/300"), 404, "not_found")
        self.cover_common("GET", template, "/guilds/100", crash=self.crash_guild)

    def test_settings_read(self):
        self.expect("GET", SETTINGS, self.call("GET", PLAYER), 200)
        for category in ("dj", "appearance", "links", "general"):
            self.expect("GET", SETTINGS, self.call("GET", f"/guilds/100/settings/{category}"), 200)
        self.expect("GET", SETTINGS, self.call("GET", "/guilds/300/settings/player"), 404, "not_found")
        self.cover_common("GET", SETTINGS, PLAYER, crash=self.crash_guild, unavailable=self.config_unavailable)

    def test_settings_write(self):
        body = {"expected_version": self.version(PLAYER, "player"), "values": PLAYER_VALUES}
        self.expect("PUT", SETTINGS, self.call("PUT", PLAYER, body), 200)
        stale = self.call("PUT", PLAYER, {**body, "expected_version": OLD_VERSION})
        self.expect("PUT", SETTINGS, stale, 409, "conflict")
        self.expect("PUT", SETTINGS, self.call("PUT", PLAYER, {**body, "values": {}}), 400, "validation_failed")
        plain = self.call("PUT", PLAYER, raw_body="x", headers={"Content-Type": "text/plain"})
        self.expect("PUT", SETTINGS, plain, 415, "validation_failed")
        self.expect("PUT", SETTINGS, self.call("PUT", "/guilds/300/settings/player", body), 404, "not_found")
        # as outras formas de corpo (um formulário por vez) também validam contra o arquivo
        for path, form, values, extra in (
            ("/guilds/100/settings/dj", "dj", {"djroles": ["5002"]}, {}),
            ("/guilds/100/settings/general", "prefixo", {"prefix": "%"}, {"form": "prefixo"}),
            (
                "/guilds/100/settings/general",
                "status_voz",
                {"voice_status_permanent": False, "voice_channel_status": ""},
                {"form": "status_voz"},
            ),
            ("/guilds/100/settings/general", "idioma", {"language": None}, {"form": "idioma"}),
            (
                "/guilds/100/settings/appearance",
                "skin_servidor",
                {"skin": "neon", "static_skin": "green"},
                {"form": "skin_servidor"},
            ),
            (
                "/guilds/100/settings/appearance",
                "skin_global",
                {"global_skin": False, "player_skin": None, "player_skin_static": None},
                {"form": "skin_global"},
            ),
        ):
            response = self.call("PUT", path, {"expected_version": self.version(path, form), "values": values, **extra})
            self.expect("PUT", SETTINGS, response, 200)
        listen = "/guilds/800/settings/general"
        removal = {"form": "ouvir_junto", "action": "remove", "values": {"canal": "123"}}
        ok = self.call("PUT", listen, {**removal, "expected_version": self.version(listen, "ouvir_junto")})
        self.expect("PUT", SETTINGS, ok, 200)
        beta = "/guilds/200/settings/general"
        forbidden = self.call("PUT", beta, {**removal, "expected_version": self.version(beta, "ouvir_junto")})
        self.expect("PUT", SETTINGS, forbidden, 403, "forbidden")
        links = "/guilds/100/settings/links"
        add = {
            "action": "add",
            "scope": "jukebox",
            "values": {"nome": "Rock BR", "url": "https://example.com/rock", "generos": ["rock"], "ordem": None},
        }
        added = self.call("PUT", links, {**add, "expected_version": self.version(links, "links")})
        self.expect("PUT", SETTINGS, added, 200)
        imported = {
            "action": "import",
            "scope": "radio",
            "values": {"dados": {"playlists": [{"name": "Rádio", "url": "https://example.com/r"}]}},
        }
        self.expect(
            "PUT",
            SETTINGS,
            self.call("PUT", links, {**imported, "expected_version": self.version(links, "links")}),
            200,
        )
        again = {"expected_version": self.version(PLAYER, "player"), "values": {**PLAYER_VALUES, "autoplay": False}}
        self.cover_common(
            "PUT", SETTINGS, PLAYER, body=again, crash=self.crash_guild, unavailable=self.config_unavailable
        )

    def test_links_export(self):
        template = "/guilds/{guild_id}/links/export"
        path = "/guilds/100/links/export"
        self.expect("GET", template, self.call("GET", path), 200)
        self.expect("GET", template, self.call("GET", path + "?scope=radio"), 200)
        self.expect("GET", template, self.call("GET", path + "?scope=x"), 400, "validation_failed")
        self.expect("GET", template, self.call("GET", "/guilds/300/links/export"), 404, "not_found")
        playlists = self.world.pool.servers.playlists
        with patch.object(playlists, "export_bytes", side_effect=ServerConfigUnavailable()):
            self.expect("GET", template, self.call("GET", path), 503, "bot_unavailable")
        with patch.object(playlists, "export_bytes", side_effect=FAILURE):
            self.expect("GET", template, self.call("GET", path), 500, "bot_unavailable")
        no_session = self.api("GET", path)
        self.expect("GET", template, no_session, 401, "session_expired")
        while self.ctx.limits.user_read.allow(USER):
            pass
        self.expect("GET", template, self.call("GET", path), 429, "rate_limited")

    # ------------------------------------------------------------------ o arquivo

    def test_exemplos_de_resposta_nao_levam_segredo_nem_campo_fora_do_arquivo(self):
        listing = self.call("GET", "/account")
        self.expect("GET", "/account", listing, 200)
        assert "token" not in json.dumps(body_of(listing)["forms"]["rpc"])  # só ``disponivel`` e ``configurado``
        assert OTHER not in listing.body.decode()

    def test_zz_todo_status_documentado_das_rotas_de_dados_foi_exercitado(self):
        casos = {name for name in dir(self) if name.startswith("test_") and not name.startswith("test_zz")}
        if not casos <= self.ran:
            pytest.skip("só vale com todos os casos da classe rodando (o ``-k`` deixa casos de fora)")

        b4 = ("/jukebox", "/radios", "/radio-stations")  # as rotas do B4 têm o contrato delas (``test_openapi_b4``)
        erasure = (
            BASE + "/account/erasure"
        )  # as rotas de apagar dados (B5) têm o contrato delas (``test_openapi_erasure``)
        faltando = {
            item
            for item in documented(self.spec)
            if item[1] not in FOUNDATION_PATHS and not any(p in item[1] for p in b4) and not item[1].startswith(erasure)
        } - self.seen

        assert not faltando, f"documentados e nunca observados: {sorted(faltando)}"
