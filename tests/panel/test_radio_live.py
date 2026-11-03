"""Item 8 (R3 §4, R-6/R-7) · Rádios: estação principal, reserva e o seletor por busca no painel.

Caminhos de risco: a página nunca mostra a URL do fluxo; nomes escapados; ⛔ para estação indisponível; a busca só
para quem pode editar (premium + ``manage_messages``) e só com termo válido (senão nenhuma chamada ao diretório, R-7);
a principal fica fora dos resultados; gravar a reserva só por ``source.reserve`` com compare-and-set em ``rev`` (409),
uuid inválido, igual à principal, indisponível ou rádio personalizada → 422 sem gravar; auditoria só com o nome do campo.
"""

import asyncio
import html
import re
from types import SimpleNamespace

from empiremuse.panel.handlers.settings import MAX_BODY_BYTES, WRITE_LIMIT_PER_MINUTE
from empiremuse.panel.services.base import CONFLICT_TEXT, SAVED_TEXT
from empiremuse.panel.services.radio_live import NAME_CACHE_SIZE, NAME_CACHE_TTL_S, LivePage, StationNames
from empiremuse.rules.errors import RuleError
from empiremuse.rules.radio_stations import Station
from empiremuse.utils.radio.store import RadioConflict, RadioInvalid, RadioNotFound
from tests.panel.helpers_radio import UNAVAILABLE, make_channel, radio_state
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

    def reserve_pairs(self, browser, **overrides):
        form = self.reserve_form(self.page(browser))
        assert form is not None
        return form.pairs(**overrides)

    def post_reserve(self, browser, radio_id, pairs, **kwargs):
        return self.post_raw(browser, f"{self.url(ALFA, radio_id)}/reserva", pairs, htmx=True, **kwargs)


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


# ============================================================================ limites, banco fora do ar e escape (Litmus)


class LiveLimitsTest(LiveBase):
    def test_body_over_the_limit_is_413_without_writing(self):
        radio = self.seed_live()
        browser = self.logged_in()
        pairs = self.reserve_pairs(browser, reserva=B) + [("enchimento", "x" * (MAX_BODY_BYTES + 1))]
        assert self.post_reserve(browser, radio.id, pairs).code == 413
        assert self.radio.store.updates == []

    def test_writes_and_searches_past_the_per_minute_limit_are_429_and_the_directory_is_not_called(self):
        radio = self.seed_live()
        browser = self.logged_in()
        pairs = self.reserve_pairs(browser, reserva="zzz")  # inválida: conta como escrita e não grava
        for _ in range(WRITE_LIMIT_PER_MINUTE):
            assert self.post_reserve(browser, radio.id, pairs, gap=0).code == 422
        blocked = self.post_reserve(browser, radio.id, self.reserve_pairs(browser, reserva=B), gap=0)
        assert blocked.code == 429 and blocked.headers.get("Retry-After") == "60"
        search = self.search(browser, radio, "fm")
        assert search.code == 429 and search.headers.get("Retry-After") == "60"
        assert self.radio.store.updates == [] and self.radio.directory.calls == []

    def test_search_counts_against_the_same_limit(self):
        radio = self.seed_live()
        browser = self.logged_in()
        self.page(browser)
        for _ in range(WRITE_LIMIT_PER_MINUTE):
            assert self.search(browser, radio, "fm").code == 200
        assert self.search(browser, radio, "fm").code == 429
        assert len(self.radio.directory.calls) == WRITE_LIMIT_PER_MINUTE


class LiveUnavailableTest(LiveBase):
    def test_database_outage_is_503_on_search_and_on_reserve_without_calling_the_directory(self):
        radio = self.seed_live()
        browser = self.logged_in()
        pairs = self.reserve_pairs(browser, reserva=B)
        self.radio.premium.fail_with = UNAVAILABLE()
        search = self.search(browser, radio, "fm")
        reserve = self.post_reserve(browser, radio.id, pairs)
        for response in (search, reserve):
            assert response.code == 503, f"status {response.code}"
            assert "não estão disponíveis agora" in response.body.decode()
        assert self.radio.directory.calls == [] and self.radio.store.updates == []


HOSTILE = ("<script>alert(1)</script>", '" onmouseover="alert(1)')


class LiveEscapeTest(LiveBase):
    def test_hostile_names_are_escaped_in_search_results_backup_bot_and_channel(self):
        radio = self.seed_live(reserve=B)
        script, attr = HOSTILE
        self.radio.directory.stations[B] = station(B, script, attr)
        self.radio.directory.results = [station(C, attr, script)]
        self.world.bot1.user.name = script
        self.world.channels[CHANNEL] = make_channel(CHANNEL, attr, ALFA)
        browser = self.logged_in()
        page = self.page(browser)
        results = self.search(browser, radio, "fm").body.decode()
        for body in (page, results):
            assert "<script>" not in body and 'onmouseover="alert' not in body and "alert(1)" in body
        assert page.count("&lt;script&gt;alert(1)&lt;/script&gt;") >= 2  # bot e reserva
        assert "&quot; onmouseover=&quot;alert(1)" in page  # canal e país da reserva
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in results and "&quot; onmouseover=" in results
        assert 'value="' + C + '"' in results


class LiveReserveLayersTest(LiveBase):
    def test_bad_version_custom_radio_and_store_refusals_are_handled_per_layer(self):
        live = self.seed_live()
        custom = self.radio.store.seed(2, ALFA, CHANNEL)
        self.world.bot2 = SimpleNamespace()  # o bot 2 não está em Alfa: a rádio custom é só do teste do 422
        browser = self.logged_in()
        pairs_ok = self.reserve_pairs(browser, reserva=B)

        def with_version(version):
            return [(k, version if k == "versao" else v) for k, v in pairs_ok]

        def check(case):
            name, radio_id, pairs, error, status = case
            self.radio.store.set_reserve_error = error
            response = self.post_reserve(browser, radio_id, pairs)
            self.radio.store.set_reserve_error = None
            assert response.code == status, f"{name}: status {response.code}"
            assert self.radio.store.updates == [], name

        Each.run(
            [
                ("versão não numérica", live.id, with_version("abc"), None, 422),
                ("versão com 13 dígitos", live.id, with_version("1" * 13), None, 422),
                ("store: rev mudou depois do pré-check", live.id, pairs_ok, RadioConflict(), 409),
                ("store: rádio sumiu depois do pré-check", live.id, pairs_ok, RadioNotFound(), 409),
                (
                    "store: recusa depois do pré-check",
                    live.id,
                    pairs_ok,
                    RadioInvalid(RuleError("radio_source_invalid", "reserve")),
                    422,
                ),
            ],
            check,
        )
        assert self.radio.store.docs[live.id].source["reserve"] is None

    def test_custom_radio_has_no_backup_even_with_a_valid_version(self):
        self.radio.premium.grant(ALFA)
        custom = self.radio.store.seed(1, ALFA, CHANNEL)
        browser = self.logged_in()
        pairs = [("versao", str(custom.rev)), ("reserva", B)]
        response = self.post_reserve(browser, custom.id, pairs)
        assert response.code == 422 and "Só rádios ao vivo" in html.unescape(response.body.decode())
        assert self.radio.store.updates == [] and self.radio.store.docs[custom.id].source == custom.source


class NoPremiumDirectoryTest(LiveBase):
    def test_server_without_premium_never_asks_the_directory_for_station_names(self):
        self.radio.store.seed_live(1, ALFA, CHANNEL, station=A, reserve=B)
        self.radio.directory.stations[A] = station(A, "Alfa")
        body = self.page(self.logged_in())
        assert "Estação ao vivo" not in body and self.radio.directory.by_uuid_calls == []


# ============================================================================ cache de nomes (unitário)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class RecordingDirectory:
    def __init__(self, stations):
        self.stations, self.calls = stations, []

    async def by_uuid(self, uuid):
        self.calls.append(uuid)
        return self.stations.get(uuid)


def test_station_names_cache_hits_within_the_ttl_expires_after_and_never_keeps_failures():
    async def run():
        clock = FakeClock()
        names = StationNames(clock=clock)
        directory = RecordingDirectory({A: station(A, "Alfa")})
        assert (await names.get(directory, A)).name == "Alfa" and await names.get(directory, A) is not None
        assert directory.calls == [A]
        clock.now += NAME_CACHE_TTL_S
        await names.get(directory, A)
        assert directory.calls == [A, A]  # venceu: consulta de novo
        assert await names.get(directory, B) is None and await names.get(directory, B) is None
        assert directory.calls.count(B) == 2  # falha não entra no cache

    asyncio.run(run())


def test_station_names_cache_is_bounded():
    async def run():
        names = StationNames(clock=FakeClock())
        uuids = [f"{i:08x}-0000-0000-0000-000000000000" for i in range(NAME_CACHE_SIZE + 5)]
        directory = RecordingDirectory({u: station(u, u[:8]) for u in uuids})
        for uuid in uuids:
            await names.get(directory, uuid)
        assert len(names._items) == NAME_CACHE_SIZE
        await names.get(directory, uuids[0])
        assert directory.calls.count(uuids[0]) == 2  # o mais antigo saiu

    asyncio.run(run())


def test_station_view_ignores_invalid_uuids_without_touching_the_directory():
    async def run():
        directory = RecordingDirectory({})
        page = LivePage()
        ctx = SimpleNamespace(pool=SimpleNamespace(radio=SimpleNamespace(directory=directory, health=None)))
        for raw in (None, "", "zzz", "../" + A, A.upper()):
            assert await page.station_view(ctx, raw) is None, raw
        assert directory.calls == []

    asyncio.run(run())
