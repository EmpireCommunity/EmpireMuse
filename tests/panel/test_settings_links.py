"""Painel, categoria Links → Playlists do servidor (item 17, A2, A4, A5, A6, A9, P-V7): a lista é UMA por servidor (a do principal), lida
e gravada só pelo store; importar só acrescenta; versão velha (comando × painel) é recusada; negado é 404 igual; exportar volta a lista."""

import copy
import html
import json
import re

from empiremuse.rules.playlists import Playlist, from_doc, sorted_by_order, to_export
from tests.panel.helpers_settings import NOT_FOUND_TEXT, UID, acao_form
from tests.panel.test_settings_http import HttpBase

MAX_PLAYLISTS = 25
BASE_URL = "https://example.com/"


def doc(name, order, url=None, description="uma descrição", genres=()):
    return {
        "order": order,
        "name": name,
        "description": description,
        "genres": list(genres),
        "url": url or f"{BASE_URL}{name.lower().replace(' ', '-')}",
    }


def full_list(count=MAX_PLAYLISTS):
    return [doc(f"L{i:02d}", i + 1, f"{BASE_URL}{i}") for i in range(count)]


class LinksBase(HttpBase):
    def get_app(self):
        app = super().get_app()
        self.refreshed = []
        self.pool.servers.playlists.refresh_idle = self.refreshed.append  # o embed ocioso é do store; aqui só se conta
        return app

    def links_url(self, gid, bot=None):
        return self.url(gid, "links", bot)

    def body_of(self, browser, gid):
        response = browser.get(self.links_url(gid))
        assert response.code == 200, f"GET {self.links_url(gid)} deveria ser 200, veio {response.code}"
        return response.body.decode()

    def act(self, browser, gid, acao, *, alvo=None, via=None, **overrides):
        """GET da página, escolhe o formulário `acao` (do alvo, se houver) e envia com `overrides`.

        `via` usa o formulário de OUTRA ação como base (quando o pedido nem aparece, como adicionar com a lista cheia).
        """
        form = acao_form(self.body_of(browser, gid), via or acao, alvo)
        assert form is not None, f"a página não oferece o formulário acao={(via or acao)!r} alvo={alvo!r}"
        if via:
            overrides = {"acao": acao, **overrides}
        return self.submit(browser, self.links_url(gid), form, **overrides)

    def names(self, gid=100):
        return self.world.server_playlists(gid)

    def add(self, browser, nome, url, descricao="", gid=100, **extra):
        return self.act(browser, gid, "adicionar", nome=nome, url=url, descricao=descricao, **extra)

    def snapshot(self, gid=100):
        return copy.deepcopy(self.world.database.servers[gid])

    def assert_untouched(self, before, writes):
        assert self.snapshot() == before and len(self.db.writes) == writes, "nada deveria ser gravado"


# ============================================================================ leitura (A4: a lista é a do principal)


class LinksReadTest(LinksBase):
    def test_the_list_is_the_servers_and_comes_from_the_main_bot_only(self):
        browser = self.logged_in()
        first, second = self.body_of(browser, 800), browser.get(self.links_url(800, bot=2)).body.decode()
        assert "example.com/um" in first and "example.com/dois" not in first  # "Dois" era do bot 2: ignorado
        assert "example.com/dois" not in second and "example.com/um" in second  # ?bot= não escolhe documento

    def test_only_valid_http_links_are_shown_with_rel_noopener_noreferrer(self):
        self.world.set_playlists(
            100,
            [
                doc("Ruim js", 1, "javascript:alert(1)"),
                doc("Ruim data", 2, "data:text/html,x"),
                doc("Ruim barra", 3, "//evil.example/x"),
                doc("Boa lista", 4, "https://ok.example/x"),
            ],
        )
        body = self.body_of(self.logged_in(), 100)
        hrefs = [html.unescape(h) for h in re.findall(r'<a\b[^>]*href="([^"]*)"[^>]*>', body)]
        assert not [h for h in hrefs if re.match(r"\s*(javascript|data|vbscript):", h, re.I) or h.startswith("//")]
        tags = re.findall(r'<a\b[^>]*href="https://ok\.example/x"[^>]*>', body)
        assert tags and all("noopener" in tag and "noreferrer" in tag for tag in tags)


# ============================================================================ adicionar, editar, remover


class LinksWriteTest(LinksBase):
    def test_a_write_goes_only_to_the_server_document_and_refreshes_the_idle_embed_once(self):
        bots_before = (self.db.doc("1", 100), self.db.doc("global", 100))
        response = self.add(self.logged_in(), "Rock", BASE_URL + "rock", "uma descrição", generos="Rock, Anos 80")
        assert response.code == 200 and self.names() == ["Fixo", "Rock"]
        assert (self.db.doc("1", 100), self.db.doc("global", 100)) == bots_before  # nada em player_controller.fav_links
        assert self.db.writes == [("servidor", 100)] and self.refreshed == [100]

    def test_invalid_fields_are_422_and_nothing_is_written(self):
        browser, before, writes = self.logged_in(), self.snapshot(), len(self.db.writes)
        invalid = (
            ("x", BASE_URL + "a"),
            ("Ro\nck", BASE_URL + "b"),
            ("Ro‮ck", BASE_URL + "c"),
            ("Rock", "javascript:alert(1)"),
            ("fixo", BASE_URL + "d"),  # nome repetido (casefold)
        )
        for nome, url in invalid:
            assert self.add(browser, nome, url).code == 422, (nome, url)
        assert self.add(browser, "Rock", BASE_URL + "e", ordem="abc").code == 422
        self.assert_untouched(before, writes)
        assert self.refreshed == []

    def test_adding_to_a_full_list_is_422_and_editing_still_works(self):
        self.world.set_playlists(100, full_list())
        browser, writes = self.logged_in(), len(self.db.writes)
        full = self.act(browser, 100, "adicionar", via="remover", alvo="L00", nome="Extra", url=BASE_URL + "extra")
        assert full.code == 422 and len(self.db.writes) == writes and len(self.names()) == MAX_PLAYLISTS
        assert self.act(browser, 100, "editar", alvo="L03", nome="L03", url=BASE_URL + "novo").code == 200
        assert len(self.names()) == MAX_PLAYLISTS

    def test_the_icon_is_one_emoji_and_an_edit_without_the_field_keeps_it(self):
        browser = self.logged_in()
        assert self.add(browser, "Rock", BASE_URL + "rock", icone="🎧").code == 200
        assert self.add(browser, "Pop", BASE_URL + "pop", icone="rock").code == 200  # inválido: salva sem ícone
        icons = lambda: {p["name"]: p.get("icon") for p in self.world.database.servers[100]["playlists"]}  # noqa: E731
        assert (icons()["Rock"], icons()["Pop"]) == ("🎧", None)

        form = acao_form(self.body_of(browser, 100), "editar", "Rock")
        old_page = [(key, value) for key, value in form.pairs() if key != "icone"]  # a página velha não manda o campo
        saved = self.submit(browser, self.links_url(100), form, pairs=old_page)
        assert saved.code == 200 and icons()["Rock"] == "🎧"

    def test_edit_cannot_rename_onto_another_playlist_and_remove_needs_an_existing_one(self):
        self.world.set_playlists(100, [doc("Rock", 1), doc("Pop", 2)])
        browser = self.logged_in()
        assert self.act(browser, 100, "editar", alvo="Pop", nome="rock", url=BASE_URL + "pop").code == 422
        assert self.act(browser, 100, "remover", alvo="Pop").code == 200 and self.names() == ["Rock"]
        form = acao_form(self.body_of(browser, 100), "remover", "Rock")
        before, writes = self.snapshot(), len(self.db.writes)
        assert self.submit(browser, self.links_url(100), form, alvo="Não existe").code == 422
        self.assert_untouched(before, writes)

    def test_other_fields_in_the_post_are_ignored(self):
        browser = self.logged_in()
        form = acao_form(self.body_of(browser, 100), "adicionar")
        bot_before, settings_before = self.db.doc("1", 100), self.world.server_settings(100)
        extra = [("djroles", "5001"), ("autoplay", "on"), ("rev", "99"), ("playlists", "[]")]
        pairs = form.pairs(nome="Rock", url=BASE_URL + "r") + extra
        assert self.submit(browser, self.links_url(100), form, pairs=pairs).code in (200, 422)
        assert self.world.server_settings(100) == settings_before and self.db.doc("1", 100) == bot_before


# ============================================================================ importar (A2) e exportar (A9)


class LinksImportExportTest(LinksBase):
    def do_import(self, browser, payload, gid=100):
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        return self.act(browser, gid, "importar", dados=raw)

    def test_import_only_adds_reports_what_was_skipped_and_drops_icons(self):
        self.world.set_playlists(100, [doc("Rock", 1, BASE_URL + "rock")])
        payload = {
            "playlists": [
                {"name": "ROCK", "url": BASE_URL + "outra"},  # nome repetido: pula, o existente não muda
                {"name": "Copia", "url": BASE_URL + "rock/"},  # URL repetida
                {"name": "Novo", "url": BASE_URL + "novo", "icon": "icons/novo.png"},
            ]
        }
        response = self.do_import(self.logged_in(), payload)
        body = response.body.decode()
        assert response.code == 200 and "1 adicionada" in body and "2 ignorada" in body
        assert self.names() == ["Rock", "Novo"]
        stored = self.snapshot()["playlists"]
        assert stored[0] == doc("Rock", 1, BASE_URL + "rock") and "icon" not in stored[1]

    def test_the_old_format_is_accepted(self):
        old = {"Antiga": {"url": BASE_URL + "antiga", "description": "formato antigo"}}
        assert self.do_import(self.logged_in(), old).code == 200 and self.names() == ["Fixo", "Antiga"]

    def test_nothing_new_or_a_bad_file_is_422_and_changes_nothing(self):
        browser, before, writes = self.logged_in(), self.snapshot(), len(self.db.writes)
        bad_files = ({"playlists": [{"name": "fixo", "url": BASE_URL + "z"}]}, "não é json", "[1, 2]", "")
        for bad in (*bad_files, {"playlists": [{"name": "x"}]}):
            assert self.do_import(browser, bad).code == 422, bad
        self.assert_untouched(before, writes)

    def test_a_file_over_the_size_limit_is_refused_and_changes_nothing(self):
        browser, before, writes = self.logged_in(), self.snapshot(), len(self.db.writes)
        huge = json.dumps({"playlists": [], "pad": "x" * 70_000})
        assert self.do_import(browser, huge).code in (413, 422)  # o limite de corpo do painel ou o do arquivo
        self.assert_untouched(before, writes)

    def test_export_is_the_server_list_without_icons_and_follows_the_same_authorization(self):
        self.world.set_playlists(100, [doc("Rock", 1, genres=["Rock"]), {**doc("Jazz", 2), "icon": "icons/jazz.png"}])
        browser = self.logged_in()
        response = browser.get("/painel/servidores/100/links/exportar?bot=2")  # ?bot= é ignorado
        assert response.code == 200 and 'filename="playlists-100.json"' in response.headers["Content-Disposition"]
        assert [p["name"] for p in json.loads(response.body)["playlists"]] == ["Rock", "Jazz"]
        assert "icon" not in response.body.decode()
        for path in (
            "/painel/servidores/300/links/exportar",
            "/painel/servidores/600/links/exportar",
            "/painel/servidores/500/links/exportar",
            "/painel/servidores/999999/links/exportar",
        ):
            denied = browser.get(path)
            assert denied.code == 404 and NOT_FOUND_TEXT in denied.body.decode(), path

    def test_export_then_import_into_an_empty_server_gives_the_same_list(self):
        self.world.set_playlists(100, [doc("Rock", 1, genres=["Rock", "80s"]), doc("Café", 2, description="")])
        browser = self.logged_in()
        exported = browser.get("/painel/servidores/100/links/exportar").body.decode()
        self.world.set_playlists(100, [])
        assert self.do_import(browser, exported).code == 200
        items = sorted_by_order(from_doc(d) for d in self.snapshot()["playlists"])
        assert json.loads(exported) == to_export(items)


# ============================================================================ A6 e P-V7: acesso e versão


class LinksProtocolTest(LinksBase):
    def test_anonymous_and_denied_guilds_get_the_same_not_found(self):  # A6
        assert self.browser().get(self.links_url(100)).code == 302
        browser = self.logged_in()
        assert browser.get(self.links_url(100)).code == 200, "a rota deve existir"
        for gid in (300, 600, 500, 999999):
            response = browser.get(self.links_url(gid))
            assert response.code == 404 and NOT_FOUND_TEXT in response.body.decode(), gid
            post = self.post_raw(browser, f"/painel/servidores/{gid}/links", [("acao", "remover"), ("alvo", "Fixo")])
            assert post.code == 404, gid
        assert self.db.writes == []

    def test_a_write_after_permission_was_revoked_is_404_inside_the_cache_window(
        self,
    ):  # A6: admin rebaixado com a página aberta
        browser = self.logged_in()
        form = acao_form(self.body_of(browser, 100), "remover", "Fixo")
        before, writes = self.snapshot(), len(self.db.writes)
        self.world.g100.members[UID].guild_permissions.administrator = False
        self.world.g100.members[UID].guild_permissions.manage_guild = False
        response = self.submit(browser, self.links_url(100), form, gap=2)
        assert response.code == 404 and NOT_FOUND_TEXT in response.body.decode()
        self.assert_untouched(before, writes)

    def test_a_change_by_the_command_between_get_and_post_is_a_conflict_and_nothing_is_lost(
        self,
    ):  # A5 painel × comando, P-V7
        browser = self.logged_in()
        form = acao_form(self.body_of(browser, 100), "adicionar")
        command_item = Playlist(0, "Do comando", "uma descrição", (), BASE_URL + "comando", None)
        self.io_loop.run_sync(lambda: self.pool.servers.playlists.add(100, command_item))  # o comando grava no meio
        writes = len(self.db.writes)

        response = self.submit(browser, self.links_url(100), form, nome="Do painel", url=BASE_URL + "painel")

        assert response.code == 409
        assert len(self.db.writes) == writes and self.names() == ["Fixo", "Do comando"]
        assert (
            self.add(browser, "Do painel", BASE_URL + "painel").code == 200
        )  # com a página recarregada, a escrita entra
        assert self.names() == ["Fixo", "Do comando", "Do painel"]
