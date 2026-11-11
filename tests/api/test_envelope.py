"""Envelope de erro, ``/meta``, ``/health``, ausência de CORS e de cookie (contrato T-456 §4 e §7, crítico)."""

import json

import pytest

from tests.api.helpers import SERVICE_KEY
from tests.api.support import CLIENT_ID, ApiTestCase, body_of, error_code

pytestmark = pytest.mark.critical


class EnvelopeTest(ApiTestCase):
    def test_erro_so_tem_code_fields_e_request_id_sem_texto_nem_rastreio(self):
        response = self.api("POST", "/sessions", body={"code": 1})

        erro = body_of(response)["error"]
        assert set(erro) == {"code", "fields", "request_id"}
        assert erro["code"] == "validation_failed" and erro["fields"] == ["code", "code_verifier"]
        assert response.headers["X-Request-ID"] == erro["request_id"]

    def test_request_id_do_painel_e_ecoado_so_se_tiver_formato_seguro(self):
        bom = self.api("GET", "/meta", key=None, headers={"X-Request-ID": "abc-12345_XYZ"})
        ruim = self.api("GET", "/meta", key=None, headers={"X-Request-ID": "1.2.3.4 injetado"})

        assert body_of(bom)["error"]["request_id"] == "abc-12345_XYZ"
        assert body_of(ruim)["error"]["request_id"] != "1.2.3.4 injetado"

    def test_caminho_inexistente_e_metodo_errado_dao_envelope(self):
        nao_existe = self.api("GET", "/nada")
        assert (nao_existe.code, error_code(nao_existe)) == (404, "not_found")
        fora = self.fetch("/", raise_error=False)
        assert fora.code == 404 and error_code(fora) == "not_found"  # fora de /api/v1, sem chave

        metodo = self.api("POST", "/meta", body={})
        assert (metodo.code, error_code(metodo)) == (405, "validation_failed")
        assert self.api("PUT", "/sessions", body={}).code == 405

    def test_toda_resposta_sai_no_store_e_nosniff_inclusive_erros(self):
        for response in (self.api("GET", "/meta"), self.api("GET", "/meta", key=None), self.api("GET", "/nada")):
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["X-Content-Type-Options"] == "nosniff"

    def test_sem_cors_e_sem_cookie(self):
        token = self.new_session()
        origem = {"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"}
        respostas = [
            self.api("GET", "/meta", headers=origem),
            self.api("OPTIONS", "/meta", headers=origem, key=None),
            self.api("GET", "/session", session=token, headers=origem),
            self.api("GET", "/meta", key=None, headers=origem),
        ]

        for response in respostas:
            assert not [h for h in response.headers if h.lower().startswith("access-control-")]
            assert "Set-Cookie" not in response.headers

    def test_corpo_acima_de_16_kib_e_recusado_mesmo_sendo_json_valido_e_abaixo_passa(self):
        from tornado.simple_httpclient import HTTPStreamClosedError

        from tests.api.support import CODE, VERIFIER

        def com_enchimento(tamanho):
            corpo = {"code": CODE, "code_verifier": VERIFIER, "enchimento": "x" * tamanho}
            return self.api("POST", "/sessions", body=corpo)

        assert com_enchimento(8 * 1024).code == 201
        self.oauth.identities[CODE] = {"id": "1002", "username": "u"}
        self.oauth.calls.clear()
        try:
            acima = com_enchimento(20 * 1024)
        except HTTPStreamClosedError:
            acima = None  # o servidor fecha a conexão antes de ler o corpo
        assert acima is None or acima.code in (400, 413)
        assert self.oauth.calls == []  # nunca chegou ao Discord


class MetaHealthTest(ApiTestCase):
    def test_meta_exige_a_chave_e_traz_versoes_client_id_e_redirect(self):
        response = self.api("GET", "/meta")

        assert response.code == 200
        assert body_of(response) == {
            "api_versions": ["v1"],
            "bot_version": "9.9.9",
            "client_id": CLIENT_ID,
            "redirect_uri": "https://painel.example.com/auth/callback",
            "features": ["sessions", "reauth", "account", "guilds", "guild-settings", "idempotency-key"],
        }
        assert SERVICE_KEY not in response.body.decode()
        assert self.api("GET", "/meta", key=None).code == 401

    def test_meta_sem_client_id_resolvido_da_503(self):
        self.pool.bots.clear()

        response = self.api("GET", "/meta")

        assert (response.code, error_code(response)) == (503, "bot_unavailable")

    def test_health_e_aberto_e_so_devolve_status_ok(self):
        response = self.api("GET", "/health", key=None)

        assert response.code == 200
        assert json.loads(response.body) == {"status": "ok"}
        assert self.api("GET", "/health", key="errada").code == 200
