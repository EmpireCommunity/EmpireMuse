"""B4 · rádios (contrato T-456 §4 "Rádios" e m4; ameaças T-436/T-458): a rádio e a estação são sempre do servidor do caminho
(outro servidor = ``404``), premium e Gerenciar Mensagens onde o painel também pede, escrita com ``expected_version`` (CAS em
``rev``) que reautoriza na hora, redefinir só com token de uso único, o logo só depois das conferências e nenhum segredo."""

from unittest.mock import AsyncMock, patch

import pytest

from empiremuse.utils.radio.store import RadioInvalid
from tests.api.support import body_of, error_code
from tests.api.support_radio import (
    ALFA,
    CHANNEL,
    LIVE_UUID,
    OMEGA,
    OTHER_UUID,
    PNG_1PX,
    BASE,
    RadioApiCase,
    keys_of,
    station,
)
from empiremuse.rules.errors import RuleError
from tests.panel.helpers_radio import LogoResult, make_channel

pytestmark = pytest.mark.critical

SECRET_FRAGMENTS = (
    "secret",
    "sessionkey",
    "password",
    "senha",
    "authorization",
    "cookie",
    "updated_by",
    "url_resolved",
    "favicon",
)
CHAT = {"voice_delete_after_s": 600, "stage_max_messages": 20, "stage_max_age_s": 3600}
HOSTILE = "@everyone discord.gg/convite [x](https://evil.example) ‮drop"


def rev_body(radio, values, **extra):
    return {"expected_version": str(radio.rev), "values": values, **extra}


def assert_not_found(*responses):
    assert [(r.code, error_code(r)) for r in responses] == [(404, "not_found")] * len(responses)


def assert_clean(text):
    """O texto de fora já não tem menção nem link vivos (``external_text``)."""
    assert "@everyone" not in text and "discord.gg" not in text and "](" not in text and "://" not in text
    assert "‮" not in text


class RadioReadTest(RadioApiCase):
    def test_lista_com_premium_traz_estado_estacoes_e_opcoes_sem_url_de_fluxo_nem_segredo(self):
        self.premium()
        radio = self.seed_live(reserve=OTHER_UUID)
        self.radio.setup.bots[ALFA] = ((2, "Bot Dois"),)
        self.radio.setup.channels[ALFA] = ((CHANNEL, "Sala DJ", "voice"),)

        response = self.get(f"{BASE}/radios")

        body = body_of(response)
        entry = body["radios"][0]
        assert response.code == 200 and body["premium"] is True and body["max_radios"] == 3
        assert (entry["id"], entry["kind"], entry["version"], entry["can_edit"]) == (
            radio.id,
            "live",
            str(radio.rev),
            True,
        )
        assert entry["stations"]["main"]["name"] == "Rock FM" and entry["stations"]["reserve"]["name"] == "Jazz FM"
        assert body["setup"] == {
            "bots": [{"id": "2", "name": "Bot Dois"}],
            "channels": [{"id": str(CHANNEL), "name": "Sala DJ", "kind": "voice"}],
        }
        names = [name.lower() for name in keys_of(body)]
        assert not [name for name in names if any(part in name for part in SECRET_FRAGMENTS)]
        assert "stream.example.com" not in response.body.decode() and "icon.example.com" not in response.body.decode()

    def test_sem_premium_nada_vai_ao_diretorio_e_nao_ha_opcoes_de_criar(self):
        self.seed_live()

        body = body_of(self.get(f"{BASE}/radios"))

        assert body["premium"] is False and "setup" not in body and "stations" not in body["radios"][0]
        assert body["radios"][0]["can_edit"] is False
        assert self.radio.directory.by_uuid_calls == []

    def test_titulo_e_nomes_de_fora_saem_sem_mencao_nem_link_vivos(self):
        self.premium()
        radio = self.seed_live()
        self.radio.service.states[radio.id].title = HOSTILE
        self.radio.directory.stations[LIVE_UUID] = station(LIVE_UUID, HOSTILE, HOSTILE)

        entry = body_of(self.get(f"{BASE}/radios"))["radios"][0]

        for text in (entry["title"], entry["stations"]["main"]["name"], entry["stations"]["main"]["country"]):
            assert_clean(text)

    def test_so_as_rotas_do_servidor_do_caminho_a_rotulada_de_outro_servidor_e_404(self):
        self.premium(OMEGA)
        omega = self.seed_live(guild=OMEGA, channel=789)
        self.premium()
        alfa = self.seed_live()

        listed = body_of(self.get(f"{BASE}/radios"))["radios"]
        crossed = self.get(f"{BASE}/radios/{omega.id}")  # id de Ômega sob Alfa
        swapped = self.get(f"/guilds/{OMEGA}/radios/{alfa.id}")  # id de Alfa sob Ômega
        malformed = [
            self.get(f"{BASE}/radios/abc"),
            self.get(f"{BASE}/radios/1:100:2"),
            self.get(f"{BASE}/radios/1:{ALFA + 1}"),
        ]

        assert [r["id"] for r in listed] == [alfa.id]
        assert_not_found(crossed, swapped, *malformed)
        assert self.get(f"{BASE}/radios/{alfa.id}").code == 200

    def test_servidor_negado_e_o_mesmo_404_e_nada_e_lido(self):
        self.seed_live()
        self.radio.store.fail_with = RuntimeError("não deveria ler")

        denied = self.get(f"{BASE}/radios", session=self.new_session("3003"))

        assert (denied.code, error_code(denied)) == (404, "not_found")

    def test_armazem_das_radios_fora_do_ar_e_503(self):
        from empiremuse.utils.radio.premium import RadioUnavailable

        self.radio.store.fail_with = RadioUnavailable()

        response = self.get(f"{BASE}/radios")

        assert (response.code, error_code(response)) == (503, "bot_unavailable")


class RadioChatWriteTest(RadioApiCase):
    def test_grava_com_versao_e_devolve_a_radio_com_o_rev_novo(self):
        self.premium()
        radio = self.seed_live()

        response = self.call("PUT", self.radio_url(radio, "/chat"), rev_body(radio, CHAT))

        body = body_of(response)
        assert response.code == 200 and body["chat"] == CHAT and body["version"] == str(radio.rev + 1)
        assert self.radio.store.updates == [(radio.id, "1001")]  # quem gravou é o usuário da sessão

    def test_versao_velha_da_409_com_o_rev_atual_e_nao_grava(self):
        self.premium()
        radio = self.seed_live()

        stale = self.call("PUT", self.radio_url(radio, "/chat"), {"expected_version": "1", "values": CHAT})

        assert (stale.code, error_code(stale)) == (409, "conflict")
        assert body_of(stale)["error"]["current_version"] == str(radio.rev) and self.radio.store.updates == []

    def test_sem_premium_ou_sem_gerenciar_mensagens_e_403_e_nada_e_gravado(self):
        radio = self.seed_live()
        no_premium = self.call("PUT", self.radio_url(radio, "/chat"), rev_body(radio, CHAT))
        self.premium()
        self.world.channels[CHANNEL] = make_channel(CHANNEL, "Sala DJ", ALFA, manage_messages=False)

        no_messages = self.call("PUT", self.radio_url(radio, "/chat"), rev_body(radio, CHAT))

        assert [(r.code, error_code(r)) for r in (no_premium, no_messages)] == [(403, "forbidden")] * 2
        assert self.radio.store.updates == []

    def test_rotas_de_outro_servidor_e_usuario_negado_dao_404_antes_de_olhar_o_corpo(self):
        self.premium(OMEGA)
        omega = self.seed_live(guild=OMEGA, channel=789)
        self.premium()
        alfa = self.seed_live()

        crossed = self.call("PUT", f"{BASE}/radios/{omega.id}/chat", {"values": "lixo"})
        denied = self.call("PUT", self.radio_url(alfa, "/chat"), {"values": "lixo"}, session=self.new_session("3003"))

        assert_not_found(crossed, denied)
        assert self.radio.store.updates == []

    def test_escrita_reconfere_o_usuario_agora_e_a_leitura_aceita_o_cache(self):
        self.premium()
        radio = self.seed_live()
        assert self.get(self.radio_url(radio)).code == 200  # entra no cache
        self.revoke()

        cached = self.get(self.radio_url(radio))
        write = self.call("PUT", self.radio_url(radio, "/chat"), rev_body(radio, CHAT))

        assert cached.code == 200 and (write.code, error_code(write)) == (404, "not_found")
        assert self.radio.store.updates == []

    def test_corpo_invalido_aponta_o_campo_e_chave_desconhecida_no_topo_e_ignorada(self):
        self.premium()
        radio = self.seed_live()
        url = self.radio_url(radio, "/chat")

        missing = self.call("PUT", url, rev_body(radio, {"voice_delete_after_s": 600}))
        wrong = self.call("PUT", url, rev_body(radio, {**CHAT, "stage_max_messages": "20"}))
        out_of_range = self.call("PUT", url, rev_body(radio, {**CHAT, "stage_max_messages": 99999}))
        no_version = self.call("PUT", url, {"values": CHAT})
        top_level = self.call("PUT", url, rev_body(radio, CHAT, user_id="9", enabled=True))

        assert [(r.code, error_code(r)) for r in (missing, wrong, out_of_range, no_version)] == [
            (400, "validation_failed")
        ] * 4
        assert body_of(missing)["error"]["fields"] == ["stage_max_age_s", "stage_max_messages"]
        assert body_of(out_of_range)["error"]["fields"] == ["stage_max_messages"]
        assert body_of(no_version)["error"]["fields"] == ["expected_version"]
        assert top_level.code == 200 and body_of(top_level)["enabled"] is True  # ``enabled`` do topo não mudou nada

    def test_idempotency_key_repete_a_resposta_sem_gravar_de_novo_e_outro_corpo_e_409(self):
        self.premium()
        radio = self.seed_live()
        body = rev_body(radio, CHAT)

        first = self.call("PUT", self.radio_url(radio, "/chat"), body, key="chave-de-teste-1")
        again = self.call("PUT", self.radio_url(radio, "/chat"), body, key="chave-de-teste-1")
        other = self.call(
            "PUT",
            self.radio_url(radio, "/chat"),
            rev_body(radio, {**CHAT, "stage_max_messages": 30}),
            key="chave-de-teste-1",
        )

        assert first.code == again.code == 200 and again.headers["Idempotent-Replayed"] == "true"
        assert body_of(first) == body_of(again) and len(self.radio.store.updates) == 1
        assert (other.code, error_code(other)) == (409, "conflict")

    def test_limite_de_escrita_por_usuario_no_chat(self):
        self.premium()
        radio = self.seed_live()
        while self.ctx.limits.user_write.allow("1001"):
            pass

        response = self.call("PUT", self.radio_url(radio, "/chat"), rev_body(radio, CHAT))

        assert (response.code, error_code(response)) == (429, "rate_limited") and self.radio.store.updates == []

    def test_registra_a_acao_do_painel_com_o_rotulo_do_generico_e_sem_o_usuario(self):
        self.premium()
        radio = self.seed_live()

        with patch("empiremuse.api.handlers_radio.record_panel_action") as record:
            self.call("PUT", self.radio_url(radio, "/chat"), rev_body(radio, CHAT))
            self.call("PUT", self.radio_url(radio, "/chat"), {"expected_version": "1", "values": CHAT})

        assert [call.args[1:] for call in record.call_args_list] == [("radios/chat", 200), ("radios/chat", 409)]
        assert all(call.args[0].user_id == "1001" for call in record.call_args_list)


class RadioStationWriteTest(RadioApiCase):
    def test_principal_e_reserva_de_radio_ao_vivo_com_cas_e_sem_url_de_fluxo(self):
        self.premium()
        radio = self.seed_live()

        main = self.call("PUT", self.radio_url(radio, "/station"), rev_body(radio, {"estacao": OTHER_UUID}))
        fresh = self.radio.store.docs[radio.id]
        reserve = self.call("PUT", self.radio_url(radio, "/reserve"), rev_body(fresh, {"reserva": LIVE_UUID}))

        assert (
            body_of(main)["source"]["station"] == OTHER_UUID and body_of(main)["stations"]["main"]["name"] == "Jazz FM"
        )
        assert body_of(reserve)["source"] == {"station": OTHER_UUID, "reserve": LIVE_UUID}
        assert "stream.example.com" not in main.body.decode() + reserve.body.decode()

    def test_estacao_indisponivel_ou_invalida_e_400_no_campo_e_nada_e_gravado(self):
        self.premium()
        radio = self.seed_live()
        self.radio.health.down.add(OTHER_UUID)

        down = self.call("PUT", self.radio_url(radio, "/station"), rev_body(radio, {"estacao": OTHER_UUID}))
        garbage = self.call("PUT", self.radio_url(radio, "/station"), rev_body(radio, {"estacao": "nao-e-uuid"}))
        same_as_main = self.call("PUT", self.radio_url(radio, "/reserve"), rev_body(radio, {"reserva": LIVE_UUID}))

        for response, field in ((down, "estacao"), (garbage, "estacao"), (same_as_main, "reserva")):
            assert (response.code, error_code(response)) == (400, "validation_failed")
            assert body_of(response)["error"]["fields"] == [field]
        assert self.radio.store.updates == []

    def test_reserva_so_em_radio_ao_vivo_e_nula_tira_a_reserva(self):
        self.premium()
        live = self.seed_live(reserve=OTHER_UUID)
        custom = self.seed_custom(bot=2)
        self.world.bot2.guilds[ALFA] = self.world.g100  # o bot 2 passa a estar em Alfa (para a rádio dele ter bot)

        cleared = self.call("PUT", self.radio_url(live, "/reserve"), rev_body(live, {"reserva": None}))
        wrong_kind = self.call("PUT", self.radio_url(custom, "/reserve"), rev_body(custom, {"reserva": None}))

        assert body_of(cleared)["source"]["reserve"] is None
        assert (wrong_kind.code, body_of(wrong_kind)["error"]["fields"]) == (400, ["kind"])

    def test_radio_personalizada_escolhe_estacao_do_servidor_e_a_de_outro_servidor_e_recusada(self):
        self.premium()
        custom = self.seed_custom()
        mine = self.radio.stations.seed(ALFA, "Minha")
        theirs = self.radio.stations.seed(OMEGA, "Alheia")

        ok = self.call("PUT", self.radio_url(custom, "/station"), rev_body(custom, {"estacao": mine.id}))
        refused = self.call(
            "PUT",
            self.radio_url(custom, "/station"),
            rev_body(self.radio.store.docs[custom.id], {"estacao": theirs.id}),
        )

        assert ok.code == 200 and body_of(ok)["source"]["station"] == mine.id
        assert (refused.code, error_code(refused)) == (400, "validation_failed")

    def test_versao_velha_da_409_com_o_rev_atual(self):
        self.premium()
        radio = self.seed_live()

        stale = self.call(
            "PUT", self.radio_url(radio, "/station"), {"expected_version": "1", "values": {"estacao": OTHER_UUID}}
        )

        assert (stale.code, error_code(stale)) == (409, "conflict")
        assert body_of(stale)["error"]["current_version"] == str(radio.rev)


class RadioSearchAndLocalTest(RadioApiCase):
    def test_busca_valida_o_termo_antes_do_diretorio_e_nunca_devolve_a_url_do_fluxo(self):
        self.premium()
        radio = self.seed_live()
        self.radio.directory.results = [station(OTHER_UUID, HOSTILE)]

        short = self.get(self.radio_url(radio, "/stations?q=a"))
        control = self.get(self.radio_url(radio, "/stations?q=" + "x" * 65))  # acima de 64 caracteres
        repeated = self.get(self.radio_url(radio, "/stations?q=ab&q=cd"))
        calls_before_valid = list(self.radio.directory.calls)
        found = self.get(self.radio_url(radio, "/stations?q=jazz"))

        assert [(r.code, body_of(r)["error"]["fields"]) for r in (short, control, repeated)] == [(400, ["q"])] * 3
        assert calls_before_valid == [] and self.radio.directory.calls == [("jazz", ALFA)]
        names = [item["name"] for item in body_of(found)["stations"]]
        assert found.code == 200 and len(names) == 1 and "stream.example.com" not in found.body.decode()
        assert_clean(names[0])

    def test_busca_pede_premium_gerenciar_mensagens_radio_ao_vivo_e_servidor_certo(self):
        radio = self.seed_live()
        no_premium = self.get(self.radio_url(radio, "/stations?q=jazz"))
        self.premium()
        custom = self.seed_custom(bot=2)
        self.world.bot2.guilds[ALFA] = self.world.g100
        not_live = self.get(self.radio_url(custom, "/stations?q=jazz"))
        crossed = self.get(f"/guilds/{OMEGA}/radios/{radio.id}/stations?q=jazz")

        assert (no_premium.code, error_code(no_premium)) == (403, "forbidden")
        assert_not_found(not_live, crossed)
        assert self.radio.directory.calls == []

    def test_cada_busca_gasta_o_limite_de_escrita_do_usuario(self):
        self.premium()
        radio = self.seed_live()
        while self.ctx.limits.user_write.allow("1001"):
            pass

        response = self.get(self.radio_url(radio, "/stations?q=jazz"))

        assert (response.code, error_code(response)) == (429, "rate_limited") and self.radio.directory.calls == []

    def test_cascata_so_manda_o_no_final_ao_diretorio_e_no_forjado_e_400(self):
        self.premium()
        radio = self.seed_live()
        self.radio.directory.region_hits = [station(OTHER_UUID, HOSTILE)]
        url = self.radio_url(radio, "/local")

        top = body_of(self.get(url))
        middle = body_of(self.get(url + "?node=c:BR"))
        forged = self.get(url + "?node=../../etc")
        final = body_of(self.get(url + "?node=x:3448439&pagina=1"))

        assert top["node"] is None and top["levels"] and top["stations"] == [] and top["paging"] is None
        assert middle["final"] is False and self.radio.directory.region_calls == [("x:3448439", ALFA)]
        assert (forged.code, body_of(forged)["error"]["fields"]) == (400, ["node"])
        assert final["final"] is True and final["paging"]["page"] == 1 and len(final["stations"]) == 1
        assert_clean(final["stations"][0]["name"])

    def test_sem_arvore_ou_sem_permissao_a_cascata_nao_abre(self):
        radio = self.seed_live()
        no_premium = self.get(self.radio_url(radio, "/local"))
        self.premium()
        self.radio.locations = None

        no_tree = self.get(self.radio_url(radio, "/local"))

        assert (no_premium.code, error_code(no_premium)) == (403, "forbidden")
        assert (no_tree.code, error_code(no_tree)) == (404, "not_found")


class RadioSetupTest(RadioApiCase):
    def prepare_options(self):
        self.radio.setup.bots[ALFA] = ((2, "Bot Dois"), (3, "Bot Três"))
        self.radio.setup.channels[ALFA] = ((CHANNEL, "Sala DJ", "voice"), (321, "Palco", "stage"))

    def test_criar_exige_premium_e_o_bot_nem_e_chamado_sem_ele(self):
        self.prepare_options()

        response = self.call(
            "POST", f"{BASE}/radios", {"values": {"kind": "custom", "channel_id": str(CHANNEL), "bot_id": "2"}}
        )

        assert (response.code, error_code(response)) == (403, "forbidden") and self.radio.setup.calls == []

    def test_criar_chama_o_bot_com_inteiros_e_o_ator_da_sessao_e_devolve_201(self):
        self.premium()
        self.prepare_options()

        response = self.call(
            "POST", f"{BASE}/radios", {"values": {"kind": "custom", "channel_id": str(CHANNEL), "bot_id": "2"}}
        )

        assert response.code == 201 and body_of(response)["id"] == f"2:{ALFA}" and body_of(response)["enabled"] is False
        call = self.radio.setup.calls[0]
        assert call[:3] == ("configure", ALFA, 1001) and call[3]["radio_id"] is None and call[3]["rev"] is None

    def test_recusas_do_bot_apontam_o_campo_e_o_segundo_create_do_mesmo_bot_e_recusado(self):
        self.premium()
        self.prepare_options()
        values = {"kind": "custom", "channel_id": str(CHANNEL), "bot_id": "2"}
        assert self.call("POST", f"{BASE}/radios", {"values": values}).code == 201

        again = self.call("POST", f"{BASE}/radios", {"values": values})
        not_reserve = self.call("POST", f"{BASE}/radios", {"values": {**values, "bot_id": "9"}})
        bad_kind = self.call("POST", f"{BASE}/radios", {"values": {**values, "kind": "x", "bot_id": "3"}})
        wrong_type = self.call("POST", f"{BASE}/radios", {"values": {**values, "bot_id": 3}})

        assert [(r.code, body_of(r)["error"]["fields"]) for r in (again, not_reserve)] == [
            (400, ["bot_id"]),
            (400, ["bot_id"]),
        ]
        assert body_of(bad_kind)["error"]["fields"] == ["kind"] and body_of(wrong_type)["error"]["fields"] == ["bot_id"]

    def test_configurar_mantem_o_cas_e_rejeita_rev_velho_com_o_atual(self):
        self.premium()
        self.prepare_options()
        radio = self.seed_live(enabled=False)
        values = {"kind": "live", "channel_id": "321", "bot_id": "1"}

        stale = self.call("PUT", self.radio_url(radio), {"expected_version": "1", "values": values})
        saved = self.call("PUT", self.radio_url(radio), rev_body(radio, values))

        assert (stale.code, error_code(stale)) == (409, "conflict") and body_of(stale)["error"][
            "current_version"
        ] == str(radio.rev)
        assert saved.code == 200 and body_of(saved)["channel"]["id"] == "321"

    def test_configurar_sem_premium_e_403_e_de_outro_servidor_404(self):
        self.prepare_options()
        radio = self.seed_live(enabled=False)
        values = {"kind": "live", "channel_id": "321", "bot_id": "1"}

        no_premium = self.call("PUT", self.radio_url(radio), rev_body(radio, values))
        crossed = self.call("PUT", f"/guilds/{OMEGA}/radios/{radio.id}", rev_body(radio, values))

        assert (no_premium.code, error_code(no_premium)) == (403, "forbidden") and self.radio.setup.calls == []
        assert_not_found(crossed)


class RadioResetTest(RadioApiCase):
    def prepare(self, radio):
        response = self.call("POST", self.radio_url(radio, "/reset/prepare"), {})
        assert response.code == 200, response.body
        return body_of(response)["confirm_token"]

    def reset(self, radio, token, **extra):
        return self.call("POST", self.radio_url(radio, "/reset"), {"confirm_token": token}, **extra)

    def test_prepare_nao_apaga_e_o_reset_com_o_token_apaga_sem_olhar_a_versao(self):
        radio = self.seed_live()  # sem premium: redefinir pede só gerenciar o servidor
        token = self.prepare(radio)
        assert radio.id in self.radio.store.docs and self.radio.setup.calls == []

        response = self.reset(radio, token)

        assert response.code == 204 and response.body == b"" and radio.id not in self.radio.store.docs
        assert self.radio.setup.calls[0][0] == "reset" and self.radio.setup.calls[0][3]["rev"] is None

    def test_tela_velha_tambem_apaga_o_rev_que_o_painel_viu_nao_entra(self):
        self.premium()  # a escrita do chat pede premium
        radio = self.seed_live(rev=3)
        token = self.prepare(radio)
        before = radio.rev

        edit = self.call("PUT", self.radio_url(radio, "/chat"), rev_body(radio, CHAT))  # alguém mexeu depois
        assert edit.code == 200, edit.body
        assert self.radio.store.docs[radio.id].rev != before

        response = self.reset(radio, token)

        assert response.code == 204 and radio.id not in self.radio.store.docs

    def test_token_de_outro_usuario_radio_servidor_finalidade_expirado_ou_lixo_e_recusado_e_nada_e_apagado(self):
        first, second = self.seed_live(bot=1), self.seed_live(bot=2)
        mine = self.prepare(first)
        theirs = self.prepare(second)
        other_user_token = body_of(self.call("POST", self.radio_url(first, "/reset/prepare"), {}, session=self.other))[
            "confirm_token"
        ]
        wrong_purpose = self.ctx.radio_setup_service.tokens.issue("1001", ALFA, "configurar", first.id)
        stale = self.prepare(first)
        self.advance(61)

        attempts = [
            self.reset(first, theirs),  # token de outra rádio
            self.reset(first, other_user_token, session=self.token),  # emitido para o outro usuário
            self.reset(first, wrong_purpose),  # de outra finalidade
            self.reset(first, "lixo"),
            self.reset(first, mine),  # vencido
            self.reset(first, stale),
        ]

        for response in attempts:
            assert (response.code, error_code(response)) == (400, "validation_failed")
            assert body_of(response)["error"]["fields"] == ["confirm_token"]
        assert set(self.radio.store.docs) == {first.id, second.id} and self.radio.setup.calls == []

    def test_token_de_um_servidor_nao_vale_em_outro(self):
        alfa = self.seed_live()
        omega = self.seed_live(guild=OMEGA, channel=789)
        token = self.prepare(alfa)

        crossed = self.call("POST", f"/guilds/{OMEGA}/radios/{omega.id}/reset", {"confirm_token": token})

        assert (crossed.code, error_code(crossed)) == (400, "validation_failed") and omega.id in self.radio.store.docs

    def test_o_token_e_gasto_mesmo_quando_o_bot_recusa_e_nao_vale_de_novo(self):
        radio = self.seed_live()
        token = self.prepare(radio)
        self.radio.setup.fail_with = RadioInvalid(RuleError("x", "y"))

        refused = self.reset(radio, token)
        self.radio.setup.fail_with = None
        again = self.reset(radio, token)

        assert refused.code == 400 and (again.code, body_of(again)["error"]["fields"]) == (400, ["confirm_token"])
        assert radio.id in self.radio.store.docs

    def test_so_quem_gerencia_o_servidor_agora_prepara_e_apaga_e_a_rota_de_outro_servidor_e_404(self):
        alfa = self.seed_live()
        omega = self.seed_live(guild=OMEGA, channel=789)
        token = self.prepare(alfa)
        self.revoke()

        prepare = self.call("POST", self.radio_url(alfa, "/reset/prepare"), {})
        reset = self.reset(alfa, token)
        crossed = self.call("POST", f"{BASE}/radios/{omega.id}/reset/prepare", {})

        assert_not_found(prepare, reset, crossed)
        assert alfa.id in self.radio.store.docs

    def test_idempotency_key_no_reset_repete_204_sem_chamar_o_bot_de_novo(self):
        radio = self.seed_live()
        token = self.prepare(radio)

        first = self.reset(radio, token, key="chave-de-teste-1")
        again = self.reset(radio, token, key="chave-de-teste-1")

        assert first.code == again.code == 204 and again.headers["Idempotent-Replayed"] == "true"
        assert len(self.radio.setup.calls) == 1

    def test_registra_a_acao_do_painel_com_o_rotulo_do_generico(self):
        radio = self.seed_live()

        with patch("empiremuse.api.handlers_radio.record_panel_action") as record:
            token = self.prepare(radio)
            self.reset(radio, token)

        assert [call.args[1:] for call in record.call_args_list] == [
            ("radios/reset_prepare", 200),
            ("radios/reset", 200),
        ]


class RadioLogoTest(RadioApiCase):
    def test_devolve_o_png_da_estacao_principal_so_depois_das_conferencias(self):
        self.premium()
        radio = self.seed_live()
        self.radio.directory.logos[LIVE_UUID] = LogoResult(PNG_1PX, False)

        response = self.get(self.radio_url(radio, "/logo"))

        assert response.code == 200 and response.body == PNG_1PX
        assert response.headers["Content-Type"] == "image/png" and response.headers["Cache-Control"] == "no-store"
        assert self.radio.directory.logo_calls == [LIVE_UUID]

    def test_sem_sessao_servidor_negado_ou_radio_de_outro_servidor_o_diretorio_nem_e_chamado(self):
        self.premium(OMEGA)
        omega = self.seed_live(guild=OMEGA, channel=789)
        alfa = self.seed_live()
        denied = self.new_session("3003")

        responses = [
            self.api("GET", self.radio_url(alfa, "/logo")),  # sem sessão
            self.get(self.radio_url(alfa, "/logo"), session=denied),
            self.get(f"{BASE}/radios/{omega.id}/logo"),
            self.get(f"{BASE}/radios/{alfa.id}x/logo"),
        ]

        assert [r.code for r in responses] == [401, 404, 404, 404]
        assert self.radio.directory.logo_calls == []

    def test_sem_logo_radio_personalizada_ou_falha_do_diretorio_cai_no_icone_padrao_sem_dizer_o_motivo(self):
        live = self.seed_live()
        custom = self.seed_custom(bot=2)

        default = self.get(self.radio_url(live, "/logo"))
        no_station = self.get(self.radio_url(custom, "/logo"))
        with patch.object(self.radio.directory, "logo_png", side_effect=RuntimeError("segredo http://interno")):
            failing = self.get(self.radio_url(live, "/logo"))

        for response in (default, no_station, failing):
            assert response.code == 200 and response.body.startswith(b"\x89PNG") and b"interno" not in response.body

    def test_o_logo_aninhado_e_a_unica_rota_a_antiga_por_uuid_nao_existe(self):
        self.seed_live()

        old = self.get(f"/radios/logo/{LIVE_UUID}.png")

        assert (old.code, error_code(old)) == (404, "not_found")


class RadioStationsRoutesTest(RadioApiCase):
    CREATE = {"name": "Minha", "kind": "artists", "artists": ["Queen", "Abba"]}

    def test_nome_com_espacos_internos_e_gravado_colapsado_e_a_criacao_responde_201(self):
        self.premium()
        original = self.radio.stations._validate

        def collapsing(guild_id, raw, **kwargs):  # como a store real: espaços internos colapsados
            name, kind, source = original(guild_id, {**raw, "name": " ".join(str(raw["name"]).split())}, **kwargs)
            return name, kind, source

        with patch.object(self.radio.stations, "_validate", collapsing):
            response = self.call("POST", f"{BASE}/radio-stations", {"values": {**self.CREATE, "name": "Minha   rádio"}})

        assert response.code == 201 and body_of(response)["name"] == "Minha rádio"

    def test_apagar_sem_versao_de_estacao_existente_e_400_e_nada_e_apagado(self):
        self.premium()
        item = self.radio.stations.seed(ALFA, "Minha")

        response = self.call("DELETE", f"{BASE}/radio-stations/{item.id}")

        assert (response.code, body_of(response)["error"]["fields"]) == (400, ["expected_version"])
        assert item.id in self.radio.stations.docs

    def test_exige_premium_em_todas_as_rotas_e_nada_e_gravado(self):
        station_ = self.radio.stations.seed(ALFA, "Minha")
        url = f"{BASE}/radio-stations"

        responses = [
            self.get(url),
            self.call("POST", url, {"values": self.CREATE}),
            self.call("PUT", f"{url}/{station_.id}", {"expected_version": "1", "values": self.CREATE}),
            self.call("DELETE", f"{url}/{station_.id}?expected_version=1"),
        ]

        assert [(r.code, error_code(r)) for r in responses] == [(403, "forbidden")] * 4
        assert self.radio.stations.updates == []

    def test_criar_lista_editar_e_apagar_com_cas(self):
        self.premium()
        url = f"{BASE}/radio-stations"

        created = self.call("POST", url, {"values": self.CREATE})
        station_id = body_of(created)["id"]
        listed = body_of(self.get(url))
        stale = self.call(
            "PUT", f"{url}/{station_id}", {"expected_version": "7", "values": {**self.CREATE, "name": "Nova"}}
        )
        edited = self.call(
            "PUT", f"{url}/{station_id}", {"expected_version": "1", "values": {**self.CREATE, "name": "Nova"}}
        )
        old_delete = self.call("DELETE", f"{url}/{station_id}?expected_version=1")
        deleted = self.call("DELETE", f"{url}/{station_id}?expected_version=2")

        assert created.code == 201 and body_of(created)["source"] == {"artists": ["Queen", "Abba"]}
        assert [s["name"] for s in listed["stations"]] == ["Minha"] and listed["max"] == 10 and listed["genres"]
        assert (stale.code, body_of(stale)["error"].get("current_version")) == (409, "1")
        assert edited.code == 200 and body_of(edited)["version"] == "2" and body_of(edited)["name"] == "Nova"
        assert (old_delete.code, body_of(old_delete)["error"]["current_version"]) == (409, "2")
        assert deleted.code == 204 and self.radio.stations.docs == {}

    def test_estacao_de_outro_servidor_e_404_em_editar_e_apagar_e_nada_muda(self):
        self.premium()
        theirs = self.radio.stations.seed(OMEGA, "Alheia")
        url = f"{BASE}/radio-stations/{theirs.id}"

        responses = [
            self.call("PUT", url, {"expected_version": "1", "values": self.CREATE}),
            self.call("DELETE", url + "?expected_version=1"),
            self.call("PUT", f"{BASE}/radio-stations/{'f' * 24}", {"expected_version": "1", "values": self.CREATE}),
            self.call("PUT", f"{BASE}/radio-stations/xyz", {"expected_version": "1", "values": self.CREATE}),
        ]

        assert_not_found(*responses)
        assert theirs.id in self.radio.stations.docs and self.radio.stations.updates == []

    def test_nome_repetido_limite_de_dez_e_corpo_invalido_apontam_o_campo(self):
        self.premium()
        url = f"{BASE}/radio-stations"
        assert self.call("POST", url, {"values": self.CREATE}).code == 201

        duplicate = self.call("POST", url, {"values": self.CREATE})
        bad_kind = self.call("POST", url, {"values": {**self.CREATE, "name": "Outra", "kind": "x"}})
        wrong_type = self.call("POST", url, {"values": {**self.CREATE, "artists": "Queen"}})
        for index in range(9):
            self.radio.stations.seed(ALFA, f"Pronta {index}")
        limit = self.call("POST", url, {"values": {**self.CREATE, "name": "Onze"}})

        assert body_of(duplicate)["error"]["fields"] == ["name"] and body_of(bad_kind)["error"]["fields"] == ["kind"]
        assert body_of(wrong_type)["error"]["fields"] == ["artists"] and body_of(limit)["error"]["fields"] == ["limit"]
        assert {r.code for r in (duplicate, bad_kind, wrong_type, limit)} == {400}

    def test_apagar_estacao_em_uso_e_recusado_e_nada_e_apagado(self):
        self.premium()
        used = self.radio.stations.seed(ALFA, "Em uso")
        radio = self.seed_custom()
        self.radio.store.docs[radio.id] = self.radio.store.docs[radio.id].__class__(
            **{**self.radio.store.docs[radio.id].__dict__, "source": {"station": used.id}}
        )

        response = self.call("DELETE", f"{BASE}/radio-stations/{used.id}?expected_version={used.rev}")

        assert (response.code, body_of(response)["error"]["fields"]) == (
            400,
            ["in_use"],
        ) and used.id in self.radio.stations.docs

    def test_nome_de_fora_sai_sem_mencao_e_sem_segredo_na_lista(self):
        self.premium()
        self.radio.stations.seed(ALFA, HOSTILE[:32], source={"artists": [HOSTILE]})

        response = self.get(f"{BASE}/radio-stations")

        entry = body_of(response)["stations"][0]
        assert_clean(entry["display"]["name"] + " ".join(entry["display"]["source"]["artists"]))
        assert not [n for n in keys_of(body_of(response)) if any(part in n.lower() for part in SECRET_FRAGMENTS)]

    def test_o_que_a_leitura_devolve_volta_na_edicao_mesmo_com_texto_que_o_tratamento_altera(self):
        self.premium()
        self.radio.stations.seed(ALFA, "rock.fm", source={"artists": ["rock.fm", "a@everyone"]})
        url = f"{BASE}/radio-stations"

        entry = body_of(self.get(url))["stations"][0]
        values = {"name": entry["name"], "kind": entry["kind"], **entry["source"]}
        response = self.call("PUT", f"{url}/{entry['id']}", {"expected_version": entry["version"], "values": values})

        assert entry["name"] == "rock.fm" and entry["source"]["artists"] == ["rock.fm", "a@everyone"]
        assert entry["display"]["source"]["artists"] != entry["source"]["artists"]  # o rótulo é outro texto
        assert response.code == 200 and body_of(response)["source"] == entry["source"]

    def test_lista_de_playlists_traz_o_nome_guardado_e_o_rotulo_a_parte(self):
        self.premium()
        with patch.object(self.ctx.stations_page, "playlist_names", AsyncMock(return_value=("rock.fm",))):
            body = body_of(self.get(f"{BASE}/radio-stations"))

        assert body["playlists"] == ["rock.fm"] and len(body["playlists_display"]) == 1
        assert body["playlists_display"][0] != "rock.fm"

    def test_separador_de_linha_que_nao_e_o_n_e_recusado_nos_artistas(self):
        self.premium()
        url = f"{BASE}/radio-stations"

        responses = [
            self.call("POST", url, {"values": {**self.CREATE, "artists": [f"Queen{sep}Abba"]}})
            for sep in ("\r", "\x0b", "\x1c", "\x85", "\u2028", "\u2029")
        ]

        assert {(r.code, body_of(r)["error"]["fields"][0]) for r in responses} == {(400, "artists")}
        assert self.radio.stations.docs == {}

    def test_apagar_em_uso_com_versao_velha_da_409_com_a_atual_antes_do_in_use(self):
        self.premium()
        used = self.radio.stations.seed(ALFA, "Em uso")
        radio = self.seed_custom()
        self.radio.store.docs[radio.id] = self.radio.store.docs[radio.id].__class__(
            **{**self.radio.store.docs[radio.id].__dict__, "source": {"station": used.id}}
        )

        response = self.call("DELETE", f"{BASE}/radio-stations/{used.id}?expected_version={used.rev + 5}")

        assert (response.code, body_of(response)["error"]["current_version"]) == (409, str(used.rev))
        assert used.id in self.radio.stations.docs

    def test_escrita_reconfere_agora_e_registra_o_rotulo_do_generico(self):
        self.premium()
        url = f"{BASE}/radio-stations"
        with patch("empiremuse.api.handlers_stations.record_panel_action") as record:
            self.call("POST", url, {"values": self.CREATE})
            self.revoke()
            denied = self.call("POST", url, {"values": {**self.CREATE, "name": "Outra"}})

        assert [call.args[1:] for call in record.call_args_list] == [("radios/estacoes", 200)]
        assert (denied.code, error_code(denied)) == (404, "not_found") and len(self.radio.stations.docs) == 1

    def test_rotas_sem_corpo_json_ou_com_tipo_errado(self):
        self.premium()
        url = f"{BASE}/radio-stations"

        plain = self.call("POST", url, raw_body="x", headers={"Content-Type": "text/plain"})
        not_object = self.call("POST", url, raw_body="[]", headers={"Content-Type": "application/json"})
        no_version = self.call("DELETE", f"{url}/{'a' * 24}")

        assert (plain.code, not_object.code) == (415, 400)
        assert no_version.code == 404  # a estação não existe: o ``404`` vem antes de conferir a versão
