"""E4a — comportamento HTTP transversal das configurações do servidor (todas as categorias):
autorização (IDOR, bot, revogação), XSRF, limites, rate limit, htmx × página inteira, versão otimista (409),
concorrência, auditoria e navegação. Contrato: BRIEFING-PAINEL-E4.md (decisões 1–8 e rotas da E4a).
Os formulários são lidos do HTML renderizado e reenviados como um navegador faria (helpers_settings.parse_forms).
"""

import copy
import html
import logging
import re
from urllib.parse import urlencode

from tests.panel.helpers import EXPECTED_SECURITY_HEADERS
from tests.panel.helpers_servers import INVITE_PERMISSIONS
from tests.panel.helpers_settings import CATEGORIES, NOT_FOUND_TEXT, SAVED_TEXT, SettingsBase, invites, parse_forms
import pytest

pytestmark = pytest.mark.critical

INVITE_KEYS = {"client_id", "scope", "permissions", "guild_id", "disable_guild_select"}


def normalize(body: str) -> str:
    body = re.sub(r'(name="xsrf-token"\s+content=")[^"]*', r"\1X", body)
    return re.sub(r'(name="_xsrf"\s+value=")[^"]*', r"\1X", body)


def external_change(db, scope, gid, **fields):
    """Simula outro escritor (comando no Discord): altera o documento gravado sem passar pelo painel."""
    db.docs[(scope, gid)].update(fields)
    db.baseline[(scope, gid)] = copy.deepcopy(db.docs[(scope, gid)])


class Each:
    """Executa uma verificação para vários casos e reporta TODAS as falhas (AsyncHTTPTestCase é unittest)."""

    @staticmethod
    def run(cases, check):
        failures = []
        for case in cases:
            try:
                check(case)
            except AssertionError as exc:
                failures.append(f"{case!r}: {exc}")
        assert not failures, f"{len(failures)} caso(s) falharam:\n  " + "\n  ".join(failures)


class HttpBase(SettingsBase):
    def not_found_reference(self, browser):
        response = browser.get("/painel/servidores/999999")
        assert response.code == 404 and NOT_FOUND_TEXT in response.body.decode()
        return normalize(response.body.decode())

    def post_raw(self, browser, path, pairs, *, htmx=False, gap=4, token=True, headers=None):
        if gap:
            self.clock.advance(gap)
        merged = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
        if token:
            merged.update(self.xsrf_header(browser))
        if htmx:
            merged["HX-Request"] = "true"
        return browser.post(path, body=urlencode(pairs), headers=merged)

    def audit_text(self):
        return "\n".join(
            r.getMessage()
            for r in self.caplog.records
            if r.levelno == logging.INFO and (r.name.startswith("panel") or r.name.startswith("empiremuse.panel"))
        )

    def all_log_text(self):
        return "\n".join(r.getMessage() for r in self.caplog.records)


# ============================================================================ acesso e autorização


class AccessTest(HttpBase):
    def test_anonymous_post_changes_nothing_and_reads_nothing(self):
        browser = self.browser()
        token = self.xsrf_header(browser)
        for category in CATEGORIES:
            response = browser.post(
                f"/painel/servidores/100/{category}",
                body="versao=0&autoplay=on",
                headers={"Content-Type": "application/x-www-form-urlencoded", **token},
            )
            assert response.code in (302, 401, 403), f"{category}: {response.code}"
        assert self.db.writes == [] and self.db.reads == []

    def test_denied_guilds_are_the_same_generic_404_for_get_and_post(self):
        browser = self.logged_in()
        assert browser.get(self.url(100, "player")).code == 200, "a rota da categoria deve existir"
        reference = self.not_found_reference(browser)
        reads_before = len(self.db.reads)
        denied = {
            "sem bot do pool": "300",
            "membro comum no bot": "600",
            "fora da sessão (com bot e admin)": "500",
            "inexistente": "999",
        }

        def check(case):
            label, gid = case
            for category in CATEGORIES:
                response = browser.get(f"/painel/servidores/{gid}/{category}")
                assert response.code == 404, f"GET {category}: {response.code}"
                assert normalize(response.body.decode()) == reference, f"GET {category}: página diferente"
                post = self.post_raw(browser, f"/painel/servidores/{gid}/{category}", [("versao", "0" * 16)])
                assert post.code == 404, f"POST {category}: {post.code}"
                assert normalize(post.body.decode()) == reference, f"POST {category}: página diferente"

        def check_malformed(gid):  # nunca é um servidor: a rota recusa antes do handler, sem ler nada
            for category in CATEGORIES:
                assert browser.get(f"/painel/servidores/{gid}/{category}").code == 404, f"GET {category}"
                post = self.post_raw(browser, f"/painel/servidores/{gid}/{category}", [("versao", "0" * 16)])
                assert post.code == 404, f"POST {category}: {post.code}"

        Each.run(list(denied.items()), check)
        Each.run(["abc", "9" * 25], check_malformed)
        assert self.db.writes == [], "negado não grava"
        assert len(self.db.reads) == reads_before, "negado não pode nem ler dados do servidor"

    def test_server_settings_are_the_mains_and_the_bot_query_does_not_pick_another_document(self):
        browser = self.logged_in()
        _, first, _ = self.open_form(browser, 800, "player", "default_player_volume")
        _, second, _ = self.open_form(browser, 800, "player", "default_player_volume", bot=2)
        assert (
            first.value("default_player_volume") == second.value("default_player_volume") == "60"
        )  # o doc do bot 2 (80) é ignorado

    def assert_write_is_404_after(self, revoke):
        browser = self.logged_in()
        url, form, _ = self.open_form(browser, 100, "player", "default_player_volume")
        before, writes = self.world.server_settings(100), len(self.db.writes)
        revoke()  # sem avançar o relógio: ainda dentro da janela de 30 s do cache de leitura
        response = self.submit(browser, url, form, gap=2, default_player_volume="30")
        assert response.code == 404, f"POST após revogação deveria ser 404, veio {response.code}"
        assert NOT_FOUND_TEXT in response.body.decode()
        assert len(self.db.writes) == writes and self.world.server_settings(100) == before, "nada pode ser gravado"

    def test_write_after_bot_left_the_guild_is_404_inside_the_read_cache_window(self):
        self.assert_write_is_404_after(lambda: self.world.bot1.guilds.pop(100))


# ============================================================================ protocolo: XSRF, limites, rate limit


class ProtocolTest(HttpBase):
    def test_version_is_stable_between_reads_and_changes_only_with_the_forms_own_fields(self):
        browser = self.logged_in()
        _, first, _ = self.open_form(browser, 100, "player", "default_player_volume")
        _, again, _ = self.open_form(browser, 100, "player", "default_player_volume")
        assert first.value("versao") == again.value("versao")
        self.world.external_server_change(100, djroles=["5010"])  # campo de OUTRO formulário
        _, after_dj, _ = self.open_form(browser, 100, "player", "default_player_volume")
        assert after_dj.value("versao") == first.value("versao"), "versao do player não depende de djroles"
        self.world.external_server_change(100, autoplay=True)  # campo do próprio formulário
        _, after_own, _ = self.open_form(browser, 100, "player", "default_player_volume")
        assert after_own.value("versao") != first.value("versao")

    def test_post_without_xsrf_is_403_and_writes_nothing(self):
        browser = self.logged_in()

        def check(case):
            category, field = case
            url, form, _ = self.open_form(browser, 100, category, field)
            pairs = [p for p in form.pairs() if p[0] != "_xsrf"]
            writes = len(self.db.writes)
            response = self.submit(browser, url, form, pairs=pairs)
            assert response.code == 403, f"sem token: {response.code}"
            wrong = self.submit(browser, url, form, pairs=pairs + [("_xsrf", "token-errado")])
            assert wrong.code == 403, f"token errado: {wrong.code}"
            assert len(self.db.writes) == writes

        Each.run(
            [("player", "default_player_volume"), ("dj", "djroles"), ("aparencia", "skin"), ("geral", "prefix")], check
        )

    def test_malformed_version_is_rejected_without_writing(self):
        browser = self.logged_in()
        url, form, _ = self.open_form(browser, 100, "player", "default_player_volume")

        def check(bad):
            response = self.submit(browser, url, form, versao=bad, default_player_volume="30")
            assert response.code in (409, 422), f"status {response.code}"

        Each.run(["", "x", "0" * 15, "0" * 17, "G" * 16, "%00", "<script>"], check)
        assert self.db.writes == []

    def test_responses_have_security_headers_and_are_not_cached(self):
        browser = self.logged_in()
        url, form, page = self.open_form(browser, 100, "player", "default_player_volume")
        invalid = self.submit(browser, url, form, default_player_volume="7")
        conflict = self.submit(browser, url, form, versao="0" * 16, default_player_volume="30")
        for label, response in (("GET", page), ("422", invalid), ("409", conflict)):
            assert response.code in (200, 422, 409), f"{label}: {response.code}"
            for name, value in EXPECTED_SECURITY_HEADERS.items():
                assert response.headers.get(name) == value, f"{label}: {name}"
            assert response.headers.get("Cache-Control") == "no-store", label


# ============================================================================ htmx × página inteira, 409, concorrência


class ResponseShapeTest(HttpBase):
    def test_without_htmx_success_returns_the_whole_page(self):
        browser = self.logged_in()
        url, form, _ = self.open_form(browser, 100, "player", "default_player_volume")
        response = self.submit(browser, url, form, default_player_volume="70")
        body = response.body.decode()
        assert response.code == 200, f"status {response.code}"
        assert "<html" in body.lower() and SAVED_TEXT in html.unescape(body)
        assert any(f.has("default_player_volume") for f in parse_forms(body))

    def test_change_to_a_field_outside_the_form_is_not_a_conflict_and_is_preserved(self):
        browser = self.logged_in()
        url, form, _ = self.open_form(browser, 100, "player", "default_player_volume")
        self.world.external_server_change(100, djroles=["5010", "5011"])
        response = self.submit(browser, url, form, default_player_volume="30")
        assert response.code == 200, f"status {response.code}"
        doc = self.world.server_settings(100)
        assert doc["default_player_volume"] == 30 and doc["djroles"] == ["5010", "5011"], "sem lost update"


# ============================================================================ auditoria (log sem valores)


class AuditLogTest(HttpBase):
    def test_global_scope_is_named_and_prefix_value_never_logged(self):
        browser = self.logged_in()
        url, form, _ = self.open_form(browser, 100, "geral", "prefix")
        self.caplog.clear()
        assert self.submit(browser, url, form, prefix="Zq9!").code == 200
        text = self.audit_text()
        assert "global" in text.lower() and "geral" in text and "prefix" in text, text
        assert "Zq9!" not in self.all_log_text(), "o valor do prefixo não pode aparecer em nenhum log"


# ============================================================================ navegação e convites (G-19)


class NavigationTest(HttpBase):
    def test_overview_offers_invites_only_for_pool_bots_missing_from_the_guild(self):
        browser = self.logged_in()

        def clients(gid):
            found = invites(browser.get(f"/painel/servidores/{gid}").body.decode())
            for invite in found:
                assert invite["base"] == "https://discord.com/oauth2/authorize"
                params = invite["params"]
                assert set(params) == INVITE_KEYS and params["guild_id"] == str(gid)
                assert params["scope"] == "bot applications.commands"
                assert params["permissions"] == str(INVITE_PERMISSIONS) and params["disable_guild_select"] == "true"
            return sorted(i["params"]["client_id"] for i in found)

        assert clients(100) == ["2"], "servidor 100 só tem o bot 1: convite para o bot 2"
        assert clients(800) == [], "servidor 800 já tem os dois bots"
        assert clients(200) == ["1"], "servidor 200 só tem o bot 2: convite para o bot 1"
