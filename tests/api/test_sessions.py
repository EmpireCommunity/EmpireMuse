"""Sessões de usuário (contrato T-456 §3 e §14): login, revogação do token do Discord, limites, reautenticação [M1]."""

import json
from types import SimpleNamespace

import pytest

from empiremuse.panel.sessions import MAX_SESSIONS_PER_USER
from empiremuse.utils.user_ban import UserBanRecord, UserBans
from tests.api.support import ACCESS_TOKEN, CODE, USER, VERIFIER, ApiTestCase, body_of, error_code

pytestmark = pytest.mark.critical


class LoginTest(ApiTestCase):
    def test_login_devolve_token_usuario_servidores_filtrados_e_datas(self):
        response = self.login()
        corpo = body_of(response)

        assert response.code == 201
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert set(corpo) == {"session_token", "user", "guilds", "expires_at", "authenticated_at"}
        assert len(corpo["session_token"]) == 43  # 256 bits em base64 urlsafe
        assert corpo["user"] == USER
        assert [g["id"] for g in corpo["guilds"]] == ["1", "2"]  # o servidor sem gerência fica fora
        assert set(corpo["guilds"][0]) == {"id", "name", "icon", "owner", "permissions"}
        assert corpo["authenticated_at"] == "2026-11-11T12:00:00Z"
        assert corpo["expires_at"] == "2026-11-12T00:00:00Z"  # 12 h

    def test_o_store_guarda_so_o_sha256_do_token_e_o_get_da_sessao_nao_repete_o_token(self):
        token = body_of(self.login())["session_token"]

        assert all(token not in sid for sid in self.store._sessions)
        corpo = body_of(self.api("GET", "/session", session=token))
        assert "session_token" not in corpo and corpo["user"]["id"] == "1001"

    def test_token_do_discord_e_sempre_revogado_inclusive_em_erro(self):
        self.login()
        assert self.oauth.calls == ["exchange", "user", "guilds", "revoke"]

        for falha in ("user", "guilds"):
            self.oauth.calls.clear()
            self.oauth.identities["novo-" + falha] = USER
            self.oauth.fail_at = falha
            response = self.login("novo-" + falha)
            assert (response.code, error_code(response)) == (502, "discord_unavailable")
            assert self.oauth.calls[-1] == "revoke"

    def test_falha_ao_revogar_nao_derruba_o_login(self):
        self.oauth.fail_at = "revoke"
        assert self.login().code == 201

    def test_codigo_reaproveitado_ou_recusado_pelo_discord_e_validation_failed(self):
        assert self.login().code == 201
        repetido = self.login()

        assert (repetido.code, error_code(repetido)) == (400, "validation_failed")
        assert body_of(repetido)["error"]["fields"] == ["code"]

    def test_so_o_400_do_discord_e_codigo_recusado_o_401_do_client_oauth_e_discord_unavailable(self):
        self.oauth.fail_at = "exchange"
        for status, esperado in ((400, (400, "validation_failed")), (401, (502, "discord_unavailable"))):
            self.oauth.fail_status = status
            with self.assertLogs("empiremuse.api", level="WARNING") as logs:
                response = self.login()
            assert (response.code, error_code(response)) == esperado
            assert logs.records[0].levelname == ("WARNING" if status == 400 else "ERROR")

    def test_o_verifier_do_corpo_chega_ao_discord(self):
        resposta = self.api("POST", "/sessions", body={"code": CODE, "code_verifier": "w" * 50})

        assert (resposta.code, error_code(resposta)) == (400, "validation_failed")  # o FakeOAuth recusa outro verifier

    def test_discord_fora_do_ar_na_troca_e_discord_unavailable(self):
        self.oauth.fail_at, self.oauth.fail_status = "exchange", 503
        response = self.login()
        assert (response.code, error_code(response)) == (502, "discord_unavailable")
        assert "revoke" not in self.oauth.calls  # nada a revogar: não houve token

    BAD_BODIES = [
        {},
        {"code": ""},
        {"code": 123, "code_verifier": VERIFIER},
        {"code": CODE, "code_verifier": "curto"},
        {"code": CODE, "code_verifier": "é" * 50},
        {"code": CODE, "code_verifier": None},
        {"code": ["x"], "code_verifier": VERIFIER},
        {"code": "é" * 10, "code_verifier": VERIFIER},
        {"code": "c" * 513, "code_verifier": VERIFIER},
        {"code": CODE, "code_verifier": VERIFIER, "reauth": "sim"},
    ]

    def test_corpo_com_tipo_errado_e_validation_failed_sem_chamar_o_discord(self):
        for corpo in self.BAD_BODIES:
            response = self.api("POST", "/sessions", body=corpo)

            assert (response.code, error_code(response)) == (400, "validation_failed"), corpo
        assert self.oauth.calls == []

    def test_corpo_que_nao_e_json_objeto_ou_content_type_errado(self):
        assert self.api("POST", "/sessions", raw_body="{", headers={"Content-Type": "application/json"}).code == 400
        assert self.api("POST", "/sessions", raw_body="[1]", headers={"Content-Type": "application/json"}).code == 400
        assert (
            self.api("POST", "/sessions", raw_body="[" * 20000, headers={"Content-Type": "application/json"}).code
            == 400
        )
        assert self.api("POST", "/sessions", raw_body="a=b", headers={"Content-Type": "text/plain"}).code == 415
        assert self.api("POST", "/sessions", raw_body=json.dumps({"code": CODE})).code == 415  # sem Content-Type
        assert self.oauth.calls == []

    def test_sexta_sessao_do_usuario_derruba_a_mais_antiga(self):
        tokens = [self.new_session("1001") for _ in range(MAX_SESSIONS_PER_USER + 1)]

        assert self.api("GET", "/session", session=tokens[0]).code == 401
        assert all(self.api("GET", "/session", session=t).code == 200 for t in tokens[1:])

    def test_destroy_user_do_pool_derruba_a_sessao_da_api_com_o_painel_generico_desligado(self):
        token = self.new_session("1001")
        outro = self.new_session("1002")
        assert self.pool.panel_sessions is self.store  # o mesmo store que o apagamento de dados usa

        assert self.pool.panel_sessions.destroy_user("1001") == 1

        assert self.api("GET", "/session", session=token).code == 401
        assert self.api("GET", "/session", session=outro).code == 200


class ReauthTest(ApiTestCase):
    def reauth(self, session, code="codigo-reauth", user=USER):
        self.oauth.identities[code] = user
        return self.api(
            "POST", "/sessions", session=session, body={"code": code, "code_verifier": VERIFIER, "reauth": True}
        )

    def test_m1_reauth_do_mesmo_usuario_emite_token_novo_e_o_antigo_da_401(self):
        antigo = self.new_session("1001")
        self.advance(20 * 60)

        response = self.reauth(antigo)
        corpo = body_of(response)

        assert response.code == 201
        assert corpo["session_token"] != antigo
        assert corpo["authenticated_at"] == "2026-11-11T12:20:00Z"  # o login recente renova
        assert self.api("GET", "/session", session=antigo).code == 401
        assert self.api("GET", "/session", session=corpo["session_token"]).code == 200

    def test_m1_reauth_com_outro_usuario_da_403_e_nada_muda(self):
        antigo = self.new_session("1001")
        antes = dict(self.store._sessions)

        response = self.reauth(antigo, user={**USER, "id": "2002", "username": "mallory"})

        assert (response.code, error_code(response)) == (403, "reauth_user_mismatch")
        assert self.store._sessions == antes
        assert self.api("GET", "/session", session=antigo).code == 200

    def test_m1_reauth_sem_sessao_ou_com_sessao_invalida_e_recusado_sem_chamar_o_discord(self):
        self.oauth.identities["c1"] = USER
        sem = self.api("POST", "/sessions", body={"code": "c1", "code_verifier": VERIFIER, "reauth": True})
        invalida = self.reauth("x" * 43, code="c1")

        assert [(r.code, error_code(r)) for r in (sem, invalida)] == [(401, "session_expired")] * 2
        assert self.oauth.calls == []

    def test_token_adulterado_nao_serve_de_ancora_para_reauth(self):
        token = self.new_session("1001")
        self.oauth.identities["c2"] = USER
        response = self.api(
            "POST",
            "/sessions",
            key=self.settings.service_key,
            session=token[:-1] + ("a" if token[-1] != "a" else "b"),
            body={"code": "c2", "code_verifier": VERIFIER, "reauth": True},
        )
        assert response.code == 401


class ReauthRaceTest(ApiTestCase):
    """A sessão de origem pode morrer enquanto o Discord responde: a reautenticação confere de novo antes de gravar."""

    def reauth_body(self, code):
        self.oauth.identities[code] = USER
        return {"code": code, "code_verifier": VERIFIER, "reauth": True}

    def test_logout_durante_a_troca_nao_ressuscita_a_sessao(self):
        token = self.new_session("1001")
        self.oauth.before_exchange = lambda: self.store.destroy_api(token)  # DELETE /session no meio da troca

        response = self.api("POST", "/sessions", session=token, body=self.reauth_body("r1"))

        assert (response.code, error_code(response)) == (401, "session_expired")
        assert self.store._sessions == {}
        assert self.oauth.calls[-1] == "revoke"  # o token do Discord é revogado mesmo assim

    def test_apagamento_de_dados_durante_a_troca_nao_ressuscita_a_sessao(self):
        token = self.new_session("1001")
        self.oauth.before_exchange = lambda: self.pool.panel_sessions.destroy_user("1001")  # destroy_user do apagamento

        response = self.api("POST", "/sessions", session=token, body=self.reauth_body("r2"))

        assert (response.code, error_code(response)) == (401, "session_expired")
        assert self.store._sessions == {}

    def test_duas_reautenticacoes_concorrentes_so_uma_vence(self):
        from tornado.httpclient import AsyncHTTPClient

        token = self.new_session("1001")
        self.oauth.calls.clear()
        self.oauth.hold_until = 2  # as duas passam pela autenticação antes de qualquer uma terminar
        headers = {
            "Authorization": f"Bearer {self.settings.service_key}",
            "X-EM-Session": token,
            "Content-Type": "application/json",
        }

        async def reauth(code):
            return await AsyncHTTPClient().fetch(
                self.get_url("/api/v1/sessions"),
                method="POST",
                headers=headers,
                body=json.dumps(self.reauth_body(code)),
                raise_error=False,
            )

        async def both():
            import asyncio

            return await asyncio.gather(reauth("ra"), reauth("rb"))

        respostas = self.io_loop.run_sync(both)

        assert sorted(r.code for r in respostas) == [201, 401]
        assert len(self.store._sessions) == 1  # um só token novo; o antigo e o do perdedor não existem
        assert (
            self.oauth.calls.count("exchange") == 2 and self.oauth.calls.count("revoke") == 2
        )  # nenhum token do Discord sobra


def lista(*banidos, carregada=True):
    bans = UserBans(SimpleNamespace(), owners=frozenset({999}))
    bans._records = {uid: UserBanRecord(uid, 1, "motivo", 999) for uid in banidos}
    bans._loaded = carregada
    return bans


class BannedUserTest(ApiTestCase):
    def test_banido_nao_cria_sessao_e_o_login_nao_revela_o_motivo(self):
        self.pool.user_bans = lista(1001)

        response = self.login()

        assert (response.code, error_code(response)) == (403, "forbidden")
        assert "motivo" not in response.body.decode()
        assert self.store._sessions == {}
        assert self.oauth.calls[-1] == "revoke"

    def test_banido_depois_do_login_perde_todas_as_sessoes_na_proxima_requisicao(self):
        token = self.new_session("1001")
        self.pool.user_bans = lista(1001)

        assert self.api("GET", "/session", session=token).code == 401
        assert self.store._sessions == {}

    def test_lista_de_banidos_nao_carregada_fecha_o_login_com_503(self):
        self.pool.user_bans = lista(carregada=False)

        response = self.login()

        assert (response.code, error_code(response)) == (503, "bot_unavailable")
        assert self.oauth.calls == []


class SharedStoreTest(ApiTestCase):
    def test_painel_generico_e_api_usam_o_mesmo_store_e_o_mesmo_runner(self):
        from empiremuse.panel import build_panel_app
        from empiremuse.panel.settings import load_panel_settings
        from tests.panel.helpers import local_config

        generico = build_panel_app(self.pool, load_panel_settings(local_config()), clock=self.clock)

        assert generico.settings["session_store"] is self.store
        assert generico.settings["task_runner"] is self.pool.panel_tasks

    def test_painel_generico_e_api_usam_os_mesmos_servicos_de_formulario_e_portanto_os_mesmos_locks(self):
        from empiremuse.panel import build_panel_app
        from empiremuse.panel.settings import load_panel_settings
        from tests.panel.helpers import local_config

        generico = build_panel_app(self.pool, load_panel_settings(local_config()), clock=self.clock)

        assert generico.settings["settings_service"] is self.ctx.settings_service is self.pool.panel_forms
        assert generico.settings["account_service"] is self.ctx.account_service is self.pool.panel_accounts
        assert self.ctx.settings_service.lock("1", 100) is generico.settings["settings_service"].lock("1", 100)


class StoreSeparationTest(ApiTestCase):
    def test_sessao_da_api_nunca_sai_pelo_leitor_do_cookie_do_painel_generico(self):
        import hashlib

        token = self.new_session("1001")
        hashed = hashlib.sha256(token.encode()).hexdigest()

        assert self.store.get(token) is None
        assert self.store.get(hashed) is None  # nem pelo id que o store guarda
        assert self.api("GET", "/session", session=token).code == 200

    def test_sessao_do_painel_generico_nao_serve_de_token_da_api(self):
        cookie_id = self.store.create(SimpleNamespace(id="1001", username="u", global_name=None, avatar=None), ())

        assert self.api("GET", "/session", session=cookie_id).code == 401
