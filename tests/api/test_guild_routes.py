"""B3 · servidores e configurações (contrato T-456 §4 "Servidores" e "Configurações", m4, §14): o usuário só age no servidor
que gerencia agora, a escrita reautoriza sem cache, ``expected_version`` velho dá ``409`` com a versão atual e nada vaza."""

from unittest.mock import patch

import pytest

from tests.api.support import body_of, error_code
from tests.api.support_data import OLD_VERSION, OTHER, USER, DataApiCase

pytestmark = pytest.mark.critical

SECRET_FRAGMENTS = ("secret", "sessionkey", "session_key", "password", "senha", "authorization", "cookie", "token")
PLAYER = "/guilds/100/settings/player"
PLAYER_VALUES = {
    "default_player_volume": 60,
    "autoplay": True,
    "check_other_bots_in_vc": False,
    "enable_restrict_mode": False,
}
EXPORT = "/guilds/100/links/export"


def player_body(version, **changes):
    return {"expected_version": version, "values": {**PLAYER_VALUES, **changes}}


def key_names(node):
    """Todo nome de chave de um JSON, em qualquer profundidade."""
    if isinstance(node, dict):
        for name, value in node.items():
            yield name
            yield from key_names(value)
    elif isinstance(node, list):
        for item in node:
            yield from key_names(item)


def assert_no_secret_keys(response):
    names = [name.lower() for name in key_names(body_of(response))]
    assert not [n for n in names if any(part in n for part in SECRET_FRAGMENTS)], names


class GuildListTest(DataApiCase):
    def test_lista_so_os_servidores_do_login_com_bot_e_convites_dos_que_faltam(self):
        response = self.call("GET", "/guilds")

        guilds = {g["id"]: g for g in body_of(response)["guilds"]}
        assert response.code == 200 and set(guilds) == {"100", "200", "300", "600", "800"}  # o 500 não gerencia
        assert [g["bot_present"] for g in (guilds["100"], guilds["300"])] == [True, False]
        assert len(guilds["300"]["invites"]) == 2 and guilds["100"]["invites"][0]["bot_name"] == "Bot Dois"
        assert guilds["100"]["permissions"] == "8"
        assert_no_secret_keys(response)

    def test_detalhe_traz_so_id_nome_icone_e_os_bots_do_servidor(self):
        response = self.call("GET", "/guilds/800")

        body = body_of(response)
        assert response.code == 200 and (body["id"], body["name"]) == ("800", "Ômega")
        assert [bot["id"] for bot in body["bots"]] == ["1", "2"] and body["invites"] == []
        assert set(body["bots"][0]) == {"id", "name", "avatar_url"}

    def test_negado_e_inexistente_dao_o_mesmo_404(self):
        # 600: o OAuth diz administrador, mas agora é membro comum; 300: sem bot; 500: fora do login; resto: não existe
        answers = []
        for guild_id in ("600", "300", "500", "424242", "007"):
            response = self.call("GET", f"/guilds/{guild_id}")
            body = body_of(response)
            body["error"].pop("request_id")
            answers.append((response.code, body))

        assert answers == [(404, {"error": {"code": "not_found"}})] * 5

    def test_id_fora_do_formato_e_404_sem_chegar_ao_servico(self):
        for path in ("/guilds/abc", "/guilds/1" + "0" * 20, "/guilds/100/settings/outra", "/guilds/-1"):
            with self.subTest(path=path):
                response = self.call("GET", path)

                assert (response.code, error_code(response)) == (404, "not_found")


class GuildAuthorizationTest(DataApiCase):
    def lose_permission(self):
        member = self.world.g100.members[int(USER)]
        member.guild_permissions.administrator = False

    def test_outro_usuario_nao_le_nem_escreve_no_servidor_e_nada_e_gravado(self):
        outro = self.new_session(OTHER)
        version = self.version_of(PLAYER, "player")

        read = self.call("GET", PLAYER, session=outro)
        write = self.call("PUT", PLAYER, player_body(version), session=outro)

        assert [(r.code, error_code(r)) for r in (read, write)] == [(404, "not_found")] * 2
        assert self.db_writes == []

    def test_escrita_reconfere_agora_e_a_leitura_aceita_o_cache_de_30_s(self):
        version = self.version_of(PLAYER, "player")  # a leitura entra no cache
        self.lose_permission()

        cached_read = self.call("GET", PLAYER)
        write = self.call("PUT", PLAYER, player_body(version))
        after = self.call("GET", PLAYER)  # a negativa derrubou a entrada do cache

        assert cached_read.code == 200
        assert (write.code, error_code(write)) == (404, "not_found")
        assert after.code == 404 and self.db_writes == []

    def test_depois_dos_30_s_a_leitura_tambem_nega(self):
        self.call("GET", PLAYER)
        self.lose_permission()
        self.advance(31)

        assert self.call("GET", PLAYER).code == 404

    def test_servidor_negado_nao_diz_se_o_corpo_era_valido(self):
        outro = self.new_session(OTHER)

        invalid = self.call("PUT", PLAYER, {"values": "lixo"}, session=outro)

        assert (invalid.code, error_code(invalid)) == (404, "not_found")  # a autorização vem antes da validação


class SettingsReadTest(DataApiCase):
    def test_cada_categoria_traz_os_formularios_dela_sem_segredo(self):
        expected = {
            "player": ["player"],
            "dj": ["dj"],
            "appearance": ["skin_servidor", "skin_global"],
            "links": ["links"],
            "general": ["prefixo", "status_voz", "idioma", "ouvir_junto"],
        }
        for category, keys in expected.items():
            with self.subTest(category=category):
                response = self.call("GET", f"/guilds/100/settings/{category}")

                body = body_of(response)
                assert response.code == 200 and body["category"] == category and list(body["forms"]) == keys
                assert all(form["key"] == key and len(form["version"]) == 16 for key, form in body["forms"].items())
                assert_no_secret_keys(response)

    def test_volume_sai_como_numero_e_nao_como_texto(self):
        values = body_of(self.call("GET", PLAYER))["forms"]["player"]["values"]

        assert values["default_player_volume"] == 100 and type(values["default_player_volume"]) is int
        assert values["autoplay"] is False

    def test_cada_servidor_mostra_os_dados_dele(self):
        omega = body_of(self.call("GET", "/guilds/800/settings/player"))["forms"]["player"]["values"]

        assert (omega["default_player_volume"], omega["autoplay"]) == (60, True)


class SettingsWriteTest(DataApiCase):
    def test_grava_o_formulario_e_responde_com_o_estado_novo(self):
        version = self.version_of(PLAYER, "player")

        response = self.call("PUT", PLAYER, player_body(version))

        body = body_of(response)
        assert response.code == 200 and body["values"] == PLAYER_VALUES and body["version"] != version
        assert self.world.server_settings(100)["default_player_volume"] == 60
        assert self.world.server_settings(100)["autoplay"] is True
        assert self.world.p100.called("process_save_queue") == [{}]  # o efeito do painel genérico também roda

    def test_registra_a_acao_do_painel_com_o_mesmo_rotulo_do_generico_e_sem_o_usuario(self):
        version = self.version_of(PLAYER, "player")

        with patch("empiremuse.api.handlers_guilds.record_panel_action") as record:
            self.call("PUT", PLAYER, player_body(version))
            self.call("PUT", PLAYER, player_body(OLD_VERSION))

        assert [call.args[1:] for call in record.call_args_list] == [("player/player", 200), ("player/player", 409)]
        assert (
            all(call.args[0].user_id == USER for call in record.call_args_list) is True
        )  # o contexto, nunca um id solto

    def test_versao_velha_da_409_com_a_versao_atual_e_nao_grava(self):
        current = self.version_of(PLAYER, "player")

        response = self.call("PUT", PLAYER, player_body(OLD_VERSION))

        assert (response.code, error_code(response)) == (409, "conflict")
        assert body_of(response)["error"]["current_version"] == current
        assert self.db_writes == []

    def test_outro_escritor_no_meio_invalida_a_versao_da_leitura(self):
        version = self.version_of(PLAYER, "player")
        self.world.external_server_change(100, autoplay=True)  # um comando no Discord

        response = self.call("PUT", PLAYER, player_body(version))

        assert (response.code, error_code(response)) == (409, "conflict")

    def test_corpo_invalido_da_400_com_os_campos_e_nunca_type_error(self):
        version = self.version_of(PLAYER, "player")
        everything = player_body(version)
        cases = {
            "volume fora da faixa": (player_body(version, default_player_volume=7), ["default_player_volume"]),
            "volume em texto": (player_body(version, default_player_volume="60"), ["default_player_volume"]),
            "volume booleano": (player_body(version, default_player_volume=True), ["default_player_volume"]),
            "flag em texto": (player_body(version, autoplay="sim"), ["autoplay"]),
            "campo desconhecido": (player_body(version, token="x"), ["token"]),
            "values não é objeto": ({"expected_version": version, "values": [1]}, ["values"]),
            "campo que falta": (
                {"expected_version": version, "values": {"default_player_volume": 60}},
                ["autoplay", "check_other_bots_in_vc", "enable_restrict_mode"],
            ),
            "sem a versão": ({"values": everything["values"]}, ["expected_version"]),
        }
        for name, (body, fields) in cases.items():
            with self.subTest(name):
                response = self.call("PUT", PLAYER, body)

                assert (response.code, error_code(response)) == (400, "validation_failed")
                assert body_of(response)["error"]["fields"] == fields
        assert self.db_writes == []

    def test_user_id_e_servidor_do_corpo_sao_ignorados(self):
        version = self.version_of(PLAYER, "player")

        response = self.call("PUT", PLAYER, {**player_body(version), "user_id": OTHER, "guild_id": "800"})

        assert response.code == 200
        assert self.world.server_settings(100)["default_player_volume"] == 60
        assert self.world.server_settings(800)["default_player_volume"] == 60  # o do 800 já era 60: não foi tocado
        assert self.world.db.writes == [("servidor", 100)]

    def test_categoria_com_varios_formularios_exige_o_form_e_recusa_um_de_outra_categoria(self):
        path = "/guilds/100/settings/general"
        version = self.version_of(path, "prefixo")
        values = {"prefix": "%"}

        missing = self.call("PUT", path, {"expected_version": version, "values": values})
        foreign = self.call("PUT", path, {"form": "player", "expected_version": version, "values": values})
        ok = self.call("PUT", path, {"form": "prefixo", "expected_version": version, "values": values})

        assert [r.code for r in (missing, foreign, ok)] == [400, 400, 200]
        assert body_of(missing)["error"]["fields"] == ["form"] == body_of(foreign)["error"]["fields"]
        assert self.world.db.doc("global", 100)["prefix"] == "%"

    def test_dj_aparencia_e_status_de_voz(self):
        dj = self.call(
            "PUT",
            "/guilds/100/settings/dj",
            {
                "expected_version": self.version_of("/guilds/100/settings/dj", "dj"),
                "values": {"djroles": ["5002", "5003"]},
            },
        )
        bad_role = self.call(
            "PUT",
            "/guilds/100/settings/dj",
            {"expected_version": body_of(dj)["version"], "values": {"djroles": ["999"]}},
        )
        appearance = "/guilds/100/settings/appearance"
        skin = self.call(
            "PUT",
            appearance,
            {
                "form": "skin_servidor",
                "expected_version": self.version_of(appearance, "skin_servidor"),
                "values": {"skin": "neon", "static_skin": "green"},
            },
        )
        bad_skin = self.call(
            "PUT",
            appearance,
            {
                "form": "skin_servidor",
                "expected_version": body_of(skin)["version"],
                "values": {"skin": "inexistente", "static_skin": "green"},
            },
        )
        glob = self.call(
            "PUT",
            appearance,
            {
                "form": "skin_global",
                "expected_version": self.version_of(appearance, "skin_global"),
                "values": {"global_skin": True, "player_skin": None, "player_skin_static": "green"},
            },
        )
        general = "/guilds/100/settings/general"
        voice = self.call(
            "PUT",
            general,
            {
                "form": "status_voz",
                "expected_version": self.version_of(general, "status_voz"),
                "values": {"voice_status_permanent": True, "voice_channel_status": "{track.title}"},
            },
        )

        assert [r.code for r in (dj, bad_role, skin, bad_skin, glob, voice)] == [200, 400, 200, 400, 200, 200]
        assert body_of(bad_role)["error"]["fields"] == ["djroles"] and body_of(bad_skin)["error"]["fields"] == ["skin"]
        assert self.world.server_settings(100)["djroles"] == ["5002", "5003"]
        assert self.world.server_settings(100)["skin"] == "neon"
        assert body_of(glob)["values"]["player_skin"] == ""

    def test_remover_convite_de_ouvir_junto_exige_gerenciar_canais(self):
        general = "/guilds/800/settings/general"
        body = {"form": "ouvir_junto", "action": "remove", "values": {"canal": "123"}}
        ok = self.call("PUT", general, {**body, "expected_version": self.version_of(general, "ouvir_junto")})
        general_beta = "/guilds/200/settings/general"  # no 200 o usuário só tem Gerenciar Servidor
        forbidden = self.call(
            "PUT", general_beta, {**body, "expected_version": self.version_of(general_beta, "ouvir_junto")}
        )

        assert ok.code == 200 and [i["canal_id"] for i in body_of(ok)["extras"]["itens"]] == ["456"]
        assert (forbidden.code, error_code(forbidden)) == (403, "forbidden")
        assert "123" in self.world.db.doc("global", 200)["listen_along_invites"]


class LinksTest(DataApiCase):
    LINKS = "/guilds/100/settings/links"

    def write(self, action, values, scope="jukebox", **extra):
        version = self.version_of(self.LINKS, "links")
        return self.call(
            "PUT",
            self.LINKS,
            {"expected_version": version, "action": action, "scope": scope, "values": values, **extra},
        )

    def test_adicionar_editar_remover_e_importar(self):
        item = {
            "nome": "Rock BR",
            "url": "https://example.com/rock",
            "descricao": "só o melhor",
            "generos": ["rock", "pop"],
            "ordem": None,
        }
        added = self.write("add", item)
        edited = self.write("edit", {**item, "alvo": "Rock BR", "nome": "Rock Brasil", "ordem": 1})
        imported = self.write(
            "import",
            {
                "dados": {
                    "playlists": [
                        {"name": "Rock Brasil", "url": "https://example.com/outra", "description": "x"},
                        {"name": "Jazz", "url": "https://example.com/jazz", "description": "suave"},
                    ]
                }
            },
        )
        removed = self.write("remove", {"alvo": "Jazz"}, scope="jukebox")

        assert [r.code for r in (added, edited, imported, removed)] == [200] * 4
        assert body_of(imported)["extras"]["relatorio"] == {"adicionadas": 1, "ignoradas": [{"nome": "Rock Brasil"}]}
        assert self.world.server_playlists(100) == ["Rock Brasil", "Fixo"]  # o "Fixo" veio do documento antigo

    def test_o_escopo_e_a_acao_sao_obrigatorios_e_validos(self):
        for extra, fields in (({"scope": "outro"}, ["scope"]), ({"action": "x"}, ["action"])):
            with self.subTest(fields=fields):
                body = {
                    "expected_version": self.version_of(self.LINKS, "links"),
                    "action": "remove",
                    "scope": "jukebox",
                    "values": {"alvo": "x"},
                    **extra,
                }

                response = self.call("PUT", self.LINKS, body)

                assert (response.code, body_of(response)["error"]["fields"]) == (400, fields)

    def test_regra_do_servico_vira_400_com_o_campo_e_versao_velha_409(self):
        item = {"nome": "Rock BR", "url": "ftp://x"}
        bad_url = self.write("add", item)
        unknown = self.write("remove", {"alvo": "nada"})
        stale = self.call(
            "PUT",
            self.LINKS,
            {"expected_version": OLD_VERSION, "action": "remove", "scope": "jukebox", "values": {"alvo": "x"}},
        )
        wrong_type = self.write("add", {**item, "generos": "rock"})

        assert body_of(bad_url)["error"]["fields"] == ["url"]
        assert body_of(unknown)["error"]["fields"] == ["action"]
        assert (stale.code, error_code(stale)) == (409, "conflict")
        assert body_of(wrong_type)["error"]["fields"] == ["generos"]

    def test_exportacao_so_para_quem_gerencia_e_com_escopo_valido(self):
        self.write("add", {"nome": "Rock BR", "url": "https://example.com/rock"})

        full = self.call("GET", EXPORT)
        scoped = self.call("GET", EXPORT + "?scope=radio")
        invalid = self.call("GET", EXPORT + "?scope=x")
        denied = self.call("GET", "/guilds/300/links/export")

        assert [p["name"] for p in body_of(full)["playlists"]] == ["Fixo", "Rock BR"]
        assert body_of(scoped)["playlists"] == []
        assert (invalid.code, body_of(invalid)["error"]["fields"]) == (400, ["scope"])
        assert (denied.code, error_code(denied)) == (404, "not_found")


class IdempotencyAndLimitsTest(DataApiCase):
    def test_mesma_chave_e_mesmo_corpo_grava_uma_vez_e_outro_corpo_ou_servidor_da_409(self):
        version = self.version_of(PLAYER, "player")
        first = self.call("PUT", PLAYER, player_body(version), key="chave-0001")
        again = self.call("PUT", PLAYER, player_body(version), key="chave-0001")
        other_body = self.call("PUT", PLAYER, player_body(version, default_player_volume=70), key="chave-0001")
        other_guild = self.call("PUT", "/guilds/800/settings/player", player_body(version), key="chave-0001")

        assert (first.code, again.code) == (200, 200) and body_of(again) == body_of(first)
        assert again.headers["Idempotent-Replayed"] == "true"
        assert [r.code for r in (other_body, other_guild)] == [409, 409]
        assert (
            self.world.db.writes == [("servidor", 100)]
            and self.world.server_settings(100)["default_player_volume"] == 60
        )

    def test_a_repeticao_tambem_passa_pela_autorizacao_fresca(self):
        version = self.version_of(PLAYER, "player")
        self.call("PUT", PLAYER, player_body(version), key="chave-0001")
        self.world.g100.members[int(USER)].guild_permissions.administrator = False

        again = self.call("PUT", PLAYER, player_body(version), key="chave-0001")

        assert (again.code, error_code(again)) == (404, "not_found")  # perdeu a permissão: nem a resposta guardada sai

    def test_sessao_que_acaba_durante_a_autorizacao_nao_escreve_nem_guarda_a_resposta(self):
        version = self.version_of(PLAYER, "player")
        authorize = self.ctx.authorizer.authorize

        async def authorize_then_erase(*args, **kwargs):
            access = await authorize(*args, **kwargs)
            self.store.destroy_user(USER)  # o apagamento (ou banimento) derruba a sessão no meio do pedido
            return access

        with patch.object(self.ctx.authorizer, "authorize", authorize_then_erase):
            response = self.call("PUT", PLAYER, player_body(version), key="chave-0001")
        claim = self.ctx.idempotency.claim(USER, "chave-0001", "qualquer-impressao")

        assert (response.code, error_code(response)) == (401, "session_expired")
        assert self.db_writes == [] and claim.replay is None and claim.ticket is not None  # a chave está livre

    def test_sessao_que_acaba_durante_a_escrita_responde_401_e_nao_guarda_a_resposta(self):
        service = self.ctx.settings_service
        submit = service.submit

        async def submit_then_erase(*args, **kwargs):
            outcome = await submit(*args, **kwargs)
            self.store.destroy_user(USER)  # a sessão acaba dentro do trabalho da escrita
            return outcome

        for key in ("chave-0001", None):  # com e sem Idempotency-Key
            with self.subTest(key=key):
                self.token = self.new_session(USER)  # a anterior caiu junto com o ``destroy_user``
                version = self.version_of(PLAYER, "player")
                with patch.object(service, "submit", submit_then_erase):
                    response = self.call("PUT", PLAYER, player_body(version), key=key)

                assert (response.code, error_code(response)) == (401, "session_expired")
        claim = self.ctx.idempotency.claim(USER, "chave-0001", "qualquer-impressao")
        assert claim.replay is None and claim.ticket is not None  # nada guardado: a chave está livre

    def test_escrita_20_por_minuto_por_usuario_somando_as_sessoes(self):
        version = self.version_of(PLAYER, "player")
        second = self.new_session(USER)
        self.advance(1)

        codes = [
            self.call(
                "PUT", PLAYER, player_body(version, default_player_volume=100), session=(self.token, second)[i % 2]
            ).code
            for i in range(21)
        ]

        assert codes[20] == 429 and 429 not in codes[:20]
        limited = self.call("PUT", PLAYER, player_body(version))
        assert (error_code(limited), 1 <= int(limited.headers["Retry-After"]) <= 60) == ("rate_limited", True)
        self.advance(61)
        assert self.call("GET", PLAYER).code == 200


ROUTES = [
    ("GET", "/account"),
    ("PATCH", "/account/prefix"),
    ("PATCH", "/account/rpc"),
    ("PATCH", "/account/integrations"),
    ("PATCH", "/account/lastfm"),
    ("GET", "/account/favorites"),
    ("PUT", "/account/favorites"),
    ("DELETE", "/account/favorites"),
    ("GET", "/account/favorites/export"),
    ("GET", "/account/radio-favorites"),
    ("DELETE", "/account/radio-favorites"),
    ("GET", "/guilds"),
    ("GET", "/guilds/100"),
    ("GET", "/guilds/100/settings/player"),
    ("PUT", "/guilds/100/settings/player"),
    ("GET", EXPORT),
]


class EveryRouteTest(DataApiCase):
    def test_sem_chave_sem_sessao_ou_com_sessao_invalida_nenhuma_rota_responde(self):
        for method, path in ROUTES:
            with self.subTest(method=method, path=path):
                no_key = self.api(
                    method, path, key=None, session=self.token, body={} if method in ("PUT", "PATCH") else None
                )
                no_session = self.api(method, path, body={} if method in ("PUT", "PATCH") else None)
                garbage = self.api(method, path, session="x" * 43, body={} if method in ("PUT", "PATCH") else None)

                assert (no_key.code, error_code(no_key)) == (401, "forbidden")
                assert [(r.code, error_code(r)) for r in (no_session, garbage)] == [(401, "session_expired")] * 2

    def test_cookie_e_ignorado(self):
        response = self.api("GET", "/account", headers={"Cookie": f"session={self.token}"})

        assert (response.code, error_code(response)) == (401, "session_expired")

    def test_respostas_de_sessao_levam_no_store_e_nosniff_inclusive_erro_e_exportacao(self):
        version = self.version_of(PLAYER, "player")
        responses = [
            self.call("GET", "/account"),
            self.call("GET", "/guilds"),
            self.call("GET", "/guilds/100"),
            self.call("GET", "/guilds/300"),  # 404
            self.call("GET", PLAYER),
            self.call("PUT", PLAYER, player_body(version)),
            self.call("PUT", PLAYER, player_body(OLD_VERSION)),  # 409
            self.call("GET", EXPORT),
            self.call("GET", "/account/favorites/export"),
        ]

        for response in responses:
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert "Set-Cookie" not in response.headers and "Access-Control-Allow-Origin" not in response.headers
        assert [r.code for r in responses] == [200, 200, 200, 404, 200, 200, 409, 200, 200]

    def test_metodo_fora_da_rota_da_405_no_envelope(self):
        response = self.call("POST", "/guilds")

        assert (response.code, error_code(response)) == (405, "validation_failed")
        assert body_of(response)["error"]["fields"] == ["method"]
