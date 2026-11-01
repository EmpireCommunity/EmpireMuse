"""Item 8 (R4 §5, R-6/R-7) · Rádios: cascata de local e troca da estação principal no painel.

Caminhos de risco: o ``node`` do usuário só vale se a árvore o conhece (forjado, longo ou antigo → 422 no topo, nada ao
diretório); navegar pelos níveis é leitura em memória, e só o nó final chama ``directory.region`` (com a chave do
servidor, só com premium e ``manage_messages``, e sob o limitador de escrita); a principal fica fora da lista, 24 por
página; a principal nova grava só ``source.station`` com compare-and-set em ``rev``, é aplicada à rádio ligada por
``manager.change_station`` (falha ali não desfaz o save) e a auditoria leva só o nome do campo; rótulos da árvore
escapados; a página volta com a mesma lista depois do POST; sem árvore carregada o filtro some e a busca continua.
"""

import html
import re

from empiremuse.panel.handlers.settings import MAX_BODY_BYTES, WRITE_LIMIT_PER_MINUTE
from empiremuse.panel.services.base import CONFLICT_TEXT, SAVED_TEXT
from empiremuse.panel.services.radio_local import PAGE_SIZE
from empiremuse.utils.radio.store import RadioConflict
from tests.panel.helpers_radio import CAMPINAS, PORTO_VELHO, SP, make_channel
from tests.panel.helpers_settings import parse_forms
from tests.panel.test_radio_live import A, B, C, SECRET, LiveBase, station
from tests.panel.test_radio_page import ALFA, CHANNEL
from tests.panel.test_settings_http import Each

CREDIT = "Dados de localização: GeoNames, CC BY 4.0"
HOSTILE = "<script>alert(1)</script>"


def uuids(body, field):
    return re.findall(rf'name="{field}"[^>]*? value="([^"]*)"', body)


def selected(form):
    """O valor escolhido em cada select ``node`` da cascata, na ordem dos níveis ("" = só o "Escolha…", que volta ao pai)."""
    result = []
    for control in form.controls:
        if control.get("name") != "node":
            continue
        chosen = next((o for o in control["options"] if o["selected"]), None)
        result.append("" if chosen is None or chosen["label"].startswith("Escolha") else chosen["value"])
    return result


class LocalBase(LiveBase):
    def seed_local(self, **kwargs):
        radio = self.seed_live(**kwargs)
        self.world.g100.preferred_locale = "pt-BR"
        self.radio.directory.region_hits = [
            station(B, "Beta FM", "Portugal"),
            station(C, "Gama Rock"),
            station(A, "Alfa"),
        ]
        return radio

    def local(self, browser, radio, node="", *, page=None, htmx=True, gap=4):
        if gap:
            self.clock.advance(gap)
        query = f"?node={node}" + (f"&pagina={page}" if page is not None else "")
        headers = {"HX-Request": "true"} if htmx else {}
        return browser.get(f"{self.url(ALFA, radio.id)}/local{query}", headers=headers)

    def local_form(self, body):
        return next((f for f in parse_forms(body) if f.action and f.action.endswith("/local")), None)

    def post_station(self, browser, radio, value, *, versao=None, extra=(), **kwargs):
        form = self.reserve_form(self.page(browser))
        assert form is not None, "a rádio ao vivo editável deve ter o formulário"
        pairs = [(k, v) for k, v in form.pairs() if k not in ("reserva", "estacao")]
        pairs = [(k, versao if k == "versao" and versao is not None else v) for k, v in pairs]
        pairs += [("estacao", value), *extra]
        return self.post_raw(browser, f"{self.url(ALFA, radio.id)}/estacao", pairs, htmx=True, **kwargs)


# ============================================================================ página e navegação


class LocalPageTest(LocalBase):
    def test_page_opens_the_top_with_the_star_country_first_and_credits_geonames(self):
        self.seed_local()
        body = self.page(self.logged_in())
        form = self.local_form(body)
        assert form is not None and CREDIT in body
        assert form.choices("node")[:3] == ["", "c:BR", "r:america_norte"] and len(form.choices("node")) == 8
        assert "⭐ Brasil" in body and "América Latina" in body and "países" not in body  # descrição só da escolhida
        assert self.radio.directory.region_calls == [] and self.radio.locations.calls[0] == (None, "pt-BR", "pt-BR")

    def test_without_a_server_country_or_a_tree_the_page_degrades(self):
        self.seed_local()
        del self.world.g100.preferred_locale
        browser = self.logged_in()
        form = self.local_form(self.page(browser))
        assert form is not None and "c:BR" not in form.choices("node")  # sem país: só as 6 regiões
        self.radio.locations = None
        body = self.page(browser)
        assert self.local_form(body) is None and CREDIT in body
        assert "Buscar estação" in body and self.reserve_form(body) is not None  # a busca por nome continua

    def test_navigating_levels_is_in_memory_and_only_the_final_node_lists_stations_out_of_band(self):
        radio = self.seed_local()
        self.radio.health.down.add(C)
        browser = self.logged_in()
        region = self.local(browser, radio, "r:america_latina").body.decode()
        assert "<html" not in region.lower() and "hx-swap-oob" in region
        assert selected(self.local_form(f"<form action='{self.url()}/x/local'>{region}</form>")) == [
            "r:america_latina",
            "",
        ]
        assert "Nicarágua" in region and "2 países" in region and uuids(region, "estacao") == []
        country = self.local(browser, radio, "c:BR").body.decode()
        assert "Demais estados: Norte" in country and "1603 estações" in country
        assert "Acre, Amapá" not in country and "317 estações" not in country  # descrição só da escolhida
        state = self.local(browser, radio, "s:BR:@norte").body.decode()
        assert "Acre, Amapá, Rondônia, Roraima" in state and "Porto Velho" in state
        assert self.radio.directory.region_calls == []
        city = self.local(browser, radio, PORTO_VELHO).body.decode()
        assert uuids(city, "estacao") == [B, C] and uuids(city, "reserva") == [B, C]  # a principal fica de fora
        assert f'value="{C}" disabled' in city and city.count(f'value="{C}" disabled') == 2
        assert f'name="node" value="{PORTO_VELHO}"' in city and 'name="pagina" value="1"' in city
        assert SECRET not in city and "Página" not in city
        assert self.radio.directory.region_calls == [(PORTO_VELHO, ALFA)]
        small = self.local(browser, radio, "c:NI").body.decode()
        assert small.count("<select") == 2 and uuids(small, "estacao") == [B, C]  # país final: sem estado nem cidade

    def test_hostile_tree_labels_are_escaped(self):
        radio = self.seed_local()
        self.radio.locations.add("s:BR:99", "state", HOSTILE, "c:BR", description='" onmouseover="alert(1)')
        body = self.local(self.logged_in(), radio, "s:BR:99").body.decode()
        assert "<script>" not in body and 'onmouseover="alert' not in body
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body and "&quot; onmouseover=&quot;alert(1)" in body

    def test_unknown_long_or_hostile_nodes_are_422_at_the_top_without_the_directory(self):
        radio = self.seed_local()
        browser = self.logged_in()

        def check(node):
            response = self.local(browser, radio, node)
            body = response.body.decode()
            assert response.code == 422, f"{node!r}: status {response.code}"
            assert "não está mais na lista" in html.unescape(body) and body.count("<select") == 1
            assert self.radio.directory.region_calls == []

        Each.run(["zzz", "x:999", "x" * 101, "..%2Fc:BR", "c:BR%00", "r:america_latina%26node=c:BR"], check)

    def test_full_page_without_htmx_carries_the_cascade_state_and_the_list(self):
        radio = self.seed_local()
        response = self.local(self.logged_in(), radio, SP, htmx=False)
        body = response.body.decode()
        assert response.code == 200 and "<html" in body.lower() and CREDIT in body
        assert selected(self.local_form(body)) == ["r:america_latina", "c:BR", "s:BR:27", SP]
        assert uuids(body, "estacao") == [B, C]


class LocalPagingTest(LocalBase):
    def test_stations_come_24_per_page_and_the_page_number_is_clamped(self):
        radio = self.seed_local()
        many = [station(f"{i:08x}-0000-0000-0000-000000000000", f"Estação {i}") for i in range(PAGE_SIZE + 6)]
        self.radio.directory.region_hits = many
        browser = self.logged_in()
        first = self.local(browser, radio, SP).body.decode()
        assert len(uuids(first, "estacao")) == PAGE_SIZE and "Página 1 de 2" in first
        assert "Anterior" not in first and "pagina=2" in first
        second = self.local(browser, radio, SP, page=2).body.decode()
        assert len(uuids(second, "estacao")) == 6 and "Página 2 de 2" in second and "Próxima" not in second
        assert 'name="pagina" value="2"' in second
        for raw in ("99", "abc", "0", "-1"):
            body = self.local(browser, radio, SP, page=raw).body.decode()
            assert ("Página 2 de 2" if raw == "99" else "Página 1 de 2") in body, raw
        assert all(call == (SP, ALFA) for call in self.radio.directory.region_calls)

    def test_partial_and_empty_results_are_hints_not_errors(self):
        radio = self.seed_local()
        browser = self.logged_in()
        self.radio.directory.region_partial = True
        body = self.local(browser, radio, SP).body.decode()
        assert "pode estar incompleta" in body and uuids(body, "estacao") == [B, C]
        self.radio.directory.region_hits = [station(A, "Alfa")]  # só a principal: lista vazia
        body = self.local(browser, radio, CAMPINAS).body.decode()
        assert "Nenhuma estação nesse local" in body and uuids(body, "estacao") == []


# ============================================================================ portas (R-7) e limites


class LocalGatesTest(LocalBase):
    def test_permission_premium_kind_and_tree_gate_the_cascade(self):
        radio = self.seed_local()
        self.radio.premium.grant(ALFA)
        custom = self.radio.store.seed(2, ALFA, 789)
        browser = self.logged_in()

        def check(case):
            name, prepare, radio_id, status = case
            prepare()
            response = browser.get(f"{self.url(ALFA, radio_id)}/local?node={SP}", headers={"HX-Request": "true"})
            assert response.code == status, f"{name}: status {response.code}"
            assert self.radio.directory.region_calls == [], f"{name}: o diretório foi chamado"

        def no_permission():
            self.world.channels[CHANNEL] = make_channel(CHANNEL, "Sala", ALFA, manage_messages=False)

        def no_premium():
            self.world.channels[CHANNEL] = make_channel(CHANNEL, "Sala", ALFA)
            self.radio.premium.docs.clear()

        def no_tree():
            self.radio.premium.grant(ALFA)
            self.radio.locations = None

        Each.run(
            [
                ("sem manage_messages", no_permission, radio.id, 403),
                ("sem premium", no_premium, radio.id, 403),
                ("rádio personalizada", lambda: None, custom.id, 404),
                ("outro servidor", lambda: None, "1:800", 404),
                ("sem árvore", no_tree, radio.id, 404),
            ],
            check,
        )

    def test_only_the_station_list_counts_against_the_write_limit(self):
        radio = self.seed_local()
        browser = self.logged_in()
        self.page(browser)
        for _ in range(WRITE_LIMIT_PER_MINUTE + 2):  # navegar é leitura em memória: sem limite
            assert self.local(browser, radio, "c:BR", gap=0).code == 200
        for _ in range(WRITE_LIMIT_PER_MINUTE):
            assert self.local(browser, radio, SP, gap=0).code == 200
        blocked = self.local(browser, radio, SP, gap=0)
        assert blocked.code == 429 and blocked.headers.get("Retry-After") == "60"
        assert len(self.radio.directory.region_calls) == WRITE_LIMIT_PER_MINUTE


# ============================================================================ estação principal (R-6)


class MainStationTest(LocalBase):
    def test_choosing_the_main_station_writes_source_station_switches_the_live_radio_and_keeps_the_list(self):
        radio = self.seed_local(reserve=B)
        browser = self.logged_in()
        response = self.post_station(browser, radio, B, extra=[("node", SP), ("pagina", "1"), ("channel_id", "9")])
        body = response.body.decode()
        assert response.code == 200 and SAVED_TEXT in body and "<html" not in body.lower()
        saved = self.radio.store.docs[radio.id]
        assert saved.source == {"station": B, "reserve": None} and saved.rev == radio.rev + 1  # reserva igual: zerada
        assert saved.channel_id == CHANNEL and self.radio.store.updates == [(radio.id, "1001")]
        assert self.radio.manager.station_changes == [saved]
        audit = self.audit_text()
        assert "escopo=radio:1 categoria=radios campos=source.station" in audit and B not in audit
        assert selected(self.local_form(body)) == ["r:america_latina", "c:BR", "s:BR:27", SP]
        assert uuids(body, "estacao") == [C, A] and self.radio.directory.region_calls == [(SP, ALFA)]  # a lista volta
        assert "Beta FM (Portugal · MP3 128 kbps)" in body  # agora como principal

    def test_the_search_list_also_comes_back_after_the_post(self):
        radio = self.seed_local()
        body = self.post_station(self.logged_in(), radio, B, extra=[("q", "fm")]).body.decode()
        assert self.radio.directory.calls == [("fm", ALFA)] and uuids(body, "reserva") == [C, A]
        assert 'name="q" value="fm"' in body

    def test_invalid_main_stations_are_422_or_409_without_writing(self):
        radio = self.seed_local()
        self.radio.health.down.add(C)
        browser = self.logged_in()

        def check(case):
            name, value, kwargs, status, text = case
            self.radio.store.set_station_error = kwargs.pop("store_error", None)
            response = self.post_station(browser, radio, value, **kwargs)
            self.radio.store.set_station_error = None
            assert response.code == status, f"{name}: status {response.code}"
            assert text in html.unescape(response.body.decode()), name
            assert self.radio.store.updates == [] and self.radio.manager.station_changes == [], name

        Each.run(
            [
                ("uuid inválido", "zzz", {}, 422, "não pode ser a principal"),
                ("vazia", "", {}, 422, "não pode ser a principal"),
                ("indisponível", C, {}, 422, "não pode ser a principal"),
                ("traversal", "../" + B, {}, 422, "não pode ser a principal"),
                ("versão velha", B, {"versao": "1"}, 409, CONFLICT_TEXT),
                ("store recusou depois", B, {"store_error": RadioConflict()}, 409, CONFLICT_TEXT),
            ],
            check,
        )
        same = self.post_station(browser, radio, A)
        assert same.code == 200 and self.radio.store.updates == []  # a mesma principal: nada a gravar
        self.radio.store.docs.clear()
        custom = self.radio.store.seed(1, ALFA, CHANNEL)
        pairs = [("versao", str(custom.rev)), ("estacao", B)]
        response = self.post_raw(browser, f"{self.url(ALFA, custom.id)}/estacao", pairs, htmx=True)
        assert response.code == 422 and "Só rádios ao vivo" in html.unescape(response.body.decode())
        assert self.radio.store.docs[custom.id].source == custom.source

    def test_manager_failure_does_not_undo_the_save_and_is_only_a_warning(self):
        radio = self.seed_local()
        self.radio.manager.fail_with = RuntimeError("varredor caiu")
        response = self.post_station(self.logged_in(), radio, B)
        assert response.code == 200 and self.radio.store.docs[radio.id].source["station"] == B
        assert "não trocou" in self.all_log_text() and "varredor caiu" not in self.all_log_text()

    def test_without_permission_the_post_is_403_and_the_list_is_not_reloaded(self):
        radio = self.seed_local()
        browser = self.logged_in()
        self.world.channels[CHANNEL] = make_channel(CHANNEL, "Sala", ALFA, manage_messages=False)
        pairs = [("versao", str(radio.rev)), ("estacao", B), ("node", SP)]
        response = self.post_raw(browser, f"{self.url(ALFA, radio.id)}/estacao", pairs, htmx=True)
        assert response.code == 403 and self.radio.store.updates == []
        assert self.radio.directory.region_calls == [] and self.radio.manager.station_changes == []

    def test_body_and_rate_limits_apply(self):
        radio = self.seed_local()
        browser = self.logged_in()
        big = self.post_station(browser, radio, B, extra=[("enchimento", "x" * (MAX_BODY_BYTES + 1))])
        assert big.code == 413
        for _ in range(WRITE_LIMIT_PER_MINUTE):
            assert self.post_station(browser, radio, "zzz", gap=0).code == 422
        blocked = self.post_station(browser, radio, B, gap=0)
        assert blocked.code == 429 and blocked.headers.get("Retry-After") == "60"
        assert self.radio.store.updates == []
