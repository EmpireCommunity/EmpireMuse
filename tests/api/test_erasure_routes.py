"""B5 · apagar dados pela API (contrato T-456 §3.5, §4 "Apagar dados", §11.1, §14 "Apagar"): login recente pela ``created_at``,
``confirm_token`` de 60 s e de uso único preso a usuário e sessão, palavra digitada, ``OK``/``PARTIAL``/``BUSY``, teto por hora e
todas as sessões mortas depois do ``OK``. O serviço real (E1) roda sobre bancos TinyMongo; os desfechos raros usam um dublê."""

import copy
import json
import shutil
import tempfile
from types import SimpleNamespace

import pytest

from empiremuse.api.handlers_erasure import session_binding
from empiremuse.api.signing import PURPOSE_JUKEBOX, derive_key
from empiremuse.panel.services.tokens import SignedTokens
from empiremuse.rules.erasure import ERASE_GLOBAL_PER_HOUR, ERASE_MAX_SESSION_AGE_S, ErasureReport, ErasureStatus
from empiremuse.utils.db import LocalDatabase, global_db_models
from empiremuse.utils.erasure import ErasureBusy, ErasureRateLimited, ErasureService, ErasureTombstones
from empiremuse.utils.user_locks import UserLocks
from tests.api.helpers import SERVICE_KEY, SIGNING_SECRET
from tests.api.support import VERIFIER, body_of, error_code
from tests.api.support_data import OTHER, USER, DataApiCase
from tests.panel.helpers_settings import UID

pytestmark = pytest.mark.critical

PREPARE, ERASE = "/account/erasure/prepare", "/account/erasure"
WORD = "APAGAR"
WORDS = ("APAGAR", "DELETE")
TOKEN_TTL_S = 60
MINUTE = 60
OTHER_UID = int(OTHER)


async def _no_wait(_seconds):
    return None


class ErasureCase(DataApiCase):
    """A API de teste com o serviço de apagamento REAL (``pool.erasure``) sobre dois bancos locais em pasta temporária."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="erase-api-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        pool = self.pool
        pool.database, pool.local_database = LocalDatabase(f"{self.tmp}/principal"), LocalDatabase(f"{self.tmp}/local")
        pool.user_locks, pool.erasure_tombstones = UserLocks(), ErasureTombstones(clock=self.clock)
        pool.lastfm_sessions, pool.user_prefix_cache = {}, {}
        self.service = pool.erasure = ErasureService(
            pool, clock=self.clock, sleep=_no_wait, rpc_closer=lambda _u: False
        )
        for uid in (UID, OTHER_UID):
            self.io_loop.run_sync(lambda uid=uid: self.seed_user(uid))

    async def seed_user(self, uid):
        doc = copy.deepcopy(global_db_models["users"])
        doc.update(custom_prefix="x", fav_links={"Rock": "https://example.com/rock"})
        await self.pool.database.update_data(uid, doc, db_name="users", collection="global")

    def stored(self, uid):
        found = self.io_loop.run_sync(lambda: self.pool.database.find_doc(uid, db_name="users", collection="global"))
        return found is not None

    def use_stub(self, outcome):
        """Troca ``pool.erasure`` por um dublê; ``calls`` guarda (usuário, fonte, lock do usuário preso?)."""
        calls, locks = [], self.pool.user_locks

        async def erase_user(user_id, *, source):
            calls.append((user_id, source, locks.lock(user_id).locked()))
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        self.pool.erasure = SimpleNamespace(erase_user=erase_user)
        return calls

    def prepare(self, session=None):
        response = self.call("POST", PREPARE, {}, session=session)
        assert response.code == 200, response.body
        return body_of(response)["confirm_token"]

    def erase(self, token, typed=WORD, *, session=None, **extra):
        return self.call("POST", ERASE, {"confirm_token": token, "typed": typed, **extra}, session=session)

    def alive(self, session):
        return self.call("GET", "/account", session=session).code == 200


class PrepareTest(ErasureCase):
    def test_emite_o_token_de_60_s_com_resumo_so_de_codigos_e_nada_e_apagado(self):
        response = self.call("POST", PREPARE, {})

        body = body_of(response)
        assert response.code == 200 and response.headers["Cache-Control"] == "no-store"
        assert body["expires_in"] == TOKEN_TTL_S
        assert "confirmation_word" not in body["summary"]  # a tela escolhe a palavra pelo idioma da página
        assert set(body) == {"confirm_token", "expires_in", "summary"}
        assert self.stored(UID) and self.alive(self.token)

    def test_sem_sessao_ou_sem_servico_nada_e_emitido(self):
        assert self.api("POST", PREPARE, body={}).code == 401
        del self.pool.erasure
        response = self.call("POST", PREPARE, {})

        assert (response.code, error_code(response)) == (503, "bot_unavailable")


class RecentLoginTest(ErasureCase):
    """§3.5 r4-E1: a idade é ``now - created_at`` (a sessão da API nunca tem ``consented_at``): 29 min passa, 31 recusa."""

    def test_login_de_29_min_sem_consented_at_apaga(self):
        self.advance(ERASE_MAX_SESSION_AGE_S - 30)  # 29 min 30 s
        token = self.prepare()
        assert self.session_consent_is_missing()

        self.advance(20)  # 29 min 50 s: ainda passa
        calls = self.use_stub(ErasureReport(ErasureStatus.OK, {}, ()))
        assert self.erase(token).code == 200 and len(calls) == 1

    def test_o_login_que_envelhece_entre_o_prepare_e_a_execucao_e_recusado_e_nada_e_apagado(self):
        self.advance(ERASE_MAX_SESSION_AGE_S - 10)
        token = self.prepare()
        self.advance(20)  # o token (60 s) ainda vale, mas o login passou de 30 min

        response = self.erase(token)

        assert (response.code, error_code(response)) == (401, "reauth_required")
        assert self.stored(UID) and self.alive(self.token)

    def test_prepare_com_login_de_31_min_e_recusado(self):
        self.advance(31 * MINUTE)

        response = self.call("POST", PREPARE, {})

        assert (response.code, error_code(response)) == (401, "reauth_required")

    def test_a_ordem_e_a_mesma_do_prepare_o_login_antigo_vem_antes_do_corpo(self):
        self.advance(31 * MINUTE)

        response = self.call("POST", ERASE, {"confirm_token": 7, "typed": ""})  # corpo inválido E login velho

        assert (response.code, error_code(response)) == (401, "reauth_required")

    def test_reautenticar_renova_a_idade_e_mata_o_token_antigo(self):
        old_confirm = self.prepare()  # um confirm_token da sessão antiga (login ainda recente)
        self.advance(31 * MINUTE)
        self.oauth.identities["novo"] = {"id": USER, "username": "alice"}
        reauth = self.api(
            "POST", "/sessions", session=self.token, body={"code": "novo", "code_verifier": VERIFIER, "reauth": True}
        )
        assert reauth.code == 201
        fresh = body_of(reauth)["session_token"]

        assert not self.alive(self.token)  # o token antigo morreu
        refused = self.erase(
            old_confirm, session=fresh
        )  # o confirm_token da sessão antiga não vale na nova (preso à sessão)
        assert (refused.code, error_code(refused)) == (400, "validation_failed")
        token = self.prepare(session=fresh)
        assert self.erase(token, session=fresh).code == 200  # com o login novo, apaga

    def session_consent_is_missing(self):
        sessions = [s for s in self.store._sessions.values() if s.user.id == USER]
        return bool(sessions) and all(s.consented_at is None for s in sessions)


class ConfirmTokenTest(ErasureCase):
    def test_expirado_recusado_sem_apagar_e_um_prepare_novo_funciona(self):
        calls = self.use_stub(ErasureReport(ErasureStatus.OK, {}, ()))
        token = self.prepare()
        self.advance(TOKEN_TTL_S + 1)

        response = self.erase(token)

        assert (response.code, error_code(response), body_of(response)["error"]["fields"]) == (
            400,
            "validation_failed",
            ["confirm_token"],
        )
        assert calls == [] and self.erase(self.prepare()).code == 200

    def test_de_uso_unico_o_token_gasto_pela_execucao_nao_vale_de_novo(self):
        calls = self.use_stub(ErasureBusy())  # a sessão segue viva: o token foi gasto mesmo assim
        token = self.prepare()
        assert body_of(self.erase(token)) == {"status": "BUSY"}

        again = self.erase(token)

        assert (again.code, error_code(again)) == (400, "validation_failed") and len(calls) == 1

    def test_token_de_outra_sessao_ou_de_outro_usuario_e_recusado(self):
        calls = self.use_stub(ErasureReport(ErasureStatus.OK, {}, ()))
        for owner in (USER, OTHER):  # outra sessão do MESMO usuário e sessão de OUTRO usuário
            other = self.new_session(owner)
            foreign = self.prepare(session=other)

            response = self.erase(foreign)

            assert (response.code, error_code(response)) == (400, "validation_failed"), owner
            assert self.alive(self.token) and self.alive(other)
        assert calls == []

    def test_token_de_outra_finalidade_ou_inventado_e_recusado_sem_erro_de_tipo(self):
        calls = self.use_stub(ErasureReport(ErasureStatus.OK, {}, ()))
        radio = self.ctx.erase_tokens.issue(USER, 0, "radio_reset", "x")  # mesma chave, outra finalidade
        for bad in (radio, "lixo", "é" * 20, "a" * 4000, 5, None, ["x"]):
            response = self.erase(bad)
            assert (response.code, error_code(response)) == (400, "validation_failed"), bad
        assert calls == []

    def test_o_token_so_vale_com_a_subchave_do_apagamento(self):
        token = self.prepare()
        for foreign in (
            SERVICE_KEY,
            SIGNING_SECRET,
            derive_key(SIGNING_SECRET, PURPOSE_JUKEBOX),
        ):  # nem a chave de serviço, nem a mestra
            assert SignedTokens(foreign).check(token, USER, 0, "erasure", session_binding(self.token)) == "invalido"
        assert self.ctx.erase_tokens.check(token, USER, 0, "erasure", session_binding(self.token)) is None

    def test_a_palavra_errada_recusa_sem_gastar_o_token(self):
        calls = self.use_stub(ErasureReport(ErasureStatus.OK, {}, ()))
        token = self.prepare()
        # só `APAGAR` e `DELETE`, exatas: nada de caixa, espaço, quebra de linha, parecido nem as duas juntas
        wrong = (
            "apagar",
            "APAGAR ",
            " APAGAR",
            "APAGA",
            "delete",
            "Delete",
            " DELETE",
            "DELETE ",
            "DELETE\n",
            "DELETAR",
            "APAGAR DELETE",
            "APAGAR\u200b",
            "",
            None,
            7,
        )
        for typed in wrong:
            response = self.erase(token, typed)
            assert (response.code, error_code(response), body_of(response)["error"]["fields"]) == (
                400,
                "validation_failed",
                ["typed"],
            ), typed
        assert calls == []

        assert self.erase(token).code == 200 and len(calls) == 1  # o mesmo token ainda vale com a palavra certa

    def test_apagar_e_delete_apagam_cada_uma_com_login_e_token_novos(self):
        for word in WORDS:
            with self.subTest(word=word):
                calls = self.use_stub(ErasureReport(ErasureStatus.OK, {}, ()))
                self.token = self.new_session(USER)  # cada apagamento derruba as sessões do usuário
                token = self.prepare()

                response = self.erase(token, word)

                assert response.code == 200 and len(calls) == 1, word

    def test_a_palavra_e_conferida_depois_do_token(self):
        calls = self.use_stub(ErasureReport(ErasureStatus.OK, {}, ()))

        response = self.erase(
            "token-invalido", "DELETAR"
        )  # token inválido E palavra errada: o campo citado é o do token

        assert (response.code, body_of(response)["error"]["fields"]) == (400, ["confirm_token"])
        assert calls == []


class OutcomeTest(ErasureCase):
    def test_ok_apaga_so_o_da_sessao_ignora_user_id_do_corpo_e_derruba_todas_as_sessoes_inclusive_a_do_pedido(self):
        second, other = self.new_session(USER), self.new_session(OTHER)
        token = self.prepare()
        self.call(
            "PATCH",
            "/account/prefix",
            {"expected_version": self.version_of("/account", "prefixo"), "values": {"custom_prefix": "?!"}},
            key="k-guardada",
        )
        assert self.ctx.idempotency._user_keys.get(USER)  # há resposta guardada do usuário

        response = self.erase(token, user_id=OTHER)

        assert (response.code, body_of(response)) == (200, {"status": "OK"})
        assert not self.stored(UID) and self.stored(OTHER_UID)  # o alvo é a sessão, nunca o corpo
        assert not self.alive(self.token) and not self.alive(second)  # inclusive a do pedido
        assert self.alive(other)
        assert not self.ctx.idempotency._user_keys.get(USER)  # as respostas guardadas do usuário foram descartadas

    def test_chama_o_mesmo_servico_do_slash_com_a_fonte_do_painel_e_sem_segurar_o_lock_do_usuario(self):
        calls = self.use_stub(ErasureReport(ErasureStatus.OK, {}, ()))

        assert self.erase(self.prepare()).code == 200

        assert calls == [(UID, "panel", False)]  # ``erase_user`` pega o lock sozinho: quem chama não o segura

    def test_partial_e_reportado_sem_contagens_nem_nomes(self):
        report = ErasureReport(ErasureStatus.PARTIAL, {"users": 1}, ("radios",))
        self.use_stub(report)

        response = self.erase(self.prepare())

        assert (response.code, body_of(response)) == (200, {"status": "PARTIAL"})
        assert "radios" not in response.body.decode() and "users" not in response.body.decode()

    def test_partial_do_servico_real_tambem_derruba_a_sessao(self):
        def closer_that_fails(_user_id):
            raise RuntimeError(
                "falha ao fechar o app de RPC"
            )  # a limpeza da memória fica parcial: o resultado é PARTIAL

        self.service._rpc_closer = closer_that_fails
        token = self.prepare()

        response = self.erase(token)

        assert (response.code, body_of(response)) == (200, {"status": "PARTIAL"})
        assert not self.alive(self.token)  # sem depender de dublê: o serviço real destruiu a sessão

    def test_o_teto_global_conta_execucoes_ok_e_partial(self):
        def closer_that_fails(_user_id):
            raise RuntimeError("parcial")

        self.service._rpc_closer = closer_that_fails
        for number in range(ERASE_GLOBAL_PER_HOUR):
            report = self.io_loop.run_sync(lambda n=number: self.service.erase_user(9_000 + n, source="panel"))
            assert report.status is ErasureStatus.PARTIAL

        response = self.erase(self.prepare())  # PARTIAL também gastou o teto de 60 execuções por hora

        assert (response.code, error_code(response)) == (429, "rate_limited")
        assert self.stored(UID)

    def test_busy_nada_e_apagado_a_sessao_segue_e_o_lock_do_usuario_nao_e_do_handler(self):
        calls = self.use_stub(ErasureBusy())

        response = self.erase(self.prepare())

        assert (response.code, body_of(response)) == (200, {"status": "BUSY"})
        assert calls == [(UID, "panel", False)] and self.stored(UID) and self.alive(self.token)

    def test_o_limite_por_usuario_e_o_teto_global_respondem_429_com_retry_after(self):
        self.use_stub(ErasureRateLimited(120.2))

        response = self.erase(self.prepare())

        assert (response.code, error_code(response), response.headers["Retry-After"]) == (429, "rate_limited", "121")
        assert self.stored(UID) and self.alive(self.token)

    def test_teto_global_de_60_por_hora_do_servico_real(self):
        for number in range(ERASE_GLOBAL_PER_HOUR):
            report = self.io_loop.run_sync(lambda n=number: self.service.erase_user(9_000 + n, source="panel"))
            assert report.status is ErasureStatus.OK

        response = self.erase(self.prepare())

        assert (response.code, error_code(response)) == (429, "rate_limited")
        assert int(response.headers["Retry-After"]) > 0 and self.stored(UID)

    def test_o_confirm_token_nunca_vai_para_o_log(self):
        self.use_stub(ErasureReport(ErasureStatus.OK, {}, ()))
        token = self.prepare()
        with self.assertLogs(level="DEBUG") as captured:
            self.erase(token)
            self.erase(token)  # o segundo pedido (recusado) também passa pelo log de acesso
        assert token not in "\n".join(captured.output)

    def test_sem_servico_de_apagamento_nada_e_apagado_e_a_resposta_e_503(self):
        token = self.prepare()
        del self.pool.erasure

        response = self.erase(token)

        assert (response.code, error_code(response)) == (503, "bot_unavailable") and self.stored(UID)

    def test_corpo_que_nao_e_json_objeto_e_content_type_errado(self):
        assert self.call("POST", ERASE, None, raw_body="[1]", headers={"Content-Type": "application/json"}).code == 400
        assert self.call("POST", ERASE, None, raw_body="x").code == 415
        assert json.loads(self.call("POST", ERASE, {}).body)["error"]["fields"] == ["confirm_token", "typed"]
