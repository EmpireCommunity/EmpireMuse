"""J3 · Rádios › Favoritas do servidor (painel): lista, Remover com confirmação e Adicionar pelo seletor de local.

Caminhos de risco (porta de admin): ator é o usuário da SESSÃO (``user_id``/``guild_id`` do corpo são ignorados, o
servidor é o da rota autorizada), sessão e XSRF obrigatórios, o gate ``can_manage_guild`` vale na tela e no store, o nome
da estação é relido no diretório (nunca vem do formulário), e cada estado do resultado tem o seu status: adicionada,
já existia, cheia (409), não encontrada (404, refazer), indisponível (503, tentar de novo) e proibida (403). Backend real
(``RadioFavorites``) sobre banco em memória; só o diretório, a árvore e o Discord são dublês.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from empiremuse.panel.handlers.settings import MAX_BODY_BYTES, WRITE_LIMIT_PER_MINUTE
from empiremuse.panel.services.radio_favorites import ServerFavoritesPage
from empiremuse.rules.radio_stations import Station
from empiremuse.utils.radio.directory import DirectoryError
from empiremuse.utils.radio.favorites import FAVORITES_DB, MAX_FAVORITES, FavoriteForbidden, RadioFavorites
from tests.panel.helpers_radio import CAMPINAS, SP
from tests.panel.helpers_settings import acao_forms, parse_forms
from tests.panel.test_radio_page import ALFA, RadioBase
from tests.persistence.test_radio_store import FakeDb

STREAM = "https://secret.example/stream"
HOSTILE = "Rádio <i>Alfa</i> @everyone https://evil.example"


def uuid(n):
    return f"{n:08d}-0000-0000-0000-{n:012d}"


def station(n, name=None, country="Brasil"):
    return Station(uuid(n), name or f"Estação {n}", STREAM, "MP3", 128, country)


class FavoritesBase(RadioBase):
    def get_app(self):
        app = super().get_app()
        self.fav_db = FakeDb()
        pool_like = SimpleNamespace(database=self.fav_db, get_all_bots=self.pool.get_all_bots)
        self.radio.favorites = RadioFavorites(pool_like)
        self.radio.directory.stations.update({uuid(n): station(n) for n in range(1, 40)})
        self.radio.directory.stations[uuid(1)] = station(1, HOSTILE)
        return app

    def add_direct(self, *numbers, guild=ALFA):
        async def run():
            for n in numbers:
                await self.radio.favorites.add(guild, 1001, self.radio.directory.stations[uuid(n)])

        self.io_loop.run_sync(run)

    def saved(self, guild=ALFA):
        doc = self.fav_db.tables.get(FAVORITES_DB, {}).get(str(guild))
        return [s["uuid"] for s in doc["stations"]] if doc else []

    def post(self, browser, pairs, *, htmx=True, token=True, gid=ALFA, gap=4):
        return self.post_raw(browser, f"{self.url(gid)}/favoritas", pairs, htmx=htmx, token=token, gap=gap)

    def xsrf(self, browser):
        form = next(f for f in parse_forms(self.page(browser)) if f.has("_xsrf"))
        return form.value("_xsrf")

    def act(self, browser, acao, number=None, *, extra=(), token=True, htmx=True, **kwargs):
        pairs = [("acao", acao)]
        if number is not None:
            pairs.append(("estacao", uuid(number)))
        pairs += list(extra)
        if token:
            pairs.append(("_xsrf", self.xsrf(browser)))
        return self.post(browser, pairs, htmx=htmx, token=token, **kwargs)

    def local(self, browser, node="", page=None, *, htmx=True):
        query = f"?node={node}" + (f"&pagina={page}" if page else "")
        headers = {"HX-Request": "true"} if htmx else {}
        return browser.get(f"{self.url(ALFA)}/favoritas/local{query}", headers=headers)


class FavoritesPageTest(FavoritesBase):
    def test_admin_sees_the_list_escaped_with_confirmation_on_remove_and_the_location_selector(self):
        self.add_direct(1, 2)
        body = self.page(self.logged_in())
        assert 'id="radio-favoritas"' in body and "Favoritas do servidor" in body and "2 de 24 estações" in body
        assert "<i>" not in body and "@everyone" not in body and "https://evil.example" not in body
        forms = acao_forms(body, "remover")
        assert [f.value("estacao") for f in forms] == [uuid(1), uuid(2)] and all(f.has("_xsrf") for f in forms)
        assert body.count("hx-confirm=") >= 2 and "Escolha o local" in body and STREAM not in body

    def test_the_list_is_the_servers_own_and_empty_says_so(self):
        self.radio.premium.grant(800)
        self.add_direct(3, guild=800)
        body = self.page(self.logged_in())
        assert "O servidor ainda não tem estações favoritas." in body and "Estação 3" not in body

    def test_the_block_is_hidden_from_someone_who_does_not_manage_the_server(self):
        page = ServerFavoritesPage()
        ctx = SimpleNamespace(
            pool=SimpleNamespace(
                radio=SimpleNamespace(favorites=self.radio.favorites, locations=None),
                get_all_bots=lambda: [],  # ninguém acha o membro: o gate nega
            ),
            guild_id=ALFA,
            user_id="1001",
        )
        assert self.io_loop.run_sync(lambda: page.view(ctx, "/x")) is None
        ctx.user_id = "não-numérico"  # sem ator na sessão também esconde
        assert self.io_loop.run_sync(lambda: page.view(ctx, "/x")) is None

    def test_without_the_favorites_store_the_page_still_renders_without_the_block(self):
        del self.radio.favorites
        body = self.page(self.logged_in())
        assert "radio-favoritas" not in body


class FavoritesAddTest(FavoritesBase):
    def test_add_stores_the_name_read_from_the_directory_never_from_the_form(self):
        browser = self.logged_in()
        response = self.act(browser, "adicionar", 2, extra=[("nome", "NOME FORJADO"), ("name", "FORJADO")])
        body = response.body.decode()
        assert response.code == 200 and "entrou nas favoritas do servidor" in body and "<html" not in body.lower()
        doc = self.fav_db.tables[FAVORITES_DB][str(ALFA)]
        assert doc["stations"] == [{"uuid": uuid(2), "name": "Estação 2"}]  # só uuid e nome limpo, nunca o endereço
        assert self.radio.directory.by_uuid_calls == [uuid(2)] and "FORJADO" not in str(self.fav_db.tables)

    def test_the_actor_and_the_server_come_from_the_session_and_the_route_not_the_body(self):
        browser = self.logged_in()
        response = self.act(
            browser, "adicionar", 2, extra=[("user_id", "999"), ("guild_id", "800"), ("actor_id", "999")]
        )
        assert response.code == 200
        assert self.saved(ALFA) == [uuid(2)] and self.saved(800) == []

    def test_an_already_saved_station_is_not_duplicated(self):
        self.add_direct(2)
        response = self.act(self.logged_in(), "adicionar", 2)
        assert response.code == 200 and "já está nas favoritas do servidor" in response.body.decode()
        assert self.saved() == [uuid(2)]

    def test_a_full_list_is_409_and_writes_nothing(self):
        self.add_direct(*range(2, 2 + MAX_FAVORITES))
        before = self.saved()
        response = self.act(self.logged_in(), "adicionar", 30)
        assert response.code == 409 and "já têm 24 estações" in response.body.decode()
        assert self.saved() == before and len(before) == MAX_FAVORITES

    def test_a_station_the_directory_does_not_know_is_404_to_redo_and_writes_nothing(self):
        response = self.act(self.logged_in(), "adicionar", 99)
        assert response.code == 404 and "Escolha o local de novo" in response.body.decode()
        assert self.saved() == []

    def test_directory_down_is_503_to_retry_and_is_not_blamed_on_the_station(self):
        self.radio.directory.by_uuid = AsyncMock(side_effect=DirectoryError("rede"))
        response = self.act(self.logged_in(), "adicionar", 2)
        assert response.code == 503 and "Tente de novo em instantes." in response.body.decode()
        assert self.saved() == []

    def test_database_down_is_503_to_retry_and_writes_nothing(self):
        browser = self.logged_in()
        pairs = [("acao", "adicionar"), ("estacao", uuid(2)), ("_xsrf", self.xsrf(browser))]
        self.fav_db.fail = True
        response = self.post(browser, pairs)
        self.fav_db.fail = False
        assert response.code == 503 and self.saved() == []

    def test_malformed_station_is_422_and_never_reaches_the_directory(self):
        browser = self.logged_in()
        upper = "ABCDEF01-0000-0000-0000-00000000000A"  # o formato exige minúsculas
        for bad in ("", "nao-e-uuid", upper, uuid(2) + "x", "../../etc"):
            response = self.post(
                browser, [("acao", "adicionar"), ("estacao", bad), ("_xsrf", self.xsrf(browser))], gap=0
            )
            assert response.code == 422, bad
        assert self.radio.directory.by_uuid_calls == [] and self.saved() == []

    def test_unknown_action_is_422(self):
        response = self.act(self.logged_in(), "limpar")
        assert response.code == 422 and self.saved() == []

    def test_the_store_gate_refuses_when_it_does_not_find_the_member_as_a_manager(self):
        browser = self.logged_in()
        self.radio.favorites.pool.get_all_bots = lambda: []  # a tela passa (GuildAccess), o store não acha o membro
        response = self.act(browser, "adicionar", 2)
        assert response.code == 403 and "Gerenciar Servidor" in response.body.decode()
        assert self.saved() == []

    def test_the_screen_gate_refuses_before_the_directory_and_the_store(self):
        page = ServerFavoritesPage()
        ctx = SimpleNamespace(
            pool=SimpleNamespace(radio=self.radio, get_all_bots=lambda: []), guild_id=ALFA, user_id="1001"
        )
        tr = lambda key, **kw: key  # noqa: E731 - só a chave importa aqui
        args = {"estacao": [uuid(2)]}
        for operation in (page.add, page.remove):
            outcome = self.io_loop.run_sync(lambda op=operation: op(ctx, args, tr))
            assert outcome.status == 403
        assert self.radio.directory.by_uuid_calls == [] and self.saved() == []

    def test_a_store_refusal_is_403(self):
        browser = self.logged_in()
        self.radio.favorites.add = AsyncMock(side_effect=FavoriteForbidden)
        response = self.act(browser, "adicionar", 2)
        assert response.code == 403 and self.saved() == []

    def test_the_audit_has_ids_and_the_action_only(self):
        self.act(self.logged_in(), "adicionar", 1)
        audit = self.audit_text()
        assert "categoria=radios campos=favoritas.adicionar" in audit and "usuario=1001" in audit
        assert uuid(1) not in self.all_log_text() and "Alfa" not in self.all_log_text()


class FavoritesRemoveTest(FavoritesBase):
    def test_remove_drops_only_that_station_of_this_server(self):
        self.add_direct(2, 3)
        self.radio.premium.grant(800)
        self.add_direct(2, guild=800)
        response = self.act(self.logged_in(), "remover", 2)
        assert response.code == 200 and "saiu das favoritas do servidor" in response.body.decode()
        assert self.saved() == [uuid(3)] and self.saved(800) == [uuid(2)]

    def test_removing_twice_is_idempotent_and_the_directory_is_not_asked(self):
        self.add_direct(2)
        browser = self.logged_in()
        self.act(browser, "remover", 2)
        again = self.act(browser, "remover", 2)
        assert again.code == 200 and "já não estava" in again.body.decode()
        assert self.radio.directory.by_uuid_calls == []  # uma estação que sumiu do diretório precisa poder sair

    def test_the_gate_refuses_removal_and_keeps_the_list(self):
        self.add_direct(2)
        browser = self.logged_in()
        self.radio.favorites.pool.get_all_bots = lambda: []
        assert self.act(browser, "remover", 2).code == 403 and self.saved() == [uuid(2)]


class FavoritesAccessTest(FavoritesBase):
    def test_without_xsrf_it_is_refused_and_nothing_changes(self):
        self.add_direct(2)
        browser = self.logged_in()
        assert self.act(browser, "remover", 2, token=False).code == 403
        assert self.act(browser, "adicionar", 3, token=False).code == 403
        assert self.saved() == [uuid(2)]

    def test_anonymous_post_is_sent_to_login_and_changes_nothing(self):
        self.add_direct(2)
        response = self.post_raw(
            self.browser(), f"{self.url()}/favoritas", [("acao", "remover"), ("estacao", uuid(2))], token=False
        )
        assert response.code in (302, 403) and self.saved() == [uuid(2)]

    def test_another_server_the_user_does_not_manage_is_404_and_changes_nothing(self):
        self.radio.premium.grant(600)
        browser = self.logged_in()
        pairs = [("acao", "adicionar"), ("estacao", uuid(2)), ("_xsrf", self.xsrf(browser))]
        assert self.post(browser, pairs, gid=600).code == 404
        assert self.post(browser, pairs, gid=999999).code == 404
        assert self.saved(600) == [] and self.radio.directory.by_uuid_calls == []

    def test_get_on_the_post_route_goes_back_to_the_radios_page(self):
        response = self.logged_in().get(f"{self.url()}/favoritas")
        assert response.code == 302 and response.headers["Location"].endswith("/radios")

    def test_oversized_body_is_413(self):
        browser = self.logged_in()
        pairs = [("acao", "adicionar"), ("estacao", uuid(2)), ("lixo", "x" * (MAX_BODY_BYTES + 1))]
        assert self.post(browser, pairs).code == 413 and self.saved() == []

    def test_writes_past_the_per_minute_limit_are_429_and_the_directory_is_not_called(self):
        browser = self.logged_in()
        pairs = [("acao", "adicionar"), ("estacao", "zzz"), ("_xsrf", self.xsrf(browser))]
        for _ in range(WRITE_LIMIT_PER_MINUTE):
            assert self.post(browser, pairs, gap=0).code == 422
        blocked = self.post(browser, pairs, gap=0)
        assert blocked.code == 429 and blocked.headers.get("Retry-After") == "60"
        assert self.radio.directory.by_uuid_calls == []


class FavoritesLocalTest(FavoritesBase):
    def test_a_final_node_lists_stations_with_add_buttons_and_marks_the_saved_ones(self):
        self.add_direct(3)
        self.radio.directory.region_hits = [station(2, HOSTILE), station(3)]
        response = self.local(self.logged_in(), SP)
        body = response.body.decode()
        assert response.code == 200 and self.radio.directory.region_calls == [(SP, ALFA)]  # balde do servidor
        assert "<i>" not in body and "@everyone" not in body and STREAM not in body
        forms = acao_forms(body, "adicionar")
        assert [f.value("estacao") for f in forms] == [uuid(2)] and "Já é favorita" in body
        assert forms[0].value("node") == SP and forms[0].has("_xsrf")

    def test_navigating_the_levels_never_calls_the_directory(self):
        response = self.local(self.logged_in(), "c:BR")
        assert response.code == 200 and self.radio.directory.region_calls == []

    def test_a_forged_node_is_422_back_to_the_top_and_never_reaches_the_directory(self):
        response = self.local(self.logged_in(), "x:forjado")
        assert response.code == 422 and self.radio.directory.region_calls == []

    def test_listing_past_the_write_limit_is_429(self):
        browser = self.logged_in()
        self.radio.directory.region_hits = [station(2)]
        for _ in range(WRITE_LIMIT_PER_MINUTE):
            assert self.local(browser, SP).code == 200
        blocked = self.local(browser, CAMPINAS)
        assert blocked.code == 429 and len(self.radio.directory.region_calls) == WRITE_LIMIT_PER_MINUTE

    def test_a_server_the_user_does_not_manage_is_404(self):
        response = self.logged_in().get("/painel/servidores/600/radios/favoritas/local?node=" + SP)
        assert response.code == 404 and self.radio.directory.region_calls == []

    def test_without_the_location_tree_there_is_nothing_to_navigate(self):
        self.radio.locations = None
        assert self.local(self.logged_in(), SP).code == 404

    def test_the_full_page_without_htmx_also_shows_the_block(self):
        self.radio.directory.region_hits = [station(2)]
        response = self.local(self.logged_in(), SP, htmx=False)
        body = response.body.decode()
        assert response.code == 200 and "<html" in body.lower() and 'id="radio-favoritas"' in body
