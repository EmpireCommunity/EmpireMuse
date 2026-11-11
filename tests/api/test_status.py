"""``GET /api/v1/status`` (B8, contrato §4 r8.3 e §14 item 10): o corpo é EXATAMENTE o contratado, sem dado de bot com falha."""

import json
from types import SimpleNamespace

from tests.api.support import ApiTestCase, body_of, error_code

COMMIT = "0123456789abcdef0123456789abcdef01234567"
SECRET_ERROR = "TOKEN-SECRETO-NO-ERRO"


def bot(name="Muse#0001", guilds=3, ready=True, *, identifier="main"):
    return SimpleNamespace(
        is_ready=lambda: ready,
        user=name if ready else None,
        guilds=list(range(guilds)),
        identifier=identifier,
        application_id=111111111111111111,
        config={"INVITE_PERMISSIONS": 8},
    )


class StatusTest(ApiTestCase):
    bots: list = []
    killing_state: object = False
    failed_bots: dict = {}
    commit: object = COMMIT

    def make_pool(self):
        return SimpleNamespace(
            bots=list(self.bots or [bot()]),
            database=None,
            killing_state=self.killing_state,
            failed_bots=dict(self.failed_bots),
            commit=self.commit,
        )

    def status(self, **kwargs):
        response = self.api("GET", "/status", **kwargs)
        return response, body_of(response) if response.body else None


class StatusBodyTest(StatusTest):
    def test_corpo_exato_com_um_bot_pronto(self):
        response, body = self.status()

        assert response.code == 200
        assert body == {
            "state": "ok",
            "bots": {"ready": [{"name": "Muse#0001", "guilds": 3}], "pending": 0, "failed": 0},
            "bot_version": "9.9.9",
            "commit": COMMIT[:12],
        }
        assert set(body) == {"state", "bots", "bot_version", "commit"}
        assert set(body["bots"]) == {"ready", "pending", "failed"}
        assert set(body["bots"]["ready"][0]) == {"name", "guilds"}

    def test_cabecalhos_no_store_e_sem_cookie(self):
        response, _ = self.status()

        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert "Set-Cookie" not in response.headers


class StatusSecretsTest(StatusTest):
    bots = [bot("Muse#0001", 2), bot("Muse2#0002", 1, ready=False, identifier="RESERVE_SEGREDO")]
    failed_bots = {"RESERVE_FALHA_SEGREDO": SECRET_ERROR}

    def test_nada_de_bot_pendente_ou_com_falha_vaza_so_a_contagem(self):
        response, body = self.status()
        text = response.body.decode()

        assert body["state"] == "degraded"
        assert body["bots"] == {"ready": [{"name": "Muse#0001", "guilds": 2}], "pending": 1, "failed": 1}
        for forbidden in (
            SECRET_ERROR,
            "RESERVE_FALHA_SEGREDO",
            "RESERVE_SEGREDO",
            "avatar",
            "convite",
            "invite",
            "111111111111111111",
            "rpc",
            "mensagem",
        ):
            assert forbidden.lower() not in text.lower(), forbidden


class StatusRestartingTest(StatusTest):
    bots = [bot("Muse#0001", 2), bot("Muse2#0002", 1, ready=False)]
    killing_state = True
    failed_bots = {"X": "erro"}

    def test_killing_state_true_da_restarting_com_ready_vazio(self):
        response, body = self.status()

        assert response.code == 200
        assert body["state"] == "restarting"
        assert body["bots"] == {"ready": [], "pending": 0, "failed": 1}


class StatusRatelimitStateTest(StatusTest):
    killing_state = "ratelimit"

    def test_killing_state_texto_nao_e_restarting_como_na_pagina_de_status(self):
        _, body = self.status()

        assert body["state"] == "ok"


class StatusFailedOnlyTest(StatusTest):
    failed_bots = {"RESERVE": "erro"}

    def test_um_bot_com_falha_degrada_mesmo_com_todos_os_outros_prontos(self):
        _, body = self.status()

        assert body["state"] == "degraded"
        assert body["bots"] == {"ready": [{"name": "Muse#0001", "guilds": 3}], "pending": 0, "failed": 1}


class StatusDegradedTest(StatusTest):
    bots = [bot("Muse#0001", 1, ready=False)]

    def test_nenhum_pronto_e_degraded(self):
        _, body = self.status()

        assert body["state"] == "degraded"
        assert body["bots"] == {"ready": [], "pending": 1, "failed": 0}


class StatusOnlyFailedTest(StatusTest):
    bots = [None]  # truthy: força o pool com a lista abaixo
    failed_bots = {"Z": "erro"}

    def make_pool(self):
        pool = super().make_pool()
        pool.bots = []
        return pool

    def test_so_falhas_ja_e_resposta_e_nao_503(self):
        response, body = self.status()

        assert response.code == 200
        assert (body["state"], body["bots"]["failed"], body["bots"]["ready"]) == ("degraded", 1, [])


class StatusNothingStartedTest(StatusTest):
    def make_pool(self):
        pool = super().make_pool()
        pool.bots = []
        return pool

    def test_nada_subiu_e_503_bot_unavailable(self):
        response, _ = self.status()

        assert (response.code, error_code(response)) == (503, "bot_unavailable")


class StatusNameTest(StatusTest):
    bots = [bot("N" * 200, 5)]

    def test_name_e_aparado_em_80_caracteres(self):
        _, body = self.status()

        assert body["bots"]["ready"][0]["name"] == "N" * 80


class StatusCommitTest(StatusTest):
    def test_commit_que_nao_e_hexadecimal_vira_null(self):
        for commit in (None, "", "não é hash", "XYZ", "abc", "0123456789abcdef; rm -rf", "A" * 40, 123):
            self.pool.commit = commit

            _, body = self.status()

            assert body["commit"] is None, repr(commit)

    def test_commit_curto_valido_passa_inteiro(self):
        self.pool.commit = "abcdef0"

        _, body = self.status()

        assert body["commit"] == "abcdef0"


class StatusAuthTest(StatusTest):
    def test_sem_chave_ou_com_chave_errada_e_401_forbidden(self):
        for key in (None, "", "errada"):
            response = self.api("GET", "/status", key=key)
            assert (response.code, error_code(response)) == (401, "forbidden")

    def test_nao_exige_sessao_e_ignora_cookie_e_user_id(self):
        response = self.api("GET", "/status?user_id=2002", headers={"Cookie": "a=b", "X-User-Id": "2002"})

        assert response.code == 200
        assert "Set-Cookie" not in response.headers

    def test_token_de_sessao_enviado_nao_muda_o_corpo(self):
        token = self.new_session()

        with_session = self.api("GET", "/status", session=token)
        without = self.api("GET", "/status")

        assert json.loads(with_session.body) == json.loads(without.body)

    def test_so_leitura_outros_metodos_nao_existem(self):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            assert self.api(method, "/status").code in (404, 405), method

    def test_gasta_o_balde_de_leitura_da_chave(self):
        for _ in range(600):
            assert self.api("GET", "/status").code == 200

        response = self.api("GET", "/status")

        assert (response.code, error_code(response)) == (429, "rate_limited")
