"""B9 · rádios favoritas, as rotas (contrato T-456 rev. 9 §4 ``[r9-fav]``, §14 ``[r9]``; ameaças §4 ``[r9-fav]`` k): BOLA nas listas do usuário, a porta de
admin da lista do servidor (G agora e o ator da sessão), tetos, formato e o mesmo ``404`` sem oráculo, premium só para ``custom``/``genre``, idempotência,
nenhum dado pessoal novo e o contrato (``openapi.yaml``) com todo status documentado observado."""

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from empiremuse.api.data import DataHandler
from empiremuse.rules.radio_station import shipped_catalog
from empiremuse.utils.radio.custom_favorites import CUSTOM_FAVORITES_DB, CustomFavorites
from empiremuse.utils.radio.directory import DirectoryError
from empiremuse.utils.radio.favorites import FAVORITES_DB, RadioFavorites
from empiremuse.utils.radio.premium import RadioUnavailable
from empiremuse.utils.radio.user_favorites import UserRadioFavorites
from tests.api.support import body_of, error_code
from tests.api.support_radio import ALFA, BASE, LIVE_UUID, OTHER_UUID, OMEGA, RadioApiCase, station
from tests.api.test_openapi_contract import SpecChecker, documented, load_spec
from tests.persistence.test_radio_store import FakeDb

pytestmark = pytest.mark.critical

API = "/api/v1"
G = "/guilds/{guild_id}"
FAV = f"{BASE}/radio-favorites"
SERVER, MINE = f"{FAV}/server", f"{FAV}/mine"
T_LIST, T_SERVER, T_MINE = f"{G}/radio-favorites", f"{G}/radio-favorites/server", f"{G}/radio-favorites/mine"
USER, OTHER_USER, MEMBER = "1001", "2002", "3003"
GENRE = "g:" + shipped_catalog()[0].id
FAILURE = RuntimeError("falha inesperada")
HOSTILE = "@everyone discord.gg/convite [x](https://evil.example) ‮drop"


async def _stale(premium):
    return premium


class FavoritesApiCase(RadioApiCase):
    spec = load_spec()
    seen: set = set()
    ran: set = set()

    def make_pool(self):
        pool = super().make_pool()
        self.db = FakeDb()
        store_pool = SimpleNamespace(database=self.db, get_all_bots=pool.get_all_bots)
        hub = self.radio
        hub.favorites = RadioFavorites(store_pool)
        hub.custom_favorites = CustomFavorites(store_pool)
        hub.user_favorites = UserRadioFavorites(store_pool, hub)
        hub.premium.fresh = (
            hub.premium.get
        )  # o premium AGORA (``fresh`` lê o armazém; os testes podem pôr um ``get`` velho)
        fake_get = hub.stations.get

        async def get(guild_id, station_id):  # o gênero do catálogo existe em qualquer servidor, como na store real
            if isinstance(station_id, str) and station_id.startswith("g:"):
                from empiremuse.utils.radio.stations import StationStore

                return StationStore.builtin(guild_id, station_id)
            return await fake_get(guild_id, station_id)

        hub.stations.get = get
        self.store_pool = store_pool
        return pool

    def setUp(self):
        super().setUp()
        self.ran.add(self._testMethodName)
        self.world.g100.add_member(int(MEMBER))  # membro comum: nem dono, nem Gerenciar Servidor
        self.member = self.new_session(MEMBER)
        self.stranger = self.new_session("4004")

    def expect(self, method, template, response, status, code=None):
        assert response.code == status, response.body
        if code is not None:
            assert error_code(response) == code
        self.seen.add(SpecChecker(self.spec).check(method, API + template, response))

    def custom_station(self, guild=ALFA, name="Rock"):
        return self.radio.stations.seed(guild, name)

    def add(self, route, kind, ident, **extra):
        return self.call("POST", route, {"kind": kind, "id": ident}, **extra)

    def remove(self, route, kind, ident, **extra):
        return self.call("DELETE", f"{route}?kind={kind}&id={ident}", **extra)

    def calls_to_directory(self):
        return len(self.radio.directory.by_uuid_calls)


class RadioFavoritesRisk(FavoritesApiCase):
    # ------------------------------------------------------------------ 1. BOLA

    def test_dois_usuarios_no_mesmo_servidor_nunca_alteram_a_lista_um_do_outro(self):
        self.premium()
        mine = self.custom_station().id
        a = self.call("POST", MINE, {"kind": "custom", "id": mine, "user_id": OTHER_USER, "guild_id": str(OMEGA)})
        self.expect("POST", T_MINE, a, 200)

        b_before = body_of(self.call("GET", FAV, session=self.other))
        self.expect("GET", T_LIST, self.call("GET", FAV, session=self.other), 200)
        self.add(MINE, "custom", mine, session=self.other)
        self.remove(MINE, "custom", mine)  # o DELETE de A não toca a lista de B

        assert b_before["mine"]["items"] == []
        docs = self.db.tables[CUSTOM_FAVORITES_DB]
        assert set(docs) == {
            f"{ALFA}:{USER}",
            f"{ALFA}:{OTHER_USER}",
        }  # só as chaves montadas da sessão e do caminho (nunca o ``user_id``/``guild_id`` do corpo)
        assert docs[f"{ALFA}:{USER}"]["stations"] == [] and docs[f"{ALFA}:{OTHER_USER}"]["stations"] == [
            mine
        ]  # o DELETE de A só esvaziou a de A
        assert body_of(self.call("GET", FAV, session=self.other))["mine"]["total"] == 1
        assert body_of(self.call("GET", FAV))["mine"]["total"] == 0

    def test_a_chave_de_idempotencia_de_um_usuario_nao_devolve_a_resposta_do_outro(self):
        self.premium()
        mine = self.custom_station().id
        first = self.add(MINE, "custom", mine, key="chave-compartilhada")
        self.expect("POST", T_MINE, first, 200)

        other = self.add(
            MINE, "custom", mine, session=self.other, key="chave-compartilhada"
        )  # outro usuário: escopo próprio

        assert other.code == 200 and other.headers.get("Idempotent-Replayed") is None
        assert body_of(other)["total"] == 1

    def test_a_conta_ignora_user_id_do_corpo_e_a_lista_de_a_nunca_muda_a_de_b_com_a_loja_real(self):
        def listed(user):
            return [f.uuid for f in self.io_loop.run_sync(lambda: self.radio.user_favorites.stations(user))]

        self.radio.directory.stations[LIVE_UUID] = station(LIVE_UUID)
        response = self.call(
            "POST", "/account/radio-favorites", {"kind": "station", "id": LIVE_UUID, "user_id": OTHER_USER}
        )

        assert response.code == 200
        assert listed(int(USER)) == [LIVE_UUID] and listed(int(OTHER_USER)) == []

    # ------------------------------------------------------------------ 2. porta de admin

    def test_quem_nao_gerencia_recebe_o_mesmo_404_de_servidor_inexistente(self):
        self.premium()
        mine = self.custom_station().id
        for session in (self.member, self.stranger):
            responses = [
                (("GET", T_LIST), self.call("GET", FAV, session=session)),
                (("POST", T_SERVER), self.add(SERVER, "genre", GENRE, session=session)),
                (("DELETE", T_SERVER), self.remove(SERVER, "genre", GENRE, session=session)),
                (("POST", T_MINE), self.add(MINE, "custom", mine, session=session)),
                (("DELETE", T_MINE), self.remove(MINE, "custom", mine, session=session)),
            ]
            for (method, template), response in responses:
                self.expect(method, template, response, 404, "not_found")
        gone = self.call("GET", "/guilds/999/radio-favorites")
        assert (gone.code, error_code(gone)) == (404, "not_found")
        assert self.db.tables.get(FAVORITES_DB, {}) == {} and self.db.tables.get(CUSTOM_FAVORITES_DB, {}) == {}

    def test_quem_perde_o_cargo_tem_a_proxima_escrita_negada_mesmo_com_o_cache_de_30_s_valido(self):
        self.premium()
        self.expect("GET", T_LIST, self.call("GET", FAV, session=self.other), 200)  # enche o cache de leitura
        self.revoke(OTHER_USER)

        denied = self.add(SERVER, "genre", GENRE, session=self.other)

        assert (denied.code, error_code(denied)) == (404, "not_found")
        assert self.db.tables.get(FAVORITES_DB, {}) == {}

    def test_g_liberado_e_o_store_negando_e_403_o_ator_e_o_da_sessao_nunca_o_do_corpo(self):
        self.premium()
        with patch("empiremuse.utils.radio.favorites.can_manage_guild", return_value=False) as door:
            response = self.add(SERVER, "genre", GENRE, **{"headers": {"X-Ator": OTHER_USER}})

            self.expect("POST", T_SERVER, response, 403, "forbidden")
            removal = self.remove(SERVER, "genre", GENRE)
            self.expect("DELETE", T_SERVER, removal, 403, "forbidden")
        assert {call.args[2] for call in door.call_args_list} == {int(USER)}

    # ------------------------------------------------------------------ 3. tetos e diretório

    def test_a_lista_do_servidor_enche_com_23_ao_vivo_e_1_custom_e_a_seguinte_de_qualquer_kind_e_400_limit(self):
        self.premium()
        for n in range(23):
            uuid = f"{n:08d}-0000-0000-0000-000000000000"
            self.radio.directory.stations[uuid] = station(uuid, f"Rádio {n}")
            assert self.add(SERVER, "station", uuid).code == 200
            if n % 15 == 14:
                self.advance(61)  # o teto é de 20 escritas por minuto por usuário
        assert self.add(SERVER, "custom", self.custom_station().id).code == 200
        self.advance(61)
        self.radio.directory.by_uuid_calls.clear()

        live = self.add(SERVER, "station", LIVE_UUID)
        custom = self.add(SERVER, "custom", self.custom_station(name="Outra").id)
        genre = self.add(SERVER, "genre", GENRE)

        for response in (live, custom, genre):
            self.expect("POST", T_SERVER, response, 400, "validation_failed")
            assert body_of(response)["error"]["fields"] == ["limit"]
        assert self.calls_to_directory() == 0, "cheia: nada é consultado no diretório"
        assert body_of(self.call("GET", FAV))["server"]["total"] == 24

    def test_ja_esta_e_200_sem_o_diretorio_e_diretorio_fora_e_503_nunca_404(self):
        self.radio.directory.stations[LIVE_UUID] = station(LIVE_UUID)
        self.expect("POST", T_SERVER, self.add(SERVER, "station", LIVE_UUID), 200)
        self.radio.directory.by_uuid_calls.clear()

        again = self.add(SERVER, "station", LIVE_UUID)
        self.radio.directory.strict_error = DirectoryError("unavailable")
        down = self.add(SERVER, "station", OTHER_UUID)

        self.expect("POST", T_SERVER, again, 200)
        assert self.calls_to_directory() == 1  # só a adição NOVA (a repetida não foi ao diretório)
        self.expect("POST", T_SERVER, down, 503, "bot_unavailable")

    # ------------------------------------------------------------------ 4. formato e oráculo

    def test_kind_e_id_trocados_ou_na_rota_errada_sao_400_com_o_campo(self):
        self.premium()
        custom = self.custom_station().id
        cases = [
            (SERVER, "custom", LIVE_UUID, ["id"]),
            (SERVER, "custom", GENRE, ["id"]),
            (SERVER, "genre", custom, ["id"]),
            (SERVER, "station", custom, ["id"]),
            (SERVER, "filter", "node", ["kind"]),
            (MINE, "station", LIVE_UUID, ["kind"]),
            (MINE, "genre", "g:Rock Alternativo", ["id"]),
        ]
        for route, kind, ident, fields in cases:
            response = self.add(route, kind, ident)
            self.expect("POST", T_SERVER if route == SERVER else T_MINE, response, 400, "validation_failed")
            assert body_of(response)["error"]["fields"] == fields, (route, kind, ident)
        repeated = self.call("DELETE", f"{SERVER}?kind=station&kind=custom&id={LIVE_UUID}")
        self.expect("DELETE", T_SERVER, repeated, 400, "validation_failed")

    def test_custom_de_outro_servidor_e_genero_fora_do_catalogo_sao_o_mesmo_404_e_o_node_e_ignorado(self):
        self.premium()
        self.premium(OMEGA)
        foreign = self.custom_station(OMEGA, "De Ômega").id
        unknown_genre = "g:nao_existe_no_catalogo"
        self.radio.directory.stations[LIVE_UUID] = station(LIVE_UUID)

        for route, kind, ident in (
            (SERVER, "custom", foreign),
            (MINE, "custom", foreign),
            (SERVER, "genre", unknown_genre),
            (MINE, "genre", unknown_genre),
            (SERVER, "custom", "f" * 24),
        ):
            response = self.add(route, kind, ident)
            self.expect("POST", T_SERVER if route == SERVER else T_MINE, response, 404, "not_found")
        with_node = self.call("POST", SERVER, {"kind": "station", "id": LIVE_UUID, "node": "br-sp"})

        assert with_node.code == 200
        assert "br-sp" not in str(self.db.tables[FAVORITES_DB])  # o ``node`` nunca é gravado

    # ------------------------------------------------------------------ 5. premium

    def test_custom_e_genre_sem_premium_sao_403_mas_a_ao_vivo_listar_e_remover_seguem_200(self):
        custom = self.custom_station().id
        self.radio.directory.stations[LIVE_UUID] = station(LIVE_UUID)
        for route, template in ((SERVER, T_SERVER), (MINE, T_MINE)):
            for kind, ident in (("custom", custom), ("genre", GENRE)):
                self.expect("POST", template, self.add(route, kind, ident), 403, "forbidden")
        assert self.db.tables.get(FAVORITES_DB, {}) == {} and self.db.tables.get(CUSTOM_FAVORITES_DB, {}) == {}

        self.expect("POST", T_SERVER, self.add(SERVER, "station", LIVE_UUID), 200)
        self.expect("GET", T_LIST, self.call("GET", FAV), 200)
        self.expect("DELETE", T_SERVER, self.remove(SERVER, "station", LIVE_UUID), 200)
        self.expect(
            "DELETE", T_MINE, self.remove(MINE, "custom", custom), 200
        )  # quem perdeu o premium limpa a própria lista

    def test_o_premium_e_conferido_agora_sem_o_cache(self):
        self.premium()
        stale = self.radio.premium.docs[ALFA]
        self.radio.premium.docs.clear()  # revogado agora
        self.radio.premium.get = lambda guild_id: _stale(
            stale
        )  # o ``get`` em cache de 30 s ainda diria "premium"; só o ``fresh`` sabe

        response = self.add(SERVER, "genre", GENRE)

        self.expect("POST", T_SERVER, response, 403, "forbidden")

    # ------------------------------------------------------------------ 6. preservação e leitura

    def test_adicionar_um_tipo_preserva_o_outro_e_a_entrada_que_sumiu_sai_com_missing_e_e_removivel(self):
        self.premium()
        self.radio.directory.stations[LIVE_UUID] = station(LIVE_UUID, HOSTILE)
        station_id = self.custom_station(name="Rock").id
        self.add(SERVER, "custom", station_id)
        self.add(SERVER, "station", LIVE_UUID)
        self.add(SERVER, "genre", GENRE)
        del self.radio.stations.docs[station_id]  # apagada no servidor

        listed = body_of(self.call("GET", FAV))["server"]

        by_id = {item["id"]: item for item in listed["items"]}
        assert [item["kind"] for item in listed["items"]] == ["station", "custom", "genre"]
        assert by_id[station_id]["missing"] is True and by_id[station_id]["name"] is None
        assert by_id[GENRE]["name"] is None and "missing" not in by_id[GENRE]
        assert (
            "@everyone" not in by_id[LIVE_UUID]["name"]
            and "](" not in by_id[LIVE_UUID]["name"]
            and "‮" not in by_id[LIVE_UUID]["name"]
        )
        removed = self.remove(SERVER, "custom", station_id)
        assert removed.code == 200 and body_of(removed)["total"] == 2

    # ------------------------------------------------------------------ 7. idempotência

    def test_mesma_chave_mesmo_pedido_repete_outro_corpo_ou_rota_e_409_e_delete_repetido_e_200(self):
        self.premium()
        self.radio.directory.stations[LIVE_UUID] = station(LIVE_UUID)
        self.radio.directory.stations[OTHER_UUID] = station(OTHER_UUID, "Jazz FM")
        first = self.add(SERVER, "station", LIVE_UUID, key="chave-de-pedido")
        replay = self.add(SERVER, "station", LIVE_UUID, key="chave-de-pedido")

        self.expect("POST", T_SERVER, first, 200)
        assert replay.headers.get("Idempotent-Replayed") == "true" and body_of(replay) == body_of(first)
        self.expect("POST", T_SERVER, self.add(SERVER, "station", OTHER_UUID, key="chave-de-pedido"), 409, "conflict")
        self.expect("POST", T_MINE, self.add(MINE, "genre", GENRE, key="chave-de-pedido"), 409, "conflict")
        self.expect("DELETE", T_SERVER, self.remove(SERVER, "station", LIVE_UUID), 200)
        self.expect("DELETE", T_SERVER, self.remove(SERVER, "station", LIVE_UUID), 200)

    # ------------------------------------------------------------------ 8. sem dado pessoal novo

    def test_nenhum_id_de_usuario_no_documento_do_servidor_e_o_log_nao_leva_uuid_nome_nem_usuario(self):
        self.premium()
        self.radio.directory.stations[LIVE_UUID] = station(LIVE_UUID, "Rock FM")
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("empiremuse").debug("sentinela")  # garante ao menos um registro
            response = self.add(SERVER, "station", LIVE_UUID)
            self.add(SERVER, "genre", GENRE)
            self.remove(SERVER, "station", LIVE_UUID)

        text = "\n".join(logs.output)
        stored = str(self.db.tables[FAVORITES_DB])
        assert USER not in stored and "added_by" not in stored
        assert (
            text.count("sentinela") == 1
            and LIVE_UUID not in text
            and "Rock FM" not in text
            and GENRE not in text
            and USER not in text.replace("sentinela", "")
        )
        assert response.headers["Cache-Control"] == "no-store"

    # ------------------------------------------------------------------ 9. exclusão (E1 não muda)

    def test_o_que_a_api_grava_e_apagado_pela_exclusao_e_a_lista_do_servidor_nao_tem_o_usuario(self):
        from empiremuse.rules.erasure import custom_favorite_key_matches

        self.premium()
        self.add(MINE, "custom", self.custom_station().id)
        self.add(SERVER, "genre", GENRE)

        assert [key for key in self.db.tables[CUSTOM_FAVORITES_DB] if custom_favorite_key_matches(key, int(USER))] == [
            f"{ALFA}:{USER}"
        ]
        assert str(USER) not in str(self.db.tables[FAVORITES_DB])


class RadioFavoritesContract(FavoritesApiCase):
    """Todo par rota+status documentado das rotas novas é observado, e cada resposta valida contra o arquivo."""

    def routes(self):
        return ((SERVER, T_SERVER, True), (MINE, T_MINE, False))

    def test_contrato_das_escritas_e_da_leitura(self):
        self.premium()
        custom = self.custom_station().id
        self.radio.directory.stations[LIVE_UUID] = station(LIVE_UUID)
        self.expect("GET", T_LIST, self.call("GET", FAV), 200)
        for route, template, is_server in self.routes():
            kind, ident = ("station", LIVE_UUID) if is_server else ("custom", custom)
            self.expect("POST", template, self.add(route, kind, ident), 200)
            self.expect("DELETE", template, self.remove(route, kind, ident), 200)
            self.expect("POST", template, self.add(route, "kind-ruim", ident), 400, "validation_failed")
            self.expect("DELETE", template, self.call("DELETE", f"{route}?kind={kind}"), 400, "validation_failed")
            plain = self.call("POST", route, raw_body="x", headers={"Content-Type": "text/plain"})
            self.expect("POST", template, plain, 415, "validation_failed")
            self.expect("POST", template, self.add(route, kind, ident, session=self.stranger), 404, "not_found")
            self.expect("DELETE", template, self.remove(route, kind, ident, session=self.stranger), 404, "not_found")
            self.expect("POST", template, self.add(route, kind, ident, session="x" * 43), 401, "session_expired")
            self.expect(
                "DELETE",
                template,
                self.call("DELETE", f"{route}?kind={kind}&id={ident}", session="x" * 43),
                401,
                "session_expired",
            )
            self.call("POST", route, {"kind": kind, "id": ident}, key="chave-de-contrato")
            self.expect(
                "POST",
                template,
                self.call("POST", route, {"kind": kind, "id": ident, "x": 1}, key="chave-de-contrato"),
                409,
                "conflict",
            )
            self.call("DELETE", f"{route}?kind={kind}&id={ident}", key="chave-de-contrato-2")
            reused = self.call(
                "DELETE",
                f"{route}?kind={kind}&id={'f' * 24 if kind == 'custom' else OTHER_UUID}",
                key="chave-de-contrato-2",
            )
            self.expect("DELETE", template, reused, 409, "conflict")
            self.advance(61)  # 20 escritas por minuto
        self.expect("POST", T_SERVER, self.add(SERVER, "custom", custom, **{}), 200)
        self.expect("POST", T_SERVER, self.add(SERVER, "custom", "e" * 24), 404, "not_found")
        self.radio.premium.docs.clear()
        self.expect("POST", T_SERVER, self.add(SERVER, "genre", GENRE), 403, "forbidden")
        self.expect("POST", T_MINE, self.add(MINE, "genre", GENRE), 403, "forbidden")

    def test_contrato_503_500_e_429(self):
        self.premium()
        self.radio.directory.stations[LIVE_UUID] = station(LIVE_UUID)
        for route, template, is_server in self.routes():
            kind, ident = ("station", LIVE_UUID) if is_server else ("custom", self.custom_station().id)
            self.radio.store.fail_with = None
            self.radio.premium.fail_with = RadioUnavailable()
            self.expect("POST", template, self.add(route, kind, ident), 503, "bot_unavailable")
            self.radio.premium.fail_with = None
            self.db.fail = True  # o banco das favoritas fora do ar (DELETE e GET não leem o premium)
            self.expect("DELETE", template, self.remove(route, kind, ident), 503, "bot_unavailable")
            self.db.fail = False
            with patch.object(DataHandler, "guild_access", side_effect=FAILURE):
                self.expect("POST", template, self.add(route, kind, ident), 500, "bot_unavailable")
                self.expect("DELETE", template, self.remove(route, kind, ident), 500, "bot_unavailable")
            while self.ctx.limits.user_write.allow(USER):
                pass
            self.expect("POST", template, self.add(route, kind, ident), 429, "rate_limited")
            self.expect("DELETE", template, self.remove(route, kind, ident), 429, "rate_limited")
            self.advance(61)
        with patch.object(DataHandler, "guild_access", side_effect=FAILURE):
            self.expect("GET", T_LIST, self.call("GET", FAV), 500, "bot_unavailable")
        self.db.fail = True
        self.expect("GET", T_LIST, self.call("GET", FAV), 503, "bot_unavailable")
        self.db.fail = False
        self.expect("GET", T_LIST, self.call("GET", FAV, session="x" * 43), 401, "session_expired")
        self.expect("GET", T_LIST, self.call("GET", FAV, session=self.stranger), 404, "not_found")
        while self.ctx.limits.user_read.allow(USER):
            pass
        self.expect("GET", T_LIST, self.call("GET", FAV), 429, "rate_limited")

    def test_zz_todo_status_documentado_das_rotas_de_favoritas_foi_exercitado(self):
        casos = {name for name in dir(self) if name.startswith("test_") and not name.startswith("test_zz")}
        if not casos <= self.ran:
            pytest.skip("só vale com todos os casos da classe rodando (o ``-k`` deixa casos de fora)")
        faltando = {
            item for item in documented(self.spec) if "/guilds/{guild_id}/radio-favorites" in item[1]
        } - self.seen

        assert not faltando, f"documentados e nunca observados: {sorted(faltando)}"
