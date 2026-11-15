"""Autenticação da API (contrato T-456 §2 e §14, crítico): chave de serviço, token de sessão preso à chave, rotação."""

import logging

import pytest

from tests.api.helpers import PREVIOUS_KEY, SERVICE_KEY, external_config
from tests.api.support import USER, ApiTestCase, body_of, error_code
from tests.api.support_data import OLD_VERSION, DataApiCase

pytestmark = pytest.mark.critical


class AuthTest(ApiTestCase):
    def test_sem_chave_ou_com_chave_errada_responde_401_forbidden(self):
        for key in (None, "", "errada", SERVICE_KEY + "x"):
            response = self.api("GET", "/meta", key=key)
            assert (response.code, error_code(response)) == (401, "forbidden")
        response = self.api("GET", "/meta", headers={"Authorization": f"Basic {SERVICE_KEY}"}, key=None)
        assert response.code == 401

    def test_chave_certa_sem_sessao_nas_rotas_de_usuario_responde_401_session_expired(self):
        for method, path in (("GET", "/session"), ("DELETE", "/session")):
            response = self.api(method, path)
            assert (response.code, error_code(response)) == (401, "session_expired")

    def test_token_de_sessao_sem_a_chave_de_servico_nao_vale(self):
        token = self.new_session()

        assert self.api("GET", "/session", key=None, session=token).code == 401
        assert self.api("GET", "/session", session=token).code == 200

    def test_token_de_formato_estranho_e_tratado_como_ausente(self):
        for token in ("curto", "a" * 200, "com espaço " * 3, "é" * 30):
            try:
                response = self.api("GET", "/session", session=token)
            except UnicodeEncodeError:  # o cliente nem consegue enviar o cabeçalho
                continue
            assert (response.code, error_code(response)) == (401, "session_expired")

    def test_cookie_enviado_e_ignorado(self):
        token = self.new_session()

        sem_cabecalho = self.api("GET", "/session", headers={"Cookie": f"painel_sessao={token}; X-EM-Session={token}"})
        response = self.api("GET", "/session", session=token, headers={"Cookie": "a=b"})

        assert sem_cabecalho.code == 401
        assert response.code == 200 and "Set-Cookie" not in response.headers

    def test_user_id_em_corpo_caminho_ou_cabecalho_e_ignorado(self):
        token = self.new_session("1001")

        extra = self.api("GET", "/session?user_id=2002", session=token, headers={"X-User-Id": "2002"})
        self.oauth.identities["c9"] = USER
        com_corpo = self.login("c9", user_id="2002", userId="2002")

        assert body_of(extra)["user"]["id"] == "1001"
        assert body_of(com_corpo)["user"]["id"] == "1001"  # o usuário vem do code, nunca do corpo

    def test_sessao_expirada_ociosa_ou_encerrada_responde_401(self):
        ttl = self.new_session("1001")
        self.advance(12 * 3600)
        assert self.api("GET", "/session", session=ttl).code == 401

        ociosa = self.new_session("1002")
        self.advance(61 * 60)
        assert self.api("GET", "/session", session=ociosa).code == 401

        encerrada = self.new_session("1003")
        assert self.api("DELETE", "/session", session=encerrada).code == 204
        assert self.api("GET", "/session", session=encerrada).code == 401

    def test_atividade_renova_a_ociosidade_mas_nao_o_ttl(self):
        token = self.new_session()
        for _ in range(14):  # 11 h 40 min de uso, sempre dentro da ociosidade
            self.advance(50 * 60)
            assert self.api("GET", "/session", session=token).code == 200
        self.advance(50 * 60)  # passou de 12 h desde o login
        assert self.api("GET", "/session", session=token).code == 401


class RotationTest(ApiTestCase):
    config_overrides = {
        "EXTERNAL_PANEL_API_SECRET_PREVIOUS": PREVIOUS_KEY,
        "EXTERNAL_PANEL_API_PREVIOUS_UNTIL": "2026-11-12",
    }

    def test_chave_anterior_vale_ate_a_data_e_a_sessao_dela_morre_com_ela(self):
        token = self.new_session(key=PREVIOUS_KEY)

        assert self.api("GET", "/session", key=PREVIOUS_KEY, session=token).code == 200
        assert (
            self.api("GET", "/session", key=SERVICE_KEY, session=token).code == 401
        )  # sessão presa à chave que a criou

        self.wall.advance(2 * 86400)  # a data venceu, sem reiniciar o bot: conferido a cada uso
        assert self.api("GET", "/meta", key=PREVIOUS_KEY).code == 401
        assert self.api("GET", "/session", key=PREVIOUS_KEY, session=token).code == 401

    def test_sessao_da_chave_nova_nao_vale_com_a_anterior(self):
        token = self.new_session(key=SERVICE_KEY)

        assert self.api("GET", "/session", key=PREVIOUS_KEY, session=token).code == 401


class IgnoredPreviousTest(ApiTestCase):
    config_overrides = {"EXTERNAL_PANEL_API_SECRET_PREVIOUS": PREVIOUS_KEY}  # sem data

    def test_chave_anterior_sem_data_e_ignorada(self):
        assert self.api("GET", "/meta", key=PREVIOUS_KEY).code == 401
        assert self.api("GET", "/meta").code == 200


class LogsTest(ApiTestCase):
    def test_segredos_nunca_aparecem_nos_logs(self):
        from tests.api.support import ACCESS_TOKEN, CODE, VERIFIER

        with self.assertLogs(level=logging.DEBUG) as capturado:
            logging.getLogger("teste").debug("início")
            token = self.new_session("1001")
            self.api("GET", "/session?code=" + CODE, session=token)
            self.api("GET", "/meta", key="chave-errada-" + "z" * 30)
            self.login("outro-codigo-qualquer")
            self.api("DELETE", "/session", session=token)
        texto = "\n".join(capturado.output)

        for segredo in (SERVICE_KEY, token, CODE, VERIFIER, ACCESS_TOKEN, "chave-errada-"):
            assert segredo not in texto
        assert "?" not in "".join(linha for linha in capturado.output if "GET /api/v1/session" in linha)

    def test_erro_inesperado_responde_envelope_sem_rastreio_e_loga_sem_segredos(self):
        from tests.api.support import CODE

        async def quebra(*args):
            raise RuntimeError("falha-interna-com-detalhe")

        self.oauth.exchange_code = quebra
        with self.assertLogs(level=logging.DEBUG) as capturado:
            response = self.api(
                "POST",
                "/sessions?code=SEGREDO-NA-QUERY&state=outro",
                body={"code": CODE, "code_verifier": "v" * 50},
            )
        corpo = response.body.decode()

        assert response.code == 500
        assert error_code(response) == "bot_unavailable"
        assert "Traceback" not in corpo and "falha-interna" not in corpo and "request_id" in corpo
        assert "Uncaught exception" in "\n".join(capturado.output)
        assert SERVICE_KEY not in "\n".join(capturado.output)
        assert "SEGREDO-NA-QUERY" not in "\n".join(capturado.output)  # a query nunca vai para o log de exceção


class QueryNeverLoggedTest(DataApiCase):
    def test_nome_e_alvo_das_remocoes_por_query_nunca_vao_para_nenhum_log(self):
        with self.assertLogs(level=logging.DEBUG) as captured:
            logging.getLogger("teste").debug("início")
            for path in (
                f"/account/favorites?name=NOME-SECRETO-X&expected_version={OLD_VERSION}",
                f"/account/favorites?name=NOME-SECRETO-X&expected_version={self.version_of('/account/favorites', 'favoritos')}",
                "/account/radio-favorites?kind=station&id=ALVO-SECRETO-Y",
                "/account/radio-favorites?kind=nada&id=ALVO-SECRETO-Y",
            ):
                self.call("DELETE", path)
            self.call("DELETE", "/account/favorites?name=NOME-SECRETO-X")  # 400: erro de validação
        text = "\n".join(captured.output)

        assert "NOME-SECRETO-X" not in text and "ALVO-SECRETO-Y" not in text
