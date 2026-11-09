"""J3 · Minha conta › Rádios favoritas: estações e locais PESSOAIS, só Remover.

Caminhos de risco (dado pessoal, contrato Jukebox m7): a lista é sempre a do usuário da SESSÃO (``user_id`` do corpo é
ignorado), exige sessão e XSRF, nunca mostra a lista de outro usuário, remover é idempotente, banco fora do ar é 503 sem
derrubar a página, e nada de estação nem de usuário vai para o log. Backend real (``UserRadioFavorites``) sobre banco
em memória; só o diretório, a árvore de local e o Discord são dublês.
"""

from types import SimpleNamespace

import pytest

from empiremuse.rules.radio_stations import Station
from empiremuse.utils.radio.user_favorites import USER_FAVORITES_DB, UserRadioFavorites
from tests.panel.helpers_account import OTHER_UID
from tests.panel.helpers_radio import SP, build_tree
from tests.panel.helpers_settings import UID, acao_forms
from tests.panel.test_account_page import URL, AccountCase
from tests.persistence.test_radio_store import FakeDb

A, B = (f"{d * 8}-{d * 4}-{d * 4}-{d * 4}-{d * 12}" for d in "ab")
HOSTILE = "Rádio <b>Alfa</b> \"aspas\" 'simples' @everyone https://evil.example"
pytestmark = pytest.mark.critical

STREAM = "https://secret.example/stream"


class Directory:
    def __init__(self):
        self.stations = {
            A: Station(A, HOSTILE, STREAM, "MP3", 128, "Brasil"),
            B: Station(B, "Beta FM", STREAM, "MP3", 128, "Brasil"),
        }

    async def by_uuid(self, uuid):
        return self.stations.get(uuid)

    by_uuid_strict = by_uuid


class RadioAccountCase(AccountCase):
    def get_app(self):
        app = super().get_app()
        self.fav_db = FakeDb()
        tree = build_tree()
        hub = SimpleNamespace(directory=Directory(), locations=tree)
        self.favorites = UserRadioFavorites(SimpleNamespace(database=self.fav_db), hub)
        self.pool.radio = SimpleNamespace(user_favorites=self.favorites, locations=tree)
        return app

    def seed(self, user=UID, stations=(A,), filters=(SP,)):
        async def run():
            for uuid in stations:
                await self.favorites.toggle_station(user, uuid)
            for node in filters:
                await self.favorites.toggle_filter(user, node)

        self.io_loop.run_sync(run)

    def doc(self, user=UID):
        return self.fav_db.tables.get(USER_FAVORITES_DB, {}).get(str(user))

    def radio_forms(self, browser, acao):
        return acao_forms(self.body(browser), acao)

    def remove(self, browser, acao, field, value, *, htmx=True, extra=(), token=True):
        form = self.radio_forms(browser, acao)[0]
        pairs = [(k, v) for k, v in form.pairs() if k != field and (token or k != "_xsrf")] + [(field, value), *extra]
        return self.post_raw(browser, URL, pairs, htmx=htmx, token=token)  # sem token: nem cabeçalho nem campo _xsrf


class AccountRadioPageTest(RadioAccountCase):
    def test_lists_only_the_session_users_stations_and_locations_escaped_and_without_stream_urls(self):
        self.seed()
        self.seed(user=OTHER_UID, stations=(B,), filters=())
        body = self.body(self.logged_in())
        assert "Rádios favoritas" in body and 'id="conta-radios"' in body
        assert "Rádio &lt;b&gt;Alfa" in body and "<b>" not in body  # autoescape do template
        assert "@everyone" not in body and "https://evil.example" not in body  # nome externo pelo sanitizador único
        assert '"aspas"' not in body and "&quot;aspas&quot;" in body  # aspas não saem do atributo ``hx-confirm``
        assert "América Latina › Brasil › São Paulo › São Paulo" in body  # o rótulo vem da árvore, não do documento
        assert "Beta FM" not in body and STREAM not in body and "secret.example" not in body

    def test_the_other_user_sees_only_theirs(self):
        self.seed()
        self.seed(user=OTHER_UID, stations=(B,), filters=())
        body = self.body(self.other_user())
        assert "Beta FM" in body and "Alfa" not in body

    def test_empty_lists_say_so_and_offer_no_remove_form(self):
        body = self.body(self.logged_in())
        assert "Você ainda não tem estações favoritas." in body and "Você ainda não salvou filtros de local." in body
        assert not acao_forms(body, "radio_estacao_remover") and not acao_forms(body, "radio_filtro_remover")

    def test_remove_forms_ask_for_confirmation_and_carry_xsrf(self):
        self.seed()
        body = self.body(self.logged_in())
        station_form = acao_forms(body, "radio_estacao_remover")[0]
        assert station_form.has("_xsrf") and station_form.value("estacao") == A
        assert body.count("hx-confirm=") >= 2 and "Remover" in body

    def test_without_the_radio_hub_the_section_is_not_offered(self):
        del self.pool.radio
        body = self.body(self.logged_in())
        assert "conta-radios" not in body and "Rádios favoritas" not in body

    def test_database_outage_keeps_the_page_and_says_to_try_again(self):
        self.seed()
        self.fav_db.fail = True
        response = self.logged_in().get(URL)
        body = response.body.decode()
        assert response.code == 200 and 'id="conta-radios"' in body and "Tente de novo em instantes." in body
        assert "Alfa" not in body and "Favoritos" in body  # as outras seções seguem


class AccountRadioRemoveTest(RadioAccountCase):
    def test_remove_station_changes_only_the_session_users_document(self):
        self.seed()
        self.seed(user=OTHER_UID, stations=(A, B), filters=(SP,))
        other_before = self.doc(OTHER_UID)
        response = self.remove(self.logged_in(), "radio_estacao_remover", "estacao", A)
        body = response.body.decode()
        assert response.code == 200 and "Removido das suas rádios favoritas." in body and "<html" not in body.lower()
        assert self.doc()["stations"] == [] and self.doc()["filters"] == [{"node": SP}]  # o filtro fica
        assert self.doc(OTHER_UID) == other_before
        assert "Alfa" not in body

    def test_remove_filter(self):
        self.seed()
        response = self.remove(self.logged_in(), "radio_filtro_remover", "filtro", SP)
        assert response.code == 200 and self.doc()["filters"] == [] and len(self.doc()["stations"]) == 1

    def test_user_id_and_guild_fields_in_the_body_are_ignored(self):
        self.seed()
        self.seed(user=OTHER_UID, stations=(A,), filters=())
        other_before = self.doc(OTHER_UID)
        response = self.remove(
            self.logged_in(),
            "radio_estacao_remover",
            "estacao",
            A,
            extra=[("user_id", str(OTHER_UID)), ("usuario", str(OTHER_UID)), ("guild_id", "100")],
        )
        assert response.code == 200
        assert self.doc()["stations"] == [] and self.doc(OTHER_UID) == other_before

    def test_without_xsrf_it_is_refused_and_nothing_changes(self):
        self.seed()
        browser = self.logged_in()
        before = self.doc()
        assert self.remove(browser, "radio_estacao_remover", "estacao", A, token=False).code == 403
        assert self.doc() == before

    def test_anonymous_post_is_sent_to_login_and_nothing_changes(self):
        self.seed()
        before = self.doc()
        response = self.post_raw(self.browser(), URL, [("acao", "radio_estacao_remover"), ("estacao", A)], token=False)
        assert response.code in (302, 403) and self.doc() == before

    def test_removing_twice_is_idempotent(self):
        self.seed()
        browser = self.logged_in()
        form = self.radio_forms(browser, "radio_estacao_remover")[0]
        pairs = form.pairs()
        assert self.post_raw(browser, URL, pairs, htmx=True).code == 200
        again = self.post_raw(browser, URL, pairs, htmx=True)
        assert again.code == 200 and "já não estava na sua lista" in again.body.decode()

    def test_a_missing_target_is_422_and_an_unknown_one_changes_nothing(self):
        self.seed()
        browser = self.logged_in()
        before = self.doc()
        form = self.radio_forms(browser, "radio_estacao_remover")[0]
        missing = [(k, v) for k, v in form.pairs() if k != "estacao"]
        assert self.post_raw(browser, URL, missing, htmx=True).code == 422
        unknown = self.remove(browser, "radio_estacao_remover", "estacao", B)  # existe no diretório, não na lista
        assert unknown.code == 200 and self.doc() == before

    def test_database_outage_on_remove_is_503_and_keeps_the_list(self):
        self.seed()
        browser = self.logged_in()
        form = self.radio_forms(browser, "radio_estacao_remover")[0]
        before = self.doc()
        self.fav_db.fail = True
        response = self.post_raw(browser, URL, form.pairs(), htmx=True)
        self.fav_db.fail = False
        assert response.code == 503 and "Tente de novo em instantes." in response.body.decode()
        assert self.doc() == before

    def test_logs_carry_no_user_station_or_location(self):
        self.seed()
        self.remove(self.logged_in(), "radio_estacao_remover", "estacao", A)
        text = self.all_log_text()
        assert A not in text and "Alfa" not in text and SP not in text
