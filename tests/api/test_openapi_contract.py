"""Contrato de ``docs/api/openapi.yaml`` (T-456 §12, B1b): as respostas reais das rotas validam contra o arquivo.

Cada caso faz uma requisição de verdade à API de teste e confere status, ``code`` do erro, corpo (JSON Schema do
arquivo) e cabeçalhos declarados. No fim, todo par rota+status documentado tem de ter sido exercitado: o arquivo não
promete resposta que o bot não dá, e uma resposta nova sem documentação quebra o teste.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from ruamel.yaml import YAML

from empiremuse.api.handlers import HealthHandler, StatusHandler
from empiremuse.api.server import build_api_app
from tests.api.support import CODE, VERIFIER, ApiTestCase, body_of
from tests.api.test_sessions import lista

pytestmark = pytest.mark.critical

SPEC_PATH = Path(__file__).resolve().parents[2] / "docs" / "api" / "openapi.yaml"
SPEC_URI = "urn:empiremuse:openapi"
HTTP_METHODS = ("get", "put", "post", "delete", "patch")
PEER = "127.0.0.1"
USER_ID = "1001"
KEY_FAILURES_PER_MINUTE = 30
SESSIONS = "/api/v1/sessions"
SESSION = "/api/v1/session"
META = "/api/v1/meta"
HEALTH = "/api/v1/health"
STATUS = "/api/v1/status"
FAILURE = RuntimeError("falha inesperada")
FOUNDATION_PATHS = {META, HEALTH, STATUS, SESSIONS, SESSION}  # as rotas de dados (B3 em diante) têm o teste delas


def load_spec() -> dict:
    return YAML(typ="safe").load(SPEC_PATH.read_text(encoding="utf-8"))


def documented(spec: dict) -> set[tuple[str, str, str]]:
    return {
        (method.upper(), path, status)
        for path, item in spec["paths"].items()
        for method, operation in item.items()
        if method in HTTP_METHODS
        for status in operation["responses"]
    }


class SpecChecker:
    """Valida uma resposta real contra o que o arquivo declara para ``método + caminho + status``."""

    def __init__(self, spec: dict) -> None:
        self.spec = spec
        resource = Resource.from_contents(spec, default_specification=DRAFT202012)
        self.registry = Registry().with_resource(SPEC_URI, resource)

    def resolve(self, node: dict) -> dict:
        """Segue ``$ref`` locais (``KeyError`` se a referência não existe)."""
        while "$ref" in node and node["$ref"].startswith("#/"):
            target = self.spec
            for part in node["$ref"][2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            node = target
        return node

    def validate(self, schema: dict, instance: object) -> None:
        wrapped = {"$ref": SPEC_URI + schema["$ref"]} if "$ref" in schema else schema
        Draft202012Validator(wrapped, registry=self.registry).validate(instance)

    def check(self, method: str, path: str, response) -> tuple[str, str, str]:
        key = (method, path, str(response.code))
        operation = self.spec["paths"][path][method.lower()]
        assert key[2] in operation["responses"], f"{key}: status não documentado"
        declared = self.resolve(operation["responses"][key[2]])
        self._check_headers(key, declared, response)
        content = declared.get("content")
        if not content:
            assert not response.body, f"{key}: o arquivo declara resposta sem corpo"
        else:
            assert response.headers["Content-Type"].startswith("application/json"), key
            self.validate(content["application/json"]["schema"], body_of(response))
        return key

    def _check_headers(self, key: tuple[str, str, str], declared: dict, response) -> None:
        for name, header in declared.get("headers", {}).items():
            header = self.resolve(header)
            value = response.headers.get(name)
            assert value is not None or not header.get("required"), f"{key}: falta o cabeçalho {name}"
            if value is not None:
                self.validate(header["schema"], int(value) if header["schema"].get("type") == "integer" else value)


class OpenApiContractTest(ApiTestCase):
    spec = load_spec()
    seen: set[tuple[str, str, str]] = set()
    ran: set[str] = set()

    def setUp(self):
        super().setUp()
        self.ran.add(self._testMethodName)

    def expect(self, method: str, path: str, response, status: int, code: str | None = None) -> None:
        assert response.code == status, response.body
        if code is not None:
            assert body_of(response)["error"]["code"] == code
        self.seen.add(SpecChecker(self.spec).check(method, path, response))

    def exhaust_key_failures(self) -> None:
        for _ in range(KEY_FAILURES_PER_MINUTE):
            self.ctx.limits.key_failures.allow(PEER)

    # ------------------------------------------------------------------ /meta e /health

    def test_meta(self):
        self.expect("GET", META, self.api("GET", "/meta"), 200)
        self.expect("GET", META, self.api("GET", "/meta", key=None), 401, "forbidden")
        self.expect("GET", META, self.api("GET", "/meta", key="errada"), 401, "forbidden")

    def test_meta_nao_pronto_e_limite_de_falhas_de_chave(self):
        self.pool.bots.clear()
        self.expect("GET", META, self.api("GET", "/meta"), 503, "bot_unavailable")
        self.exhaust_key_failures()
        self.expect("GET", META, self.api("GET", "/meta", key="errada"), 429, "rate_limited")

    def test_meta_erro_inesperado(self):
        with patch("empiremuse.api.handlers.resolve_client_id", side_effect=FAILURE):
            self.expect("GET", META, self.api("GET", "/meta"), 500, "bot_unavailable")

    def test_status(self):
        self.pool.bots[:] = [SimpleNamespace(is_ready=lambda: True, user="Muse#0001", guilds=[1, 2])]
        self.pool.commit = "0123456789abcdef0123456789abcdef01234567"
        self.expect("GET", STATUS, self.api("GET", "/status"), 200)
        self.expect("GET", STATUS, self.api("GET", "/status", key=None), 401, "forbidden")
        self.expect("GET", STATUS, self.api("GET", "/status", key="errada"), 401, "forbidden")

    def test_status_nada_subiu_limite_e_erro_inesperado(self):
        self.pool.bots.clear()
        self.expect("GET", STATUS, self.api("GET", "/status"), 503, "bot_unavailable")
        self.exhaust_key_failures()
        self.expect("GET", STATUS, self.api("GET", "/status", key="errada"), 429, "rate_limited")

    def test_status_erro_inesperado(self):
        with patch.object(StatusHandler, "get", side_effect=FAILURE):
            self.expect("GET", STATUS, self.api("GET", "/status"), 500, "bot_unavailable")

    def test_health(self):
        self.expect("GET", HEALTH, self.api("GET", "/health", key=None), 200)
        with patch.object(HealthHandler, "get", side_effect=FAILURE):
            self.expect("GET", HEALTH, self.api("GET", "/health", key=None), 500, "bot_unavailable")

    # ------------------------------------------------------------------ POST /sessions

    def test_login_e_reautenticacao(self):
        self.expect("POST", SESSIONS, self.login(), 201)
        token = self.new_session(USER_ID)
        self.oauth.identities["reauth"] = {"id": USER_ID, "username": "alice"}
        self.expect("POST", SESSIONS, self.login("reauth", reauth=True), 401, "session_expired")  # sem sessão

        reauth = self.api(
            "POST", "/sessions", session=token, body={"code": "reauth", "code_verifier": VERIFIER, "reauth": True}
        )
        self.expect("POST", SESSIONS, reauth, 201)

        self.oauth.identities["outro"] = {"id": "2002", "username": "bob"}
        mismatch = self.api(
            "POST",
            "/sessions",
            session=body_of(reauth)["session_token"],
            body={"code": "outro", "code_verifier": VERIFIER, "reauth": True},
        )
        self.expect("POST", SESSIONS, mismatch, 403, "reauth_user_mismatch")

    def test_login_corpo_invalido_chave_e_content_type(self):
        self.expect("POST", SESSIONS, self.api("POST", "/sessions", body={"code": 1}), 400, "validation_failed")
        self.expect("POST", SESSIONS, self.login("desconhecido"), 400, "validation_failed")
        self.expect("POST", SESSIONS, self.login(key=None), 401, "forbidden")
        texto = self.api("POST", "/sessions", raw_body="x", headers={"Content-Type": "text/plain"})
        self.expect("POST", SESSIONS, texto, 415, "validation_failed")

    def test_login_banido_e_lista_de_banidos_nao_carregada(self):
        self.pool.user_bans = lista(int(USER_ID))
        self.expect("POST", SESSIONS, self.login(), 403, "forbidden")
        self.pool.user_bans = lista(carregada=False)
        self.expect("POST", SESSIONS, self.login(), 503, "bot_unavailable")

    def test_login_discord_fora_e_erro_inesperado(self):
        self.oauth.fail_at = "user"
        self.expect("POST", SESSIONS, self.login(), 502, "discord_unavailable")
        self.oauth.fail_at = None
        self.oauth.identities[CODE] = {"id": USER_ID, "username": "alice"}
        with patch.object(self.oauth, "exchange_code", side_effect=FAILURE):
            self.expect("POST", SESSIONS, self.login(), 500, "bot_unavailable")

    def test_login_limite(self):
        self.exhaust_key_failures()
        self.expect("POST", SESSIONS, self.login(key="errada"), 429, "rate_limited")

    # ------------------------------------------------------------------ GET e DELETE /session

    def test_get_session(self):
        token = self.new_session(USER_ID)
        self.expect("GET", SESSION, self.api("GET", "/session", session=token), 200)
        self.expect("GET", SESSION, self.api("GET", "/session"), 401, "session_expired")
        self.expect("GET", SESSION, self.api("GET", "/session", key=None), 401, "forbidden")
        with patch.object(self.store, "expires_at", side_effect=FAILURE):
            self.expect("GET", SESSION, self.api("GET", "/session", session=token), 500, "bot_unavailable")

    def test_get_session_limite_de_leitura(self):
        token = self.new_session(USER_ID)
        while self.ctx.limits.user_read.allow(USER_ID):
            pass
        self.expect("GET", SESSION, self.api("GET", "/session", session=token), 429, "rate_limited")

    def test_delete_session(self):
        token = self.new_session(USER_ID)
        self.expect("DELETE", SESSION, self.api("DELETE", "/session", session=token), 204)
        self.expect("DELETE", SESSION, self.api("DELETE", "/session", session=token), 401, "session_expired")

    def test_delete_session_erro_inesperado_e_limite_de_escrita(self):
        token = self.new_session(USER_ID)
        with patch.object(self.store, "destroy_api", side_effect=FAILURE):
            self.expect("DELETE", SESSION, self.api("DELETE", "/session", session=token), 500, "bot_unavailable")
        while self.ctx.limits.user_write.allow(USER_ID):
            pass
        self.expect("DELETE", SESSION, self.api("DELETE", "/session", session=token), 429, "rate_limited")

    # ------------------------------------------------------------------ o arquivo em si

    def test_request_id_do_cabecalho_e_o_do_envelope(self):
        response = self.api("GET", "/meta", key=None, headers={"X-Request-ID": "abc-12345_XYZ"})

        assert response.headers["X-Request-ID"] == body_of(response)["error"]["request_id"] == "abc-12345_XYZ"

    def test_o_arquivo_documenta_exatamente_as_rotas_da_api(self):
        # a rota com parâmetro leva o caminho do arquivo como nome (``api/server.py``)
        served = {
            rule.name or rule.matcher.regex.pattern.rstrip("$")
            for rule in build_api_app(self.ctx).wildcard_router.rules
        }

        assert served == set(self.spec["paths"])

    def test_arquivo_e_openapi_3_1_com_referencias_e_schemas_validos(self):
        checker = SpecChecker(self.spec)
        refs: list[str] = []

        def collect(node: object) -> None:
            if isinstance(node, dict):
                refs.extend(value for key, value in node.items() if key == "$ref")
                list(map(collect, node.values()))
            elif isinstance(node, list):
                list(map(collect, node))

        collect(self.spec)

        assert self.spec["openapi"].startswith("3.1")
        assert refs
        for ref in refs:
            checker.resolve({"$ref": ref})
        for schema in self.spec["components"]["schemas"].values():
            Draft202012Validator.check_schema(schema)

    def test_zz_todo_status_documentado_foi_exercitado(self):
        casos = {name for name in dir(self) if name.startswith("test_") and not name.startswith("test_zz")}
        if not casos <= self.ran:
            pytest.skip("só vale com todos os casos da classe rodando (o ``-k`` deixa casos de fora)")

        faltando = {item for item in documented(self.spec) if item[1] in FOUNDATION_PATHS} - self.seen

        assert not faltando, f"documentados e nunca observados: {sorted(faltando)}"
