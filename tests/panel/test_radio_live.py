"""Item 8 (R3 §4, R-6/R-7) · Rádios: estação principal, reserva e o seletor por busca no painel.

Caminhos de risco: a página nunca mostra a URL do fluxo; nomes escapados; ⛔ para estação indisponível; a busca só
para quem pode editar (premium + ``manage_messages``) e só com termo válido (senão nenhuma chamada ao diretório, R-7);
a principal fica fora dos resultados; gravar a reserva só por ``source.reserve`` com compare-and-set em ``rev`` (409),
uuid inválido, igual à principal, indisponível ou rádio personalizada → 422 sem gravar; auditoria só com o nome do campo.
"""

import html
import re

from empiremuse.panel.services.base import CONFLICT_TEXT, SAVED_TEXT
from empiremuse.rules.radio_stations import Station
from tests.panel.helpers_radio import make_channel, radio_state
from tests.panel.helpers_settings import parse_forms
from tests.panel.test_radio_page import ALFA, CHANNEL, RadioBase
from tests.panel.test_settings_http import Each

A, B, C = (f"{d * 8}-{d * 4}-{d * 4}-{d * 4}-{d * 12}" for d in "abc")
SECRET = "https://secret.example/stream"


def station(uuid, name, country="Brasil"):
    return Station(uuid, name, SECRET, "MP3", 128, country)


class LiveBase(RadioBase):
    def seed_live(self, *, reserve=None, gid=ALFA, channel=CHANNEL, enabled=True):
        self.radio.premium.grant(gid)
        radio = self.radio.store.seed_live(1, gid, channel, station=A, reserve=reserve, enabled=enabled)
        self.radio.service.states[radio.id] = radio_state(on_reserve=reserve is not None)
        self.radio.directory.stations.update(
            {A: station(A, "Rádio <b>Alfa</b>"), B: station(B, "Beta FM", "Portugal"), C: station(C, "Gama Rock")}
        )
        self.radio.directory.results = [station(B, "Beta FM", "Portugal"), station(C, "Gama Rock"), station(A, "Alfa")]
        return radio

    def reserve_form(self, body):
        return next((f for f in parse_forms(body) if f.action and f.action.endswith("/reserva")), None)

    def choose(self, browser, radio, value, *, htmx=True, **overrides):
        form = self.reserve_form(self.page(browser))
        assert form is not None, "a rádio ao vivo editável deve ter o formulário da reserva"
        return self.submit(browser, self.url(), form, htmx=htmx, reserva=value, **overrides)

    def search(self, browser, radio, term, *, htmx=True):
        headers = {"HX-Request": "true"} if htmx else {}
        return browser.get(f"{self.url(ALFA, radio.id)}/estacoes?q={term}", headers=headers)


# ============================================================================ página


class LivePageTest(LiveBase):
    def test_live_radio_shows_main_and_backup_escaped_with_unavailable_mark_and_no_stream_url(self):
        self.seed_live(reserve=B)
        self.radio.health.down.add(B)
        body = self.page(self.logged_in())
        assert "Rádio &lt;b&gt;Alfa&lt;/b&gt; (Brasil · MP3 128 kbps)" in body and "<b>Alfa</b>" not in body
        assert "Beta FM (Portugal · MP3 128 kbps)" in body and "⛔" in body and "Reserva no ar" in body
        assert SECRET not in body and "secret.example" not in body
        assert self.reserve_form(body) is not None

    def test_custom_radio_and_read_only_live_radio_have_no_selector(self):
        self.radio.premium.grant(ALFA)
        self.radio.store.seed(1, ALFA, CHANNEL)
        body = self.page(self.logged_in())
        assert "Estação ao vivo" not in body and self.reserve_form(body) is None
        self.world.channels[CHANNEL] = make_channel(CHANNEL, "Sala", ALFA, manage_messages=False)
        self.radio.store.docs.clear()
        self.seed_live()
        body = self.page(self.logged_in())
        assert "Estação ao vivo" in body and "Principal" in body and self.reserve_form(body) is None


# ============================================================================ busca (R-7)


class LiveSearchTest(LiveBase):
    def test_search_lists_buttons_without_the_main_station_and_disables_unavailable_ones(self):
        radio = self.seed_live()
        self.radio.health.down.add(C)
        response = self.search(self.logged_in(), radio, "fm")
        body = response.body.decode()
        assert response.code == 200 and "<html" not in body.lower()
        assert re.findall(r'name="reserva" value="([^"]*)"', body) == [B, C]
        assert f'value="{C}" disabled' in body and f'value="{A}"' not in body
        assert "Beta FM" in body and SECRET not in body
        assert self.radio.directory.calls == [("fm", ALFA)]  # a busca do painel é limitada por servidor

    def test_short_term_or_missing_permission_or_premium_never_reaches_the_directory(self):
        radio = self.seed_live()
        browser = self.logged_in()

        def check(case):
            name, prepare, term, status = case
            prepare()
            response = self.search(browser, radio, term)
            assert response.code == status, f"{name}: status {response.code}"
            assert self.radio.directory.calls == [], f"{name}: o diretório foi chamado"

        def no_permission():
            self.world.channels[CHANNEL] = make_channel(CHANNEL, "Sala", ALFA, manage_messages=False)

        def no_premium():
            self.world.channels[CHANNEL] = make_channel(CHANNEL, "Sala", ALFA)
            self.radio.premium.docs.clear()

        Each.run(
            [
                ("termo curto", lambda: None, "a", 200),
                ("termo longo", lambda: None, "x" * 65, 200),
                ("sem manage_messages", no_permission, "fm", 403),
                ("sem premium", no_premium, "fm", 403),
            ],
            check,
        )

    def test_search_of_a_custom_radio_or_another_server_is_404(self):
        self.radio.premium.grant(ALFA)
        custom = self.radio.store.seed(1, ALFA, CHANNEL)
        browser = self.logged_in()
        for radio_id in (custom.id, "2:800", "abc"):
            response = browser.get(f"{self.url(ALFA, radio_id)}/estacoes?q=fm", headers={"HX-Request": "true"})
            assert response.code == 404, radio_id
        assert self.radio.directory.calls == []


# ============================================================================ reserva (R-6)


class LiveReserveTest(LiveBase):
    def test_choosing_and_removing_the_backup_writes_only_source_reserve_with_rev_and_audits_the_field(self):
        radio = self.seed_live()
        browser = self.logged_in()
        response = self.choose(browser, radio, B, channel_id="999", enabled=None, station=C)
        body = response.body.decode()
        assert response.code == 200 and SAVED_TEXT in body and "<html" not in body.lower()
        saved = self.radio.store.docs[radio.id]
        assert saved.source == {"station": A, "reserve": B} and saved.rev == radio.rev + 1
        assert (saved.channel_id, saved.enabled) == (CHANNEL, True)
        assert "Beta FM" in body and 'value=""' in body  # a reserva aparece e o botão de remover também
        audit = self.audit_text()
        assert "escopo=radio:1 categoria=radios campos=source.reserve" in audit and B not in audit
        assert self.choose(browser, saved, "").code == 200
        assert self.radio.store.docs[radio.id].source["reserve"] is None
        assert self.radio.store.updates == [(radio.id, "1001"), (radio.id, "1001")]

    def test_invalid_backups_are_422_without_writing(self):
        radio = self.seed_live()
        self.radio.health.down.add(C)
        browser = self.logged_in()

        def check(value):
            response = self.choose(browser, radio, value)
            assert response.code == 422, f"{value!r}: status {response.code}"
            assert "não pode ser a reserva" in html.unescape(response.body.decode())
            assert self.radio.store.updates == []

        Each.run(["zzz", "../" + B, A, C, B.upper()], check)

    def test_stale_rev_is_409_and_nothing_is_written(self):
        radio = self.seed_live()
        response = self.choose(self.logged_in(), radio, B, versao="1")
        assert response.code == 409 and CONFLICT_TEXT in response.body.decode()
        assert self.radio.store.updates == []
