"""Item 8 (R5 §5 "Web", R-6) · Rádios: estações personalizadas do servidor no painel.

Caminhos de risco: a seção só com premium (sem ele, 403 em qualquer POST e nada na página); a validação única fica na
store e cada recusa vira o campo marcado sem gravar (nome vazio/repetido, fonte ausente, gênero fora do catálogo, 11
artistas, playlist inexistente, 11ª estação); compare-and-set em ``rev`` (409) na edição e na exclusão; estação de
outro servidor ou id fora da forma é 404; apagar estação em uso é recusado com o canal que a usa; só ids e a ação vão
ao log; nomes e artistas hostis escapados; o seletor da rádio personalizada grava só ``source.station`` com CAS e
avisa a rádio ligada; 413/429 como nos outros POSTs.
"""

import html
import re
from dataclasses import replace

from empiremuse.panel.handlers.settings import MAX_BODY_BYTES, WRITE_LIMIT_PER_MINUTE
from empiremuse.panel.services.base import CONFLICT_TEXT, SAVED_TEXT
from empiremuse.panel.services.radio_stations import STATIONS_MAX
from tests.panel.helpers_radio import make_channel
from tests.panel.helpers_settings import parse_forms
from tests.panel.test_radio_page import ALFA, OMEGA, CHANNEL, RadioBase
from tests.panel.test_settings_http import Each
from tests.panel.test_settings_links import doc as playlist_doc

HOSTILE = "<script>alert(1)</script>"


class StationsBase(RadioBase):
    def seed_premium(self, gid=ALFA):
        self.radio.premium.grant(gid)
        self.world.set_playlists(gid, [playlist_doc("Rock", 1), playlist_doc("Café", 2)])
        self.radio.stations.playlists[gid] = ("Rock", "Café")

    def stations_url(self, gid=ALFA, station_id=None):
        return self.url(gid) + "/estacoes" + (f"/{station_id}" if station_id else "")

    def create_form(self, body):
        return next((f for f in parse_forms(body) if f.action and f.action.endswith("/estacoes")), None)

    def edit_forms(self, body):
        return [f for f in parse_forms(body) if f.action and "/estacoes/" in f.action and f.has("name")]

    def create(self, browser, *, htmx=True, **fields):
        form = self.create_form(self.page(browser))
        assert form is not None, "a página premium deve ter o formulário de criar"
        return self.submit(browser, self.url(), form, htmx=htmx, **fields)

    def post_item(self, browser, station, acao, *, versao=None, **fields):
        pairs = [("versao", str(station.rev) if versao is None else versao), ("acao", acao)]
        for name, value in fields.items():
            for item in value if isinstance(value, list) else [value]:
                pairs.append((name, item))
        return self.post_raw(browser, self.stations_url(ALFA, station.id), pairs, htmx=True)


# ============================================================================ página


class StationsPageTest(StationsBase):
    def test_premium_page_lists_stations_with_source_warnings_usage_and_the_lastfm_credit(self):
        self.seed_premium()
        genre = self.radio.stations.seed(ALFA, "Madrugada", "genre", {"genre": "lofi"})
        artists = self.radio.stations.seed(
            ALFA, "Duplas", "artists", {"artists": ["Chitãozinho", "Zezé"]}, unknown=("Zezé",)
        )
        self.radio.stations.seed(ALFA, "Café da casa", "playlists", {"playlists": ["Café"]})
        self.radio.stations.seed(OMEGA, "De outro servidor")
        radio = self.radio.store.seed(1, ALFA, CHANNEL)
        self.radio.store.docs[radio.id] = replace(radio, source={"station": genre.id})
        body = self.page(self.logged_in())
        assert "Estações personalizadas" in body and "3 de 10" in body
        assert 'href="https://www.last.fm"' in body and "Powered by Last.fm" in body
        assert body.index("Café da casa") < body.index("Duplas") < body.index("Madrugada")  # ordem por nome
        assert "Gênero" in body and "Lo-fi" in body and "Chitãozinho · Zezé" in body and "Playlists do servidor" in body
        assert "⚠️ Não encontrados no Last.fm: Zezé" in body
        assert "Em uso em: Sala &lt;i&gt;DJ&lt;/i&gt;" in body and "De outro servidor" not in body
        assert self.create_form(body) is not None and len(self.edit_forms(body)) == 3
        edit = next(f for f in self.edit_forms(body) if f.value("name") == "Duplas")
        assert edit.values("kind") == ["artists"] and edit.value("artists") == "Chitãozinho\nZezé"
        assert edit.value("versao") == str(artists.rev) and len(edit.choices("genre")) == 1 + 33
        assert edit.choices("genre")[1:4] == ["rock", "metal", "pop"] and edit.choices("genre")[-1] == "phonk_br"
        assert '<optgroup label="Internacionais">' in body and '<optgroup label="Brasileiros">' in body

    def test_without_premium_the_section_and_the_posts_are_refused(self):
        self.radio.stations.seed(ALFA, "Madrugada")
        browser = self.logged_in()
        body = self.page(browser)
        assert "Estações personalizadas" not in body and "Madrugada" not in body
        pairs = [("name", "Nova"), ("kind", "genre"), ("genre", "rock")]
        assert self.post_raw(browser, self.stations_url(), pairs, htmx=True).code == 403
        assert self.radio.stations.updates == []

    def test_hostile_names_and_artists_are_escaped(self):
        self.seed_premium()
        self.radio.stations.seed(ALFA, HOSTILE, "artists", {"artists": ['" onmouseover="alert(1)']}, unknown=(HOSTILE,))
        body = self.page(self.logged_in())
        assert "<script>" not in body and 'onmouseover="alert' not in body
        assert body.count("&lt;script&gt;alert(1)&lt;/script&gt;") >= 3  # nome, aviso e campo do formulário
        assert "&quot; onmouseover=&quot;alert(1)" in body


# ============================================================================ criar


class StationsCreateTest(StationsBase):
    def test_creating_a_station_stores_it_audits_only_ids_and_returns_the_section(self):
        self.seed_premium()
        browser = self.logged_in()
        response = self.create(
            browser,
            name="  Sertanejo raiz ",
            kind="artists",
            artists="Chitãozinho\n\n  Leandro  \n",
            guild_id="800",
            rev="9",
        )
        body = response.body.decode()
        assert response.code == 200 and SAVED_TEXT in body and "<html" not in body.lower()
        (station,) = self.radio.stations.docs.values()
        assert (station.guild_id, station.name, station.kind) == (ALFA, "Sertanejo raiz", "artists")
        assert station.source == {"artists": ["Chitãozinho", "Leandro"]} and station.updated_by == "1001"
        assert "Sertanejo raiz" in body and "1 de 10" in body
        audit = self.audit_text()
        assert f"escopo=estacao:{station.id} categoria=radios campos=criada" in audit
        assert "Sertanejo" not in audit and "Chitãozinho" not in audit

    def test_each_rule_refusal_marks_the_field_without_writing(self):
        self.seed_premium()
        self.radio.stations.seed(ALFA, "Madrugada")
        browser = self.logged_in()

        def check(case):
            name, fields, field, text = case
            response = self.create(browser, **fields)
            body = html.unescape(response.body.decode())
            assert response.code == 422, f"{name}: status {response.code}"
            assert text in body, name
            assert f'name="{field}"' in body and 'aria-invalid="true"' in body, name
            assert len(self.radio.stations.docs) == 1, name

        Each.run(
            [
                ("nome vazio", {"name": "   ", "kind": "genre", "genre": "rock"}, "name", "de 1 a 32"),
                ("nome repetido", {"name": "madrugada", "kind": "genre", "genre": "rock"}, "name", "sem repetir"),
                ("sem fonte", {"name": "Nova", "kind": None, "genre": "rock"}, "kind", "Escolha uma fonte"),
                ("gênero fora do catálogo", {"name": "Nova", "kind": "genre", "genre": "funk"}, "genre", "da lista"),
                (
                    "11 artistas",
                    {"name": "Nova", "kind": "artists", "artists": "\n".join(f"A{i}" for i in range(11))},
                    "artists",
                    "de 1 a 10",
                ),
                (
                    "playlist inexistente",
                    {"name": "Nova", "kind": "playlists", "playlists": ["Rock", "Sumida"]},
                    "playlists",
                    "existentes",
                ),
            ],
            check,
        )

    def test_the_eleventh_station_is_refused_with_the_limit_message(self):
        self.seed_premium()
        for i in range(STATIONS_MAX):
            self.radio.stations.seed(ALFA, f"Estação {i}")
        browser = self.logged_in()
        body = self.page(browser)
        assert "10 de 10" in body and "já tem 10 estações" in body and self.create_form(body) is None
        self.radio.stations.docs.pop(next(iter(self.radio.stations.docs)))  # abre uma vaga só para o formulário existir
        form = self.create_form(self.page(browser))
        self.radio.stations.seed(ALFA, "Correu na frente")  # outro escritor enche a vaga antes do POST
        response = self.submit(browser, self.url(), form, htmx=True, name="Nova", kind="genre", genre="rock")
        assert response.code == 422 and "já tem 10 estações" in html.unescape(response.body.decode())
        assert len(self.radio.stations.docs) == STATIONS_MAX


# ============================================================================ editar e apagar (R-6)


class StationsEditDeleteTest(StationsBase):
    def test_editing_writes_with_rev_and_the_stale_version_is_409(self):
        self.seed_premium()
        station = self.radio.stations.seed(ALFA, "Madrugada")
        browser = self.logged_in()
        response = self.post_item(
            browser, station, "salvar", name="Manhã", kind="playlists", playlists=["Rock", "Café"], guild_id="800"
        )
        body = response.body.decode()
        assert response.code == 200 and SAVED_TEXT in body and "Manhã" in body
        saved = self.radio.stations.docs[station.id]
        assert (saved.name, saved.kind, saved.source, saved.rev, saved.guild_id) == (
            "Manhã",
            "playlists",
            {"playlists": ["Rock", "Café"]},
            2,
            ALFA,
        )
        assert f"escopo=estacao:{station.id} categoria=radios campos=editada" in self.audit_text()
        stale = self.post_item(browser, station, "salvar", versao="1", name="Tarde", kind="genre", genre="rock")
        assert stale.code == 409 and CONFLICT_TEXT in stale.body.decode()
        assert self.radio.stations.docs[station.id].name == "Manhã"

    def test_deleting_removes_the_station_and_a_station_in_use_is_refused_with_the_channel(self):
        self.seed_premium()
        free = self.radio.stations.seed(ALFA, "Livre")
        used = self.radio.stations.seed(ALFA, "Usada")
        radio = self.radio.store.seed(1, ALFA, CHANNEL)
        self.radio.store.docs[radio.id] = replace(radio, source={"station": used.id})
        browser = self.logged_in()
        gone = self.post_item(browser, free, "apagar")
        assert gone.code == 200 and "Estação apagada" in gone.body.decode() and free.id not in self.radio.stations.docs
        assert f"escopo=estacao:{free.id} categoria=radios campos=apagada" in self.audit_text()
        refused = self.post_item(browser, used, "apagar")
        body = html.unescape(refused.body.decode())
        assert refused.code == 422 and "em uso em: Sala <i>DJ</i>" in body and used.id in self.radio.stations.docs
        assert self.post_item(browser, used, "apagar", versao="7").code == 409

    def test_other_server_malformed_id_and_unknown_action_are_refused(self):
        self.seed_premium()
        other = self.radio.stations.seed(OMEGA, "Alheia")
        mine = self.radio.stations.seed(ALFA, "Minha")
        browser = self.logged_in()
        for station_id in (other.id, "0" * 24, "zzz", "../" + mine.id):
            pairs = [("versao", "1"), ("acao", "apagar")]
            response = self.post_raw(browser, self.stations_url(ALFA, station_id), pairs, htmx=True)
            assert response.code == 404, station_id
        unknown = self.post_item(browser, mine, "explodir")
        assert unknown.code == 422 and "Ação desconhecida" in html.unescape(unknown.body.decode())
        assert self.radio.stations.updates == [] and len(self.radio.stations.docs) == 2

    def test_body_and_rate_limits_apply(self):
        self.seed_premium()
        station = self.radio.stations.seed(ALFA, "Madrugada")
        browser = self.logged_in()
        pairs = [("name", "x"), ("kind", "genre"), ("genre", "rock"), ("enchimento", "x" * (MAX_BODY_BYTES + 1))]
        assert self.post_raw(browser, self.stations_url(), pairs, htmx=True).code == 413
        for _ in range(WRITE_LIMIT_PER_MINUTE):
            assert self.post_raw(browser, self.stations_url(), [("name", "")], htmx=True, gap=0).code == 422
        blocked = self.post_raw(
            browser, self.stations_url(ALFA, station.id), [("versao", "1"), ("acao", "apagar")], htmx=True, gap=0
        )
        assert blocked.code == 429 and blocked.headers.get("Retry-After") == "60"
        assert station.id in self.radio.stations.docs


# ============================================================================ a estação de uma rádio personalizada


class CustomRadioStationTest(StationsBase):
    def test_custom_radio_offers_the_server_stations_and_the_choice_switches_the_live_radio(self):
        self.seed_premium()
        self.radio.stations.seed(OMEGA, "Alheia")
        a = self.radio.stations.seed(ALFA, "Madrugada")
        b = self.radio.stations.seed(ALFA, "Manhã")
        radio = self.radio.store.seed(1, ALFA, CHANNEL)
        browser = self.logged_in()
        form = next(
            f for f in parse_forms(self.page(browser)) if f.action and f.action.endswith(f"/{radio.id}/estacao")
        )
        assert form.choices("estacao") == ["", a.id, b.id] and form.value("versao") == str(radio.rev)
        response = self.submit(browser, self.url(), form, htmx=True, estacao=b.id, channel_id="9")
        body = response.body.decode()
        assert response.code == 200 and SAVED_TEXT in body and "<html" not in body.lower()
        saved = self.radio.store.docs[radio.id]
        assert saved.source == {"station": b.id} and saved.rev == radio.rev + 1 and saved.channel_id == CHANNEL
        assert self.radio.manager.station_changes == [saved]
        audit = self.audit_text()
        assert "escopo=radio:1 categoria=radios campos=source.station" in audit and b.id not in audit
        assert re.search(rf'value="{b.id}" selected', body)

    def test_invalid_station_choices_are_refused_without_writing(self):
        self.seed_premium()
        other = self.radio.stations.seed(OMEGA, "Alheia")
        radio = self.radio.store.seed(1, ALFA, CHANNEL)
        browser = self.logged_in()

        def check(case):
            name, estacao, versao, status = case
            pairs = [("versao", versao or str(radio.rev)), ("estacao", estacao)]
            response = self.post_raw(browser, f"{self.url(ALFA, radio.id)}/estacao", pairs, htmx=True)
            assert response.code == status, f"{name}: status {response.code}"
            assert self.radio.store.updates == [] and self.radio.manager.station_changes == [], name

        Each.run(
            [
                ("id fora da forma", "zzz", None, 422),
                ("estação de outro servidor", other.id, None, 422),
                ("estação inexistente", "0" * 24, None, 422),
                ("versão velha", other.id, "1", 409),
            ],
            check,
        )

    def test_without_permission_or_premium_the_choice_is_refused(self):
        self.seed_premium()
        station = self.radio.stations.seed(ALFA, "Madrugada")
        radio = self.radio.store.seed(1, ALFA, CHANNEL)
        browser = self.logged_in()
        self.world.channels[CHANNEL] = make_channel(CHANNEL, "Sala", ALFA, manage_messages=False)
        pairs = [("versao", str(radio.rev)), ("estacao", station.id)]
        assert self.post_raw(browser, f"{self.url(ALFA, radio.id)}/estacao", pairs, htmx=True).code == 403
        assert self.radio.store.updates == []
