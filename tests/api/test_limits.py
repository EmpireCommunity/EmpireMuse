"""Limites (contrato T-456 §7 m7 e N2): por usuário, por chave, criação de sessões e falhas de chave por par TCP."""

import pytest

from tests.api.support import ApiTestCase, error_code

pytestmark = pytest.mark.critical


class UserLimitsTest(ApiTestCase):
    def test_leitura_120_por_minuto_por_user_id_e_5_sessoes_nao_somam_500(self):
        tokens = [self.new_session("1001") for _ in range(5)]
        outro = self.new_session("1002")
        self.advance(1)

        respostas = [self.api("GET", "/session", session=tokens[i % 5]).code for i in range(121)]

        assert respostas[:120] == [200] * 120
        assert respostas[120] == 429  # 5 sessões do mesmo usuário compartilham o balde de 120
        assert self.api("GET", "/session", session=outro).code == 200  # outro usuário não é afetado

    def test_resposta_429_traz_retry_after_e_envelope(self):
        token = self.new_session("1001")
        for _ in range(120):
            self.api("GET", "/session", session=token)

        response = self.api("GET", "/session", session=token)

        assert (response.code, error_code(response)) == (429, "rate_limited")
        assert 1 <= int(response.headers["Retry-After"]) <= 60
        self.advance(61)
        assert self.api("GET", "/session", session=token).code == 200

    def test_escrita_20_por_minuto_por_usuario_separada_da_leitura(self):
        token = self.new_session("1001")
        self.oauth.identities.clear()

        # DELETE conta como escrita; sem sessão a 2ª em diante já seria 401, então reabre a cada volta
        codigos = []
        for _ in range(21):
            token = self.new_session("1001") if codigos else token
            codigos.append(self.api("DELETE", "/session", session=token).code)
            self.oauth.identities.clear()

        assert codigos[:20] == [204] * 20 and codigos[20] == 429


class KeyLimitsTest(ApiTestCase):
    def test_balde_da_chave_600_por_minuto(self):
        respostas = [self.api("GET", "/meta").code for _ in range(601)]

        assert respostas[:600] == [200] * 600 and respostas[600] == 429

    def test_criacao_de_sessoes_120_por_minuto_por_chave(self):
        respostas = []
        for i in range(121):
            self.oauth.identities[f"c{i}"] = {"id": str(5000 + i), "username": "u"}
            respostas.append(self.login(f"c{i}").code)

        assert respostas[:120] == [201] * 120 and respostas[120] == 429

    def test_n2_31_falhas_do_mesmo_par_dao_429_e_a_chave_certa_do_par_sempre_passa(self):
        falhas = [self.api("GET", "/meta", key="errada").code for _ in range(31)]

        assert falhas[:30] == [401] * 30 and falhas[30] == 429
        assert self.api("GET", "/meta").code == 200  # a chave certa nunca leva 429 por falha alheia
        assert self.api("GET", "/meta", key="errada").code == 429  # só a errada, até a janela andar
        self.advance(61)
        assert self.api("GET", "/meta", key="errada").code == 401

    def test_n2_o_aviso_sai_uma_vez_por_par_e_janela_e_as_falhas_normais_nao_avisam(self):
        with self.assertLogs("empiremuse.api", level="DEBUG") as capturado:
            for _ in range(40):
                self.api("GET", "/meta", key="errada")
        avisos = [r for r in capturado.records if r.levelname == "WARNING"]

        assert len(avisos) == 1
        assert not [r for r in capturado.records if r.levelname not in ("WARNING", "DEBUG")]
        self.advance(61)
        with self.assertLogs("empiremuse.api", level="WARNING") as capturado:
            for _ in range(31):
                self.api("GET", "/meta", key="errada")
        assert len(capturado.records) == 1  # nova janela, novo aviso

    def test_o_par_vem_do_socket_e_nunca_de_x_forwarded_for(self):
        codigos = []
        for i in range(31):
            cabecalhos = {
                "X-Forwarded-For": f"10.0.0.{i}",
                "X-Real-IP": f"10.0.1.{i}",
                "CF-Connecting-IP": f"10.0.2.{i}",
            }
            codigos.append(self.api("GET", "/meta", key="errada", headers=cabecalhos).code)

        assert codigos[30] == 429  # todos contaram para o mesmo par TCP

    def test_balde_do_usuario_vem_antes_do_da_chave_requisicao_negada_nao_gasta_a_chave(self):
        alice = self.new_session("1001")
        bruno = self.new_session("1002")
        # 120 leituras passam e 480 são negadas pelo balde do usuário; se as negadas gastassem a chave (600/min), ela esgotaria
        for _ in range(600):
            self.api("GET", "/session", session=alice)

        assert self.api("GET", "/session", session=bruno).code == 200

    def test_n2_401_de_sessao_nao_consome_o_balde_de_falhas_de_chave(self):
        for _ in range(100):  # reinício do bot seguido de uma rajada de session_expired
            assert self.api("GET", "/session", session="x" * 43).code == 401

        assert self.api("GET", "/meta").code == 200  # o painel segue sem 429
        assert self.api("GET", "/meta", key="errada").code == 401  # e ainda nem contou uma falha de chave
