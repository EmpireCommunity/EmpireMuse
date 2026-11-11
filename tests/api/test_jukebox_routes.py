"""B4 · Jukebox (contrato T-456 §4 "Jukebox" e m4; ameaças T-458 m4b/m4a): a tarefa é do dono **e** do servidor, a confirmação
de dois passos usa token assinado de uso único, a escrita reautoriza na hora e nada de segredo ou de texto sai."""

import asyncio
from unittest.mock import patch

import pytest

from tests.api.support import body_of, error_code
from tests.api.support_jukebox import BASE, OTHER_USER, JukeboxApiCase, dump
from tests.panel.helpers_jukebox import GID

pytestmark = pytest.mark.critical

CHANNEL_ID = "22"  # #jukebox
SETUP_EXISTING = {"modo": "existente", "canal": CHANNEL_ID}
SETUP_NEW_CHANNEL = {"modo": "criar", "tipo": "texto"}


def configure_body(version, values=None, **extra):
    return {"expected_version": version, "action": "configure", "values": values or SETUP_EXISTING, **extra}


def reset_body(version, **extra):
    return {"expected_version": version, "action": "reset", **extra}


class JukeboxReadTest(JukeboxApiCase):
    def test_estado_traz_canal_opcoes_e_escolhas_sem_segredo(self):
        self.world.configured_text()

        response = self.call("GET", BASE)

        body = body_of(response)
        assert response.code == 200 and body["configured"] is True and body["channel"]["id"] == CHANNEL_ID
        assert body["options"]["key"] == "opcoes" and len(body["version"]) == 16 and body["task"] is None
        assert {"texto", "voz"} <= set(body["choices"]["types"])
        assert all(word not in name.lower() for name in _names(body) for word in ("secret", "sessionkey", "password"))

    def test_servidor_negado_ou_outro_usuario_sem_acesso_e_404_sem_ler_nada(self):
        permissions = self.world.guild.members[int(OTHER_USER)].guild_permissions
        permissions.administrator = permissions.manage_guild = False  # já não gerencia o servidor
        outro = self.new_session(OTHER_USER)
        self.world.db.reads.clear()

        responses = [self.call("GET", BASE, session=outro), self.call("GET", "/guilds/999/jukebox")]

        assert [(r.code, error_code(r)) for r in responses] == [(404, "not_found")] * 2
        assert self.world.db.reads == []  # a recusa vem antes de qualquer leitura do banco

    def test_canal_guardado_que_e_de_outro_servidor_nao_vaza_o_nome_e_sai_como_ausente(self):
        self.world.configure("99", None)  # o 99 está no cache do bot, mas é de outro servidor

        body = body_of(self.call("GET", BASE))

        assert body["configured"] is True and body["channel"] == {"id": "99", "name": None, "missing": True}


class JukeboxOptionsTest(JukeboxApiCase):
    VALUES = {"jukebox_auto_delete": True, "jukebox_text_requests": False}

    def test_grava_com_versao_e_versao_velha_da_409_com_a_atual(self):
        version = self.state()["options"]["version"]

        saved = self.call("PUT", BASE, {"expected_version": version, "values": self.VALUES})
        stale = self.call(
            "PUT", BASE, {"expected_version": version, "values": {**self.VALUES, "jukebox_auto_delete": False}}
        )

        assert saved.code == 200 and body_of(saved)["values"] == self.VALUES
        current = body_of(saved)["version"]
        assert (stale.code, error_code(stale)) == (409, "conflict") and body_of(stale)["error"][
            "current_version"
        ] == current

    def test_escrita_reconfere_o_usuario_agora_e_nada_e_gravado(self):
        version = self.state()["options"]["version"]
        self.world.guild.members[int("1001")].guild_permissions.administrator = False
        self.world.guild.members[int("1001")].guild_permissions.manage_guild = False
        writes_before = len(self.world.db.writes)

        response = self.call("PUT", BASE, {"expected_version": version, "values": self.VALUES})

        assert (response.code, error_code(response)) == (404, "not_found") and len(
            self.world.db.writes
        ) == writes_before

    def test_tarefa_em_andamento_recusa_com_409_sem_versao(self):
        self.world.configured_text()
        version = self.state()["version"]
        token = body_of(self.start_task(reset_body(version)))["confirm_token"]
        with self.held_jobs():
            assert self.start_task(reset_body(version, confirm_token=token)).code == 202

            blocked = self.call(
                "PUT", BASE, {"expected_version": self.state()["options"]["version"], "values": self.VALUES}
            )

        assert (blocked.code, error_code(blocked)) == (409, "conflict")
        assert "current_version" not in body_of(blocked)["error"]

    def test_corpo_invalido_e_chave_desconhecida_no_topo(self):
        version = self.state()["options"]["version"]

        missing = self.call("PUT", BASE, {"expected_version": version, "values": {"jukebox_auto_delete": True}})
        wrong = self.call(
            "PUT", BASE, {"expected_version": version, "values": {**self.VALUES, "jukebox_text_requests": "1"}}
        )
        ignored = self.call("PUT", BASE, {"expected_version": version, "values": self.VALUES, "user_id": "9"})

        assert [(r.code, error_code(r)) for r in (missing, wrong)] == [(400, "validation_failed")] * 2
        assert body_of(missing)["error"]["fields"] == ["jukebox_text_requests"]
        assert ignored.code == 200


class JukeboxTaskFlowTest(JukeboxApiCase):
    def test_reiniciar_em_dois_passos_o_passo_1_nao_faz_nada_e_o_2_cria_a_tarefa(self):
        self.world.configured_text()
        version = self.state()["version"]

        step1 = self.start_task(reset_body(version))
        assert step1.code == 200 and body_of(step1)["requires_confirmation"] is True
        assert list(self.world.j) == [] and self.ctx.jukebox_service.runner.active_for_guild(GID) is None
        step2 = self.start_task(reset_body(version, confirm_token=body_of(step1)["confirm_token"]))

        assert step2.code == 202 and len(body_of(step2)["task_id"]) == 32
        self.drain_tasks()
        task = body_of(self.call("GET", f"{BASE}/tasks/{body_of(step2)['task_id']}"))
        assert task["kind"] == "reset" and task["state"] == "ok" and task["final"] is True
        assert set(task) == {"task_id", "kind", "state", "final"}  # nenhum texto de usuário final

    def test_configurar_canal_existente_pede_confirmacao_e_canal_novo_nao(self):
        version = self.state()["version"]

        existing = self.start_task(configure_body(version))
        direct = self.start_task(configure_body(version, SETUP_NEW_CHANNEL))

        assert existing.code == 200 and "confirm_token" in body_of(existing)
        assert direct.code == 202
        self.drain_tasks()

    def test_token_de_uso_unico_vencido_de_outra_finalidade_ou_de_outro_canal_e_recusado(self):
        self.world.configured_text()
        version = self.state()["version"]
        reset_token = body_of(self.start_task(reset_body(version)))["confirm_token"]
        setup_token = body_of(self.start_task(configure_body(version)))["confirm_token"]
        other_channel = body_of(self.start_task(configure_body(version, {"modo": "existente", "canal": "23"})))[
            "confirm_token"
        ]

        wrong_purpose = self.start_task(reset_body(version, confirm_token=setup_token))
        wrong_channel = self.start_task(configure_body(version, confirm_token=other_channel))
        garbage = self.start_task(reset_body(version, confirm_token="x" * 20))
        self.advance(61)
        expired = self.start_task(reset_body(version, confirm_token=reset_token))

        for response in (wrong_purpose, wrong_channel, garbage, expired):
            assert (response.code, error_code(response)) == (400, "validation_failed")
            assert body_of(response)["error"]["fields"] == ["confirm_token"]
        assert list(self.world.j) == []

    def test_token_gasto_nao_vale_de_novo(self):
        self.world.configured_text()
        version = self.state()["version"]
        token = body_of(self.start_task(reset_body(version)))["confirm_token"]
        with self.held_jobs() as release:
            assert self.start_task(reset_body(version, confirm_token=token)).code == 202
            release.set()
            self.drain_tasks()  # a tarefa acabou e não mexeu no estado: só o token já gasto impede a repetição

            again = self.start_task(reset_body(version, confirm_token=token))

        assert (again.code, error_code(again)) == (400, "validation_failed")

    def test_token_de_outro_usuario_nao_vale(self):
        self.world.configured_text()
        version = self.state()["version"]
        token = body_of(self.start_task(reset_body(version)))["confirm_token"]
        outro = self.new_session(OTHER_USER)

        response = self.start_task(reset_body(version, confirm_token=token), session=outro)

        assert (response.code, error_code(response)) == (400, "validation_failed")

    def test_versao_velha_da_409_com_a_atual_e_nada_acontece(self):
        self.world.configured_text()
        current = self.state()["version"]

        stale = self.start_task(reset_body("0" * 16))

        assert (stale.code, error_code(stale)) == (409, "conflict") and body_of(stale)["error"][
            "current_version"
        ] == current

    def test_servidor_ocupado_da_409_sem_versao_e_o_limite_por_usuario_da_429(self):
        self.world.configured_text()
        version = self.state()["version"]
        token = body_of(self.start_task(reset_body(version)))["confirm_token"]
        with self.held_jobs() as release:
            assert self.start_task(reset_body(version, confirm_token=token)).code == 202

            busy = self.start_task(reset_body(version))
            own = self.state()["task"]
            other = self.state(session=self.new_session(OTHER_USER))["task"]

            assert (busy.code, error_code(busy)) == (409, "conflict")
            assert "current_version" not in body_of(busy)["error"]
            assert own["final"] is False and other is None  # a tarefa em andamento só aparece para o dono
            release.set()
            self.drain_tasks()
            with patch.object(self.ctx.jukebox_service.runner, "_max_per_user", 0):
                fresh = body_of(self.start_task(reset_body(version)))["confirm_token"]
                limited = self.start_task(reset_body(version, confirm_token=fresh))
        assert (limited.code, error_code(limited)) == (429, "rate_limited") and "Retry-After" in limited.headers

    def test_validacao_do_canal_aponta_o_campo_e_o_reset_sem_canal_aponta_a_acao(self):
        version = self.state()["version"]

        bad_channel = self.start_task(
            configure_body(version, {"modo": "existente", "canal": "99"})
        )  # de outro servidor
        no_mode = self.start_task(configure_body(version, {"modo": "?"}))
        nothing_to_reset = self.start_task(reset_body(version))

        assert body_of(bad_channel)["error"]["fields"] == ["canal"]
        assert body_of(no_mode)["error"]["fields"] == ["modo"]
        assert body_of(nothing_to_reset)["error"]["fields"] == ["action"]
        assert {bad_channel.code, no_mode.code, nothing_to_reset.code} == {400}

    def test_idempotency_key_repete_o_mesmo_task_id_sem_criar_outra_tarefa(self):
        self.world.configured_text()
        version = self.state()["version"]
        token = body_of(self.start_task(reset_body(version)))["confirm_token"]
        body = reset_body(version, confirm_token=token)

        with self.held_jobs():
            first = self.start_task(body, key="chave-de-teste-1")
            second = self.start_task(body, key="chave-de-teste-1")
            other_body = self.start_task(reset_body(version, confirm_token="y" * 20), key="chave-de-teste-1")
            started = len(self.ctx.jukebox_service.runner._tasks)

        assert first.code == second.code == 202 and body_of(first)["task_id"] == body_of(second)["task_id"]
        assert second.headers["Idempotent-Replayed"] == "true" and started == 1
        assert (other_body.code, error_code(other_body)) == (409, "conflict")

    def test_idempotency_key_em_canal_novo_repete_o_mesmo_task_id(self):
        version = self.state()["version"]
        body = configure_body(version, SETUP_NEW_CHANNEL)

        with self.held_jobs():
            first = self.start_task(body, key="chave-de-teste-2")
            again = self.start_task(body, key="chave-de-teste-2")
            started = len(self.ctx.jukebox_service.runner._tasks)

        assert first.code == again.code == 202 and body_of(first)["task_id"] == body_of(again)["task_id"]
        assert again.headers["Idempotent-Replayed"] == "true" and started == 1

    def test_escrita_reautoriza_agora_a_leitura_aceita_o_cache(self):
        self.world.configured_text()
        version = self.state()["version"]
        member = self.world.guild.members[1001]
        member.guild_permissions.administrator = member.guild_permissions.manage_guild = False

        cached = self.call("GET", BASE)
        write = self.start_task(reset_body(version))

        assert cached.code == 200 and (write.code, error_code(write)) == (404, "not_found")

    def test_limite_de_escrita_por_usuario(self):
        while self.ctx.limits.user_write.allow("1001"):
            pass

        response = self.start_task(reset_body("0" * 16))

        assert (response.code, error_code(response)) == (429, "rate_limited")


class JukeboxTaskIsolationTest(JukeboxApiCase):
    def make_task(self):
        self.world.configured_text()
        version = self.state()["version"]
        token = body_of(self.start_task(reset_body(version)))["confirm_token"]
        task_id = body_of(self.start_task(reset_body(version, confirm_token=token)))["task_id"]
        self.drain_tasks()
        return task_id

    def test_tarefa_de_outro_usuario_do_mesmo_servidor_e_404_como_uma_que_nao_existe(self):
        task_id = self.make_task()
        outro = self.new_session(OTHER_USER)

        theirs = self.call("GET", f"{BASE}/tasks/{task_id}", session=outro)
        missing = self.call("GET", f"{BASE}/tasks/{'A' * 32}", session=outro)

        assert (theirs.code, error_code(theirs)) == (404, "not_found")
        assert strip(body_of(theirs)) == strip(body_of(missing))
        assert self.call("GET", f"{BASE}/tasks/{task_id}").code == 200

    def test_tarefa_de_outro_servidor_e_404_mesmo_para_o_dono(self):
        task_id = self.make_task()
        self.ctx.jukebox_service.runner.get(task_id).guild_id = 999  # a tarefa é de outro servidor

        response = self.call("GET", f"{BASE}/tasks/{task_id}")

        assert (response.code, error_code(response)) == (404, "not_found")

    def test_id_fora_do_formato_nem_chega_ao_servico_e_a_tarefa_vence_em_15_minutos(self):
        task_id = self.make_task()

        malformed = self.call("GET", f"{BASE}/tasks/curto")
        self.advance(15 * 60 + 1)
        expired = self.call("GET", f"{BASE}/tasks/{task_id}")

        assert [(r.code, error_code(r)) for r in (malformed, expired)] == [(404, "not_found")] * 2


class JukeboxSecretsTest(JukeboxApiCase):
    def test_nenhuma_resposta_traz_segredo_alem_do_token_de_confirmacao_pedido(self):
        self.world.configured_text()
        version = self.state()["version"]

        responses = [self.call("GET", BASE), self.start_task(reset_body(version))]

        for response in responses:
            text = dump(response)
            assert "sessionkey" not in text and "secret" not in text.lower()
        assert set(body_of(responses[1])) == {"requires_confirmation", "confirm_token"}


def strip(body: dict) -> dict:
    body = dict(body)
    body["error"] = {key: value for key, value in body["error"].items() if key != "request_id"}
    return body


def _names(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _names(value)
    elif isinstance(node, list):
        for item in node:
            yield from _names(item)
