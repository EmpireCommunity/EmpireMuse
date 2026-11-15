"""B3 · conta (contrato T-456 §4 "Conta", m4, m5, §14): só o usuário da sessão, ``expected_version`` em toda escrita, nenhum
segredo em resposta alguma e o token novo do RPC uma só vez."""

import asyncio
import json
from urllib.parse import urlencode

from unittest.mock import patch

import pytest

from empiremuse.api.server import API_PREFIX
from tests.api.helpers import SERVICE_KEY
from tests.api.support import body_of, error_code
from tests.api.support_data import (
    NODE,
    OLD_VERSION,
    OTHER,
    STATION,
    USER,
    DataApiCase,
)
from tests.panel.helpers_account import RPC_TOKEN_OLD, SESSIONKEY

pytestmark = pytest.mark.critical

PREFIX, LASTFM, RPC, FAVORITES, INTEGRATIONS, RADIOS = (
    "/account/prefix",
    "/account/lastfm",
    "/account/rpc",
    "/account/favorites",
    "/account/integrations",
    "/account/radio-favorites",
)


def prefix_body(version, value="?!", **extra):
    return {"expected_version": version, "values": {"custom_prefix": value}, **extra}


class AccountReadTest(DataApiCase):
    def test_lista_so_as_secoes_do_usuario_da_sessao_e_nenhum_segredo(self):
        self.world.link_lastfm()
        self.world.set_rpc_token()
        outro = self.new_session(OTHER)

        mine = self.call("GET", "/account")
        theirs = self.call("GET", "/account", session=outro)

        forms = body_of(mine)["forms"]
        assert set(forms) == {"prefixo", "favoritos", "radios", "integracoes", "lastfm", "rpc"}
        assert forms["prefixo"]["values"] == {"custom_prefix": "!"}
        assert forms["favoritos"]["extras"]["itens"] == [
            {"nome": "Rock", "url": "https://example.com/rock", "link": True}
        ]
        assert forms["lastfm"]["extras"] == {"vinculada": True, "usuario": "usuario_lfm"}
        assert forms["rpc"]["extras"]["configurado"] is True
        assert SESSIONKEY not in mine.body.decode() and RPC_TOKEN_OLD not in mine.body.decode()
        assert body_of(theirs)["forms"]["prefixo"]["values"] == {"custom_prefix": "?"}
        assert "Rock" not in theirs.body.decode()

    def test_exportacao_dos_favoritos_e_so_a_do_usuario(self):
        response = self.call("GET", "/account/favorites/export")

        assert (response.code, body_of(response)) == (200, {"Rock": "https://example.com/rock"})
        assert body_of(self.call("GET", "/account/favorites/export", session=self.new_session(OTHER))) == {
            "Jazz": "https://example.com/jazz"
        }


class PrefixWriteTest(DataApiCase):
    def test_grava_o_prefixo_do_usuario_da_sessao_e_ignora_user_id_do_corpo(self):
        version = self.version_of("/account", "prefixo")

        response = self.call("PATCH", PREFIX, prefix_body(version, user_id=OTHER, guild_id="100"))

        assert response.code == 200 and body_of(response)["values"] == {"custom_prefix": "?!"}
        assert self.world.user_doc()["custom_prefix"] == "?!"
        assert self.world.user_doc(int(OTHER))["custom_prefix"] == "?"  # o outro usuário não mudou
        assert self.world.user_doc()["extra_flag"] == "intacto"

    def test_versao_velha_da_409_com_a_versao_atual_e_nao_grava(self):
        response = self.call("PATCH", PREFIX, prefix_body(OLD_VERSION))

        assert (response.code, error_code(response)) == (409, "conflict")
        assert body_of(response)["error"]["current_version"] == self.version_of("/account", "prefixo")
        assert self.db_writes == []

    def test_corpo_invalido_da_validation_failed_com_os_campos_e_nunca_type_error(self):
        cases = [
            ({"values": {"custom_prefix": "x"}}, ["expected_version"]),
            ({"expected_version": 7, "values": {"custom_prefix": "x"}}, ["expected_version"]),
            ({"expected_version": "ZZ", "values": {"custom_prefix": "x"}}, ["expected_version"]),
            ({"expected_version": "VERSION"}, ["values"]),
            ({"expected_version": "VERSION", "values": {"custom_prefix": 5}}, ["custom_prefix"]),
            ({"expected_version": "VERSION", "values": {"custom_prefix": ["x"]}}, ["custom_prefix"]),
            ({"expected_version": "VERSION", "values": {"custom_prefix": "x", "token": "t"}}, ["token"]),
            ({"expected_version": "VERSION", "values": {"custom_prefix": "a b"}}, ["custom_prefix"]),
            ({"expected_version": "VERSION", "values": {"custom_prefix": "x" * 50}}, ["custom_prefix"]),
        ]
        version = self.version_of("/account", "prefixo")
        for body, fields in cases:
            with self.subTest(fields=fields, body=body):
                response = self.call("PATCH", PREFIX, json.loads(json.dumps(body).replace("VERSION", version)))

                assert (response.code, error_code(response)) == (400, "validation_failed")
                assert body_of(response)["error"]["fields"] == fields
        assert self.db_writes == []

    def test_content_type_fora_de_json_da_415(self):
        response = self.call("PATCH", PREFIX, raw_body="x", headers={"Content-Type": "text/plain"})

        assert (response.code, error_code(response)) == (415, "validation_failed")


class IdempotencyTest(DataApiCase):
    def test_mesma_chave_e_mesmo_corpo_repete_a_resposta_sem_gravar_de_novo(self):
        version = self.version_of("/account", "prefixo")

        first = self.call("PATCH", PREFIX, prefix_body(version), key="chave-0001")
        again = self.call("PATCH", PREFIX, prefix_body(version), key="chave-0001")

        assert (first.code, again.code) == (200, 200)
        assert body_of(again) == body_of(first)
        assert again.headers["Idempotent-Replayed"] == "true" and "Idempotent-Replayed" not in first.headers
        assert self.world.users.writes == [("global", 1001)]  # uma gravação só

    def test_mesma_chave_com_outro_corpo_da_409_e_nunca_devolve_a_resposta_anterior(self):
        version = self.version_of("/account", "prefixo")
        self.call("PATCH", PREFIX, prefix_body(version), key="chave-0001")

        other = self.call("PATCH", PREFIX, prefix_body(version, "zz"), key="chave-0001")

        assert (other.code, error_code(other)) == (409, "conflict")
        assert "current_version" not in body_of(other)["error"]
        assert self.world.user_doc()["custom_prefix"] == "?!"

    def test_mesma_chave_em_outra_rota_da_409(self):
        version = self.version_of("/account", "prefixo")
        self.call("PATCH", PREFIX, prefix_body(version), key="chave-0001")
        scrobble = {"expected_version": self.version_of("/account", "lastfm"), "values": {"scrobble": True}}

        response = self.call("PATCH", LASTFM, scrobble, key="chave-0001")

        assert (response.code, error_code(response)) == (409, "conflict")

    def test_a_chave_de_outro_usuario_nunca_devolve_a_resposta_alheia(self):
        outro = self.new_session(OTHER)
        version = self.version_of("/account", "prefixo")
        mine = self.call("PATCH", PREFIX, prefix_body(version, "aa"), key="chave-0001")
        other_version = body_of(self.call("GET", "/account", session=outro))["forms"]["prefixo"]["version"]

        theirs = self.call("PATCH", PREFIX, prefix_body(other_version, "bb"), session=outro, key="chave-0001")

        assert (mine.code, theirs.code) == (200, 200)
        assert body_of(theirs)["values"] == {"custom_prefix": "bb"}
        assert "Idempotent-Replayed" not in theirs.headers
        assert self.world.user_doc(int(OTHER))["custom_prefix"] == "bb"

    def test_erro_nao_fica_guardado_e_a_chave_vale_na_nova_tentativa(self):
        version = self.version_of("/account", "prefixo")
        bad = {"expected_version": version, "values": {"custom_prefix": "a b"}}
        assert self.call("PATCH", PREFIX, bad, key="chave-0001").code == 400

        retry = self.call("PATCH", PREFIX, bad, key="chave-0001")
        good = self.call("PATCH", PREFIX, prefix_body(version), key="chave-0002")

        assert retry.code == 400 and good.code == 200  # o erro foi refeito, não repetido de cache

    def test_apagar_os_dados_do_usuario_limpa_as_respostas_guardadas_so_dele(self):
        outro = self.new_session(OTHER)
        version = self.version_of("/account", "prefixo")
        other_version = body_of(self.call("GET", "/account", session=outro))["forms"]["prefixo"]["version"]
        self.call("PATCH", PREFIX, prefix_body(version), key="chave-0001")
        self.call("PATCH", PREFIX, prefix_body(other_version, "bb"), session=outro, key="chave-0001")

        self.store.destroy_user("1001")  # o gancho que o apagamento de dados já chama
        again = self.call("PATCH", PREFIX, prefix_body(version), session=self.new_session("1001"), key="chave-0001")
        other_again = self.call("PATCH", PREFIX, prefix_body(other_version, "bb"), session=outro, key="chave-0001")

        assert (again.code, error_code(again)) == (
            409,
            "conflict",
        ) and "Idempotent-Replayed" not in again.headers  # executou
        assert (
            other_again.code == 200 and other_again.headers["Idempotent-Replayed"] == "true"
        )  # o outro segue guardado

    def test_chave_fora_do_formato_da_400(self):
        response = self.call("PATCH", PREFIX, prefix_body(self.version_of("/account", "prefixo")), key="curta")

        assert (response.code, body_of(response)["error"]["fields"]) == (400, ["idempotency_key"])


class SignalingLock:
    """O lock do usuário que avisa (``arrived``) no instante em que a escrita o pede, antes de esperar por ele."""

    def __init__(self, lock, arrived):
        self._lock, self.arrived = lock, arrived

    async def __aenter__(self):
        self.arrived.set()  # síncrono até o ``acquire`` abaixo: quando o teste acorda, a escrita já está na fila do lock
        await self._lock.acquire()

    async def __aexit__(self, *exc):
        self._lock.release()


class ErasureWindowTest(DataApiCase):
    def test_escrita_que_esperava_o_lock_do_usuario_nao_grava_depois_do_apagamento(self):
        version = self.version_of("/account", "prefixo")
        service = self.ctx.settings_service
        real = service.lock("users", 1001)  # o lock que o apagamento e a escrita da conta dividem
        headers = {
            "Authorization": f"Bearer {SERVICE_KEY}",
            "X-EM-Session": self.token,
            "Content-Type": "application/json",
        }

        async def scenario():
            arrived = asyncio.Event()
            signaling = SignalingLock(real, arrived)
            await real.acquire()  # o apagamento está rodando
            with patch.object(service, "lock", lambda scope, user_id: signaling):
                pending = self.http_client.fetch(
                    self.get_url(API_PREFIX + PREFIX),
                    method="PATCH",
                    headers=headers,
                    body=json.dumps(prefix_body(version)),
                    raise_error=False,
                )
                await arrived.wait()  # a escrita passou pelo ``prepare`` e pediu o lock
            self.store.destroy_user("1001")  # o apagamento destrói as sessões
            real.release()
            return await pending

        response = self.io_loop.run_sync(scenario)

        assert (response.code, error_code(response)) == (401, "session_expired")
        assert self.world.user_doc()["custom_prefix"] == "!" and self.db_writes == []


class LastFmAndRpcTest(DataApiCase):
    def test_scrobble_liga_e_desliga_sem_tocar_na_chave_de_sessao_e_sem_vazar_segredo(self):
        self.world.link_lastfm()
        version = self.version_of("/account", "lastfm")

        response = self.call("PATCH", LASTFM, {"expected_version": version, "values": {"scrobble": True}})

        assert response.code == 200 and body_of(response)["values"] == {"scrobble": True}
        assert SESSIONKEY not in response.body.decode()
        assert self.world.user_doc()["lastfm"]["sessionkey"] == SESSIONKEY
        assert self.world.user_doc()["lastfm"]["scrobble"] is True

    def test_sem_conta_vinculada_recusa_e_o_vinculo_nao_existe_na_api(self):
        version = self.version_of("/account", "lastfm")

        response = self.call("PATCH", LASTFM, {"expected_version": version, "values": {"scrobble": True}})
        outro = self.call("PATCH", LASTFM, {"expected_version": version, "values": {"username": "x"}})

        assert (response.code, error_code(response)) == (400, "validation_failed")
        assert (outro.code, body_of(outro)["error"]["fields"]) == (400, ["scrobble", "username"])

    def test_token_novo_do_rpc_sai_uma_so_vez_com_no_store_e_o_antigo_nunca(self):
        self.world.set_rpc_token()
        version = self.version_of("/account", "rpc")

        issued = self.call("PATCH", RPC, {"expected_version": version}, key="chave-0001")
        listing = self.call("GET", "/account")

        token = body_of(issued)["token"]
        assert issued.code == 200 and len(token) >= 32 and issued.headers["Cache-Control"] == "no-store"
        assert RPC_TOKEN_OLD not in issued.body.decode()
        assert token not in listing.body.decode() and RPC_TOKEN_OLD not in listing.body.decode()
        assert self.world.user_doc()["token"] == token

    def test_a_chave_de_idempotencia_nao_repete_o_token_do_rpc(self):
        version = self.version_of("/account", "rpc")
        first = self.call("PATCH", RPC, {"expected_version": version}, key="chave-0001")
        version = self.version_of("/account", "rpc")  # ``configurado`` mudou: a versão é outra

        second = self.call("PATCH", RPC, {"expected_version": version}, key="chave-0001")

        assert (first.code, second.code) == (200, 200)
        assert body_of(first)["token"] != body_of(second)["token"] and "Idempotent-Replayed" not in second.headers

    def test_rpc_indisponivel_neste_bot_e_403(self):
        self.world.config["ENABLE_RPC_AUTH"] = False
        version = self.version_of("/account", "rpc")

        response = self.call("PATCH", RPC, {"expected_version": version})

        assert (response.code, error_code(response)) == (403, "forbidden")


class FavoritesAndIntegrationsTest(DataApiCase):
    def favorites_write(self, body):
        return self.call("PUT", FAVORITES, {"expected_version": self.version_of(FAVORITES, "favoritos"), **body})

    def favorite_removal(self, name, version=None):
        version = version or self.version_of(FAVORITES, "favoritos")
        return self.call("DELETE", f"{FAVORITES}?{urlencode({'name': name, 'expected_version': version})}")

    def test_adicionar_editar_remover_e_importar_favoritos(self):
        added = self.favorites_write({"action": "add", "values": {"nome": "Pop", "url": "https://example.com/pop"}})
        edited = self.favorites_write({"action": "edit", "values": {"nome": "Pop", "url": "https://example.com/p2"}})
        imported = self.favorites_write({"action": "import", "values": {"dados": {"Funk": "https://example.com/funk"}}})
        removed = self.favorite_removal("Rock")

        assert [r.code for r in (added, edited, imported, removed)] == [200] * 4
        assert self.world.user_doc()["fav_links"] == {
            "Pop": "https://example.com/p2",
            "Funk": "https://example.com/funk",
        }
        assert [item["nome"] for item in body_of(removed)["extras"]["itens"]] == ["Pop", "Funk"]

    def test_favorito_invalido_da_400_com_o_campo(self):
        cases = [
            ({"action": "add", "values": {"nome": "x", "url": "https://example.com/p"}}, ["nome"]),
            ({"action": "add", "values": {"nome": "Rock", "url": "https://example.com/p"}}, ["nome"]),
            ({"action": "add", "values": {"nome": "Pop", "url": "ftp://x"}}, ["url"]),
            ({"action": "edit", "values": {"nome": "Nao existe", "url": "https://example.com/p"}}, ["action"]),
            ({"action": "import", "values": {"dados": "texto"}}, ["dados"]),
            ({"action": "import", "values": {"dados": {"Pop": "ftp://x"}}}, ["dados"]),
            ({"action": "apagar", "values": {}}, ["action"]),
            ({"values": {}}, ["action"]),
        ]
        for body, fields in cases:
            with self.subTest(fields=fields, body=body):
                response = self.favorites_write(body)

                assert (response.code, body_of(response)["error"]["fields"]) == (400, fields)
        assert self.world.user_doc()["fav_links"] == {"Rock": "https://example.com/rock"}

    def test_remover_pela_query_exige_versao_e_nome_e_repetido_e_erro(self):
        stale = self.favorite_removal("Rock", OLD_VERSION)
        no_name = self.call("DELETE", f"{FAVORITES}?expected_version={self.version_of(FAVORITES, 'favoritos')}")
        twice = self.call("DELETE", f"{FAVORITES}?name=a&name=b&expected_version={OLD_VERSION}")

        assert (stale.code, error_code(stale)) == (409, "conflict")
        assert (no_name.code, body_of(no_name)["error"]["fields"]) == (400, ["name"])
        assert (twice.code, body_of(twice)["error"]["fields"]) == (400, ["name"])
        assert self.world.user_doc()["fav_links"] == {"Rock": "https://example.com/rock"}

    def test_versao_velha_nas_escritas_de_favoritos_da_409(self):
        response = self.call(
            "PUT",
            FAVORITES,
            {"expected_version": OLD_VERSION, "action": "import", "values": {"dados": {"A b": "https://e.com/a"}}},
        )

        assert (response.code, error_code(response)) == (409, "conflict")

    def test_integracoes_remover_e_importar(self):
        version = self.version_of("/account", "integracoes")
        imported = self.call(
            "PATCH",
            INTEGRATIONS,
            {"expected_version": version, "action": "import", "values": {"dados": {"Novo": "https://example.com/n"}}},
        )
        removed = self.call(
            "PATCH",
            INTEGRATIONS,
            {
                "expected_version": self.version_of("/account", "integracoes"),
                "action": "remove",
                "values": {"nome": "Meu canal"},
            },
        )

        assert (imported.code, removed.code) == (200, 200)
        assert list(self.world.user_doc()["integration_links"]) == ["Novo"]
        bad = self.call(
            "PATCH", INTEGRATIONS, {"expected_version": OLD_VERSION, "action": "remove", "values": {"nome": "X"}}
        )
        assert (bad.code, error_code(bad)) == (409, "conflict")


class RadioFavoritesTest(DataApiCase):
    def test_lista_e_remove_so_as_do_usuario_da_sessao(self):
        listing = body_of(self.call("GET", RADIOS))

        removed = self.call("DELETE", f"{RADIOS}?kind=station&id={STATION}&user_id={OTHER}")
        filtered = self.call("DELETE", f"{RADIOS}?kind=filter&id={NODE}")
        again = self.call("DELETE", f"{RADIOS}?kind=station&id={STATION}")

        assert [s["uuid"] for s in listing["extras"]["estacoes"]] == [STATION]
        assert (removed.code, filtered.code, again.code) == (200, 200, 200)  # remover é idempotente
        assert body_of(again)["extras"]["estacoes"] == [] and body_of(again)["extras"]["filtros"] == []
        assert self.world.favorites.station_ids[int(OTHER)] != []  # a lista do outro usuário não foi tocada

    @pytest.mark.regression
    def test_post_nao_devolve_lista_vazia_quando_a_releitura_falha_depois_de_adicionar(self):
        favorites = self.world.favorites
        original = favorites.add_station
        target = "dddddddd-dddd-dddd-dddd-dddddddddddd"

        async def add_then_fail_read(user_id, uuid):
            result = await original(user_id, uuid)
            favorites.broken = True
            return result

        favorites.add_station = add_then_fail_read
        response = self.call("POST", RADIOS, {"kind": "station", "id": target})

        assert (response.code, error_code(response)) == (503, "bot_unavailable")
        assert target in favorites.station_ids[int(USER)]  # a escrita ocorreu: a resposta não pode fingir lista vazia

    @pytest.mark.regression
    def test_post_ja_salva_antes_de_cheia_e_cheia_antes_de_consultar_o_diretorio(self):
        self.world.favorites.station_ids[int(USER)] = [
            STATION,
            *[f"{n:08x}-aaaa-aaaa-aaaa-aaaaaaaaaaaa" for n in range(23)],
        ]
        already = self.call("POST", RADIOS, {"kind": "station", "id": STATION})
        full = self.call("POST", RADIOS, {"kind": "station", "id": "cccccccc-cccc-cccc-cccc-cccccccccccc"})

        assert already.code == 200
        assert (full.code, error_code(full)) == (400, "validation_failed")
        assert body_of(full)["error"]["fields"] == ["limit"]

    def test_a_checagem_de_disponibilidade_nao_le_a_lista_do_usuario(self):
        reads = []
        original = self.world.favorites.stations

        async def counting(user_id):
            reads.append(user_id)
            return await original(user_id)

        self.world.favorites.stations = counting

        response = self.call("DELETE", f"{RADIOS}?kind=filter&id={NODE}")

        assert response.code == 200 and reads == [int(USER)]  # só a leitura da resposta; a checagem prévia não lê

    def test_corpo_invalido_da_400(self):
        for query in (f"kind=x&id={STATION}", "kind=station", f"id={STATION}", "kind=station&id="):
            with self.subTest(query=query):
                response = self.call("DELETE", f"{RADIOS}?{query}")

                assert (response.code, error_code(response)) == (400, "validation_failed")

    def test_banco_fora_do_ar_e_503_e_sem_a_funcao_ligada_tambem(self):
        self.world.favorites.broken = True
        down = self.call("DELETE", f"{RADIOS}?kind=filter&id={NODE}")
        listing = body_of(self.call("GET", RADIOS))
        self.world.pool.radio = None

        off = self.call("DELETE", f"{RADIOS}?kind=filter&id={NODE}")

        assert (down.code, error_code(down)) == (503, "bot_unavailable")
        assert listing["extras"]["indisponivel"] is True
        assert (off.code, error_code(off)) == (503, "bot_unavailable")
