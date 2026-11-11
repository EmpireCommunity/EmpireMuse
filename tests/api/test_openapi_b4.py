"""Contrato das rotas de Jukebox e de rádios (B4, ``docs/api/openapi.yaml``): as respostas reais validam contra o arquivo e todo
par rota+status documentado dessas rotas é observado (o arquivo não promete resposta que o bot não dá)."""

from contextlib import contextmanager
from unittest.mock import patch

import pytest

from empiremuse.api.data import DataHandler
from empiremuse.utils.radio.premium import RadioUnavailable
from empiremuse.utils.radio.setup import RadioForbidden
from empiremuse.utils.servers import ServerConfigUnavailable
from tests.api.support import body_of
from tests.api.support_jukebox import BASE as JUKEBOX, JukeboxApiCase
from tests.api.support_radio import ALFA, BASE, CHANNEL, LIVE_UUID, OTHER_UUID, RadioApiCase, station
from tests.api.test_openapi_contract import SpecChecker, documented, load_spec

pytestmark = pytest.mark.critical

API = "/api/v1"
FAILURE = RuntimeError("falha inesperada")
USER = "1001"
JSON = {"Content-Type": "application/json"}
G = "/guilds/{guild_id}"
CHAT = {"voice_delete_after_s": 600, "stage_max_messages": 20, "stage_max_age_s": 3600}
STATION_VALUES = {"name": "Minha", "kind": "artists", "artists": ["Queen"]}


def is_jukebox(path: str) -> bool:
    return "/jukebox" in path


def is_radio(path: str) -> bool:
    return "/radios" in path or "/radio-stations" in path


class CommonCoverage:
    """O que toda rota de dados responde, igual em Jukebox e rádios."""

    spec = load_spec()
    seen: set[tuple[str, str, str]]

    def expect(self, method, template, response, status, code=None):
        assert response.code == status, response.body
        if code is not None:
            assert body_of(response)["error"]["code"] == code
        self.seen.add(SpecChecker(self.spec).check(method, API + template, response))

    def crash(self):
        return patch.object(DataHandler, "guild_access", side_effect=FAILURE)

    def cover_common(self, method, template, path, *, body=None, unavailable=None):
        """401 sem sessão, 503 (se a rota tem), 500 e, por último, 429 (o balde do usuário fica esgotado)."""
        write = method in ("PUT", "POST", "PATCH", "DELETE")
        self.expect(method, template, self.api(method, path, body=body), 401, "session_expired")
        if unavailable is not None:
            with unavailable():
                self.expect(method, template, self.call(method, path, body), 503, "bot_unavailable")
        with self.crash():
            self.expect(method, template, self.call(method, path, body), 500, "bot_unavailable")
        limiter = self.ctx.limits.user_write if write else self.ctx.limits.user_read
        while limiter.allow(USER):
            pass
        self.expect(method, template, self.call(method, path, body), 429, "rate_limited")
        self.advance(61)

    def cover_not_found(self, method, template, path, body=None):
        self.expect(method, template, self.call(method, path, body, session=self.stranger), 404, "not_found")

    def cover_media(self, method, template, path):
        plain = self.call(method, path, raw_body="x", headers={"Content-Type": "text/plain"})
        self.expect(method, template, plain, 415, "validation_failed")

    def cover_key_reuse(self, method, template, path, body, other):
        """A mesma chave com outro pedido (``other`` é um corpo válido, só diferente) é ``409`` sem repetir a resposta."""
        assert self.call(method, path, body, key="chave-de-contrato").code < 300
        self.expect(method, template, self.call(method, path, other, key="chave-de-contrato"), 409, "conflict")


class OpenApiRadioContractTest(CommonCoverage, RadioApiCase):
    seen: set[tuple[str, str, str]] = set()
    ran: set[str] = set()

    def setUp(self):
        super().setUp()
        self.ran.add(self._testMethodName)
        self.stranger = self.new_session("3003")  # não gerencia nenhum servidor

    @contextmanager
    def radios_down(self):
        self.radio.premium.fail_with = RadioUnavailable()
        try:
            yield
        finally:
            self.radio.premium.fail_with = None

    def radio_world(self):
        self.premium()
        self.radio.setup.bots[ALFA] = ((2, "Bot Dois"), (3, "Bot Três"))
        self.radio.setup.channels[ALFA] = ((CHANNEL, "Sala DJ", "voice"), (321, "Palco", "stage"))
        return self.seed_live(reserve=OTHER_UUID)

    # ------------------------------------------------------------------ lista, criar e uma rádio

    def test_radios_lista_e_criar(self):
        t = G + "/radios"
        radio = self.radio_world()
        self.expect("GET", t, self.call("GET", f"{BASE}/radios"), 200)
        self.cover_not_found("GET", t, f"{BASE}/radios")
        self.cover_common("GET", t, f"{BASE}/radios", unavailable=self.radios_down)

        values = {"kind": "custom", "channel_id": str(CHANNEL), "bot_id": "2"}
        self.expect("POST", t, self.call("POST", f"{BASE}/radios", {"values": values}), 201)
        self.expect("POST", t, self.call("POST", f"{BASE}/radios", {"values": {}}), 400, "validation_failed")
        self.cover_not_found("POST", t, f"{BASE}/radios", {"values": values})
        self.cover_media("POST", t, f"{BASE}/radios")
        self.cover_key_reuse(
            "POST", t, f"{BASE}/radios", {"values": {**values, "bot_id": "3"}}, {"values": {**values, "kind": "live"}}
        )
        self.radio.premium.docs.clear()
        self.expect("POST", t, self.call("POST", f"{BASE}/radios", {"values": values}), 403, "forbidden")
        self.premium()
        self.cover_common("POST", t, f"{BASE}/radios", body={"values": values}, unavailable=self.radios_down)
        assert radio

    def test_radio_ler_e_configurar(self):
        t = G + "/radios/{radio_id}"
        radio = self.radio_world()
        url = self.radio_url(radio)
        self.expect("GET", t, self.call("GET", url), 200)
        self.cover_not_found("GET", t, url)
        self.cover_common("GET", t, url, unavailable=self.radios_down)

        values = {"kind": "live", "channel_id": "321", "bot_id": "1"}
        body = {"expected_version": str(radio.rev), "values": values}
        self.radio.store.docs[radio.id] = self.radio.store.seed_live(1, ALFA, CHANNEL, station=LIVE_UUID, enabled=False)
        fresh = self.radio.store.docs[radio.id]
        body = {"expected_version": str(fresh.rev), "values": values}
        self.expect("PUT", t, self.call("PUT", url, body), 200)
        self.expect("PUT", t, self.call("PUT", url, {**body, "expected_version": "1"}), 409, "conflict")
        self.expect("PUT", t, self.call("PUT", url, {"values": values}), 400, "validation_failed")
        self.cover_not_found("PUT", t, url, body)
        self.cover_media("PUT", t, url)
        self.radio.premium.docs.clear()
        self.expect("PUT", t, self.call("PUT", url, body), 403, "forbidden")
        self.premium()
        self.cover_common("PUT", t, url, body=body, unavailable=self.radios_down)

    def check_radio_write(self, suffix, template_tail, values, version_values, other_values):
        """Os casos comuns de uma escrita de rádio que exige premium e Gerenciar Mensagens."""
        t = G + "/radios/{radio_id}" + template_tail
        radio = self.radio_world()
        url = self.radio_url(radio, suffix)
        body = {"expected_version": str(radio.rev), "values": values}
        self.expect("PUT", t, self.call("PUT", url, body), 200)
        fresh = self.radio.store.docs[radio.id]
        again = {"expected_version": str(fresh.rev), "values": values}
        self.expect("PUT", t, self.call("PUT", url, {**again, "expected_version": "1"}), 409, "conflict")
        self.expect("PUT", t, self.call("PUT", url, {**again, "values": version_values}), 400, "validation_failed")
        self.cover_not_found("PUT", t, url, again)
        self.cover_media("PUT", t, url)
        self.cover_key_reuse("PUT", t, url, again, {**again, "values": other_values})
        self.radio.premium.docs.clear()
        self.expect("PUT", t, self.call("PUT", url, again), 403, "forbidden")
        self.premium()
        self.cover_common("PUT", t, url, body=again, unavailable=self.radios_down)

    def test_radio_chat(self):
        self.check_radio_write(
            "/chat", "/chat", CHAT, {"voice_delete_after_s": 600}, {**CHAT, "stage_max_messages": 30}
        )

    def test_radio_station(self):
        self.check_radio_write(
            "/station", "/station", {"estacao": OTHER_UUID}, {"estacao": "lixo"}, {"estacao": "outra"}
        )

    def test_radio_reserve(self):
        self.check_radio_write("/reserve", "/reserve", {"reserva": None}, {"reserva": 5}, {"reserva": "outra"})

    # ------------------------------------------------------------------ busca, cascata e logo

    def check_directory_read(self, suffix, template_tail, ok_query, bad_query):
        t = G + "/radios/{radio_id}" + template_tail
        radio = self.radio_world()
        self.radio.directory.results = [station(OTHER_UUID)]
        self.radio.directory.region_hits = [station(OTHER_UUID)]
        url = self.radio_url(radio, suffix)
        self.expect("GET", t, self.call("GET", url + ok_query), 200)
        self.expect("GET", t, self.call("GET", url + bad_query), 400, "validation_failed")
        self.cover_not_found("GET", t, url + ok_query)
        self.radio.premium.docs.clear()
        self.expect("GET", t, self.call("GET", url + ok_query), 403, "forbidden")
        self.premium()
        self.cover_common("GET", t, url + ok_query, unavailable=self.radios_down)

    def test_radio_busca(self):
        self.check_directory_read("/stations", "/stations", "?q=jazz", "?q=a")

    def test_radio_cascata(self):
        self.check_directory_read("/local", "/local", "?node=c:BR", "?node=forjado")
        radio = self.radio.store.docs[f"1:{ALFA}"]
        self.radio.locations, tree = None, self.radio.locations  # sem árvore: ``404``
        self.expect(
            "GET", G + "/radios/{radio_id}/local", self.call("GET", self.radio_url(radio, "/local")), 404, "not_found"
        )
        self.radio.locations = tree

    def test_radio_logo(self):
        t = G + "/radios/{radio_id}/logo"
        radio = self.radio_world()
        url = self.radio_url(radio, "/logo")
        self.expect("GET", t, self.call("GET", url), 200)
        self.cover_not_found("GET", t, url)
        self.cover_common("GET", t, url, unavailable=self.radios_down)

    # ------------------------------------------------------------------ redefinir

    def test_radio_reset_prepare(self):
        t = G + "/radios/{radio_id}/reset/prepare"
        radio = self.radio_world()
        url = self.radio_url(radio, "/reset/prepare")
        self.expect("POST", t, self.call("POST", url, {}), 200)
        self.expect("POST", t, self.call("POST", url, raw_body="[]", headers=JSON), 400, "validation_failed")
        self.cover_not_found("POST", t, url, {})
        self.cover_media("POST", t, url)
        self.cover_common("POST", t, url, body={}, unavailable=self.radios_down)

    def test_radio_reset(self):
        t = G + "/radios/{radio_id}/reset"
        radio = self.radio_world()
        url = self.radio_url(radio, "/reset")

        def token():
            return body_of(self.call("POST", url + "/prepare", {}))["confirm_token"]

        self.expect("POST", t, self.call("POST", url, {"confirm_token": "lixo"}), 400, "validation_failed")
        self.cover_not_found("POST", t, url, {"confirm_token": "lixo"})
        self.cover_media("POST", t, url)
        self.expect("POST", t, self.call("POST", url, {"confirm_token": token()}), 204)
        self.seed_live()
        self.cover_key_reuse("POST", t, url, {"confirm_token": token()}, {"confirm_token": "b" * 20})
        self.seed_live()
        self.radio.setup.fail_with = RadioForbidden("manage_guild")
        self.expect("POST", t, self.call("POST", url, {"confirm_token": token()}), 403, "forbidden")
        self.radio.setup.fail_with = None
        self.cover_common("POST", t, url, body={"confirm_token": "c" * 20}, unavailable=self.radios_down)

    # ------------------------------------------------------------------ estações do servidor

    def test_estacoes_lista_e_criar(self):
        t = G + "/radio-stations"
        self.premium()
        url = f"{BASE}/radio-stations"
        self.expect("GET", t, self.call("GET", url), 200)
        self.cover_not_found("GET", t, url)
        self.radio.premium.docs.clear()
        self.expect("GET", t, self.call("GET", url), 403, "forbidden")
        self.premium()
        self.cover_common("GET", t, url, unavailable=self.radios_down)

        self.expect("POST", t, self.call("POST", url, {"values": STATION_VALUES}), 201)
        self.expect(
            "POST", t, self.call("POST", url, {"values": {**STATION_VALUES, "kind": "x"}}), 400, "validation_failed"
        )
        self.cover_not_found("POST", t, url, {"values": STATION_VALUES})
        self.cover_media("POST", t, url)
        self.cover_key_reuse(
            "POST", t, url, {"values": {**STATION_VALUES, "name": "Outra"}}, {"values": STATION_VALUES}
        )
        self.radio.premium.docs.clear()
        self.expect("POST", t, self.call("POST", url, {"values": STATION_VALUES}), 403, "forbidden")
        self.premium()
        self.cover_common("POST", t, url, body={"values": STATION_VALUES}, unavailable=self.radios_down)

    def test_estacao_editar_e_apagar(self):
        t = G + "/radio-stations/{station_id}"
        self.premium()
        item = self.radio.stations.seed(ALFA, "Pronta")
        url = f"{BASE}/radio-stations/{item.id}"
        body = {"expected_version": "1", "values": {**STATION_VALUES, "name": "Editada"}}
        self.expect("PUT", t, self.call("PUT", url, body), 200)
        self.expect("PUT", t, self.call("PUT", url, {**body, "expected_version": "9"}), 409, "conflict")
        self.expect("PUT", t, self.call("PUT", url, {"values": {}}), 400, "validation_failed")
        self.cover_not_found("PUT", t, url, body)
        self.cover_media("PUT", t, url)
        self.cover_key_reuse(
            "PUT",
            t,
            url,
            {**body, "expected_version": "2"},
            {**body, "expected_version": "2", "values": STATION_VALUES},
        )
        self.radio.premium.docs.clear()
        self.expect("PUT", t, self.call("PUT", url, body), 403, "forbidden")
        self.premium()
        self.cover_common("PUT", t, url, body=body, unavailable=self.radios_down)

        current = f"?expected_version={self.radio.stations.docs[item.id].rev}"
        self.expect("DELETE", t, self.call("DELETE", url + "?expected_version=99"), 409, "conflict")
        self.expect("DELETE", t, self.call("DELETE", url), 400, "validation_failed")
        self.cover_not_found("DELETE", t, url + current)
        self.radio.premium.docs.clear()
        self.expect("DELETE", t, self.call("DELETE", url + current), 403, "forbidden")
        self.premium()
        self.cover_common("DELETE", t, url + current, unavailable=self.radios_down)
        self.expect("DELETE", t, self.call("DELETE", url + current), 204)

    def test_zz_todo_status_documentado_das_rotas_de_radio_foi_exercitado(self):
        casos = {name for name in dir(self) if name.startswith("test_") and not name.startswith("test_zz")}
        if not casos <= self.ran:
            pytest.skip("só vale com todos os casos da classe rodando (o ``-k`` deixa casos de fora)")

        faltando = {item for item in documented(self.spec) if is_radio(item[1])} - self.seen

        assert not faltando, f"documentados e nunca observados: {sorted(faltando)}"


class OpenApiJukeboxContractTest(CommonCoverage, JukeboxApiCase):
    seen: set[tuple[str, str, str]] = set()
    ran: set[str] = set()

    def setUp(self):
        super().setUp()
        self.ran.add(self._testMethodName)
        permissions = self.world.guild.members[2002].guild_permissions
        permissions.administrator = permissions.manage_guild = False  # o 2002 não gerencia: o ``404``
        self.stranger = self.new_session("2002")

    @contextmanager
    def config_down(self):
        with patch.object(self.world.pool.servers.jukebox, "get", side_effect=ServerConfigUnavailable()):
            yield

    @contextmanager
    def settings_down(self):
        with patch.object(self.world.pool.servers, "settings", side_effect=ServerConfigUnavailable()):
            yield

    def test_jukebox_ler(self):
        t = G + "/jukebox"
        self.expect("GET", t, self.call("GET", JUKEBOX), 200)
        self.cover_not_found("GET", t, JUKEBOX)
        self.cover_common("GET", t, JUKEBOX, unavailable=self.config_down)

    def test_jukebox_opcoes(self):
        t = G + "/jukebox"
        values = {"jukebox_auto_delete": True, "jukebox_text_requests": False}
        version = self.state()["options"]["version"]
        body = {"expected_version": version, "values": values}
        self.expect("PUT", t, self.call("PUT", JUKEBOX, body), 200)
        self.expect("PUT", t, self.call("PUT", JUKEBOX, body), 409, "conflict")
        self.expect("PUT", t, self.call("PUT", JUKEBOX, {"values": values}), 400, "validation_failed")
        self.cover_not_found("PUT", t, JUKEBOX, body)
        self.cover_media("PUT", t, JUKEBOX)
        fresh = {
            "expected_version": self.state()["options"]["version"],
            "values": {**values, "jukebox_auto_delete": False},
        }
        self.cover_key_reuse("PUT", t, JUKEBOX, fresh, {**fresh, "values": values})
        self.cover_common("PUT", t, JUKEBOX, body=body, unavailable=self.settings_down)

    def test_jukebox_tarefas(self):
        t = G + "/jukebox/tasks"
        url = JUKEBOX + "/tasks"
        self.world.configured_text()
        version = self.state()["version"]
        reset = {"expected_version": version, "action": "reset"}
        token = body_of(self.call("POST", url, reset))["confirm_token"]
        self.expect("POST", t, self.call("POST", url, reset), 200)
        with self.held_jobs():
            self.expect("POST", t, self.call("POST", url, {**reset, "confirm_token": token}), 202)
            self.expect("POST", t, self.call("POST", url, reset), 409, "conflict")  # o servidor já tem uma tarefa
        self.expect("POST", t, self.call("POST", url, {**reset, "expected_version": "0" * 16}), 409, "conflict")
        self.expect("POST", t, self.call("POST", url, {"action": "x"}), 400, "validation_failed")
        self.cover_not_found("POST", t, url, reset)
        self.cover_media("POST", t, url)
        self.cover_common("POST", t, url, body=reset, unavailable=self.config_down)

    def test_jukebox_tarefa_acompanhar(self):
        t = G + "/jukebox/tasks/{task_id}"
        self.world.configured_text()
        reset = {"expected_version": self.state()["version"], "action": "reset"}
        token = body_of(self.call("POST", JUKEBOX + "/tasks", reset))["confirm_token"]
        task = body_of(self.call("POST", JUKEBOX + "/tasks", {**reset, "confirm_token": token}))["task_id"]
        self.drain_tasks()
        url = f"{JUKEBOX}/tasks/{task}"
        self.expect("GET", t, self.call("GET", url), 200)
        self.expect("GET", t, self.call("GET", f"{JUKEBOX}/tasks/{'A' * 32}"), 404, "not_found")
        self.cover_not_found("GET", t, url)
        self.cover_common("GET", t, url)

    def test_zz_todo_status_documentado_das_rotas_da_jukebox_foi_exercitado(self):
        casos = {name for name in dir(self) if name.startswith("test_") and not name.startswith("test_zz")}
        if not casos <= self.ran:
            pytest.skip("só vale com todos os casos da classe rodando (o ``-k`` deixa casos de fora)")

        faltando = {item for item in documented(self.spec) if is_jukebox(item[1])} - self.seen

        assert not faltando, f"documentados e nunca observados: {sorted(faltando)}"
