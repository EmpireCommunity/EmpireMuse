"""T-660: a visão de membro na API (contrato r10 §2 d-h; threat model T1-T12, CC1-CC4).

Servidor 100 (Alfa) com o bot 1. ``3003`` é membro comum (sem gerência): no login, o Discord devolve o servidor 100 sem permissão, então ele entra em
``member_guild_ids``. ``4004`` também o tem na lista, mas não é membro (o Discord responde ``NotFound``). Relógio falso, sem espera real.
"""

import asyncio
import inspect
import json
import logging
import re
from unittest.mock import patch

import pytest

from empiremuse.api import handlers_member
from empiremuse.api.handlers_member import MemberRoute, member_routes
from empiremuse.api.radio_base import RadioRoute
from empiremuse.panel.member_authz import MemberBudget
from empiremuse.utils.radio.custom_favorites import CUSTOM_FAVORITES_DB
from empiremuse.utils.radio.premium import RadioUnavailable
from tests.api.support import body_of, error_code
from tests.api.support_radio import ALFA
from tests.api.test_openapi_contract import SpecChecker, documented, load_spec  # noqa: F401
from tests.api.test_radio_favorites import GENRE, HOSTILE, MEMBER, FavoritesApiCase
from tests.panel.helpers_servers import FakeMember, raw_guild

pytestmark = pytest.mark.critical

API = "/api/v1"
GUILDS = "/member/guilds"
LIST = f"/member/guilds/{ALFA}/radio-favorites"
MINE = f"{LIST}/mine"
T_GUILDS = "/member/guilds"
T_LIST = "/member/guilds/{guild_id}/radio-favorites"
T_MINE = "/member/guilds/{guild_id}/radio-favorites/mine"
STRANGER = "4004"
FAILURE = RuntimeError("falha inesperada")


def without_request_id(response):
    body = body_of(response)
    body["error"].pop("request_id", None)
    return body


class MemberApiCase(FavoritesApiCase):
    spec = load_spec()
    seen: set = set()
    ran: set = set()

    def setUp(self):
        super().setUp()
        # orçamento folgado: os testes fazem muitas escritas sem o relógio andar (o T6 testa o orçamento de verdade)
        self.ctx.member_authorizer._budget = MemberBudget(self.clock, burst=1000, user_burst=1000)
        g100 = self.world.g100
        g100.fetchable[int(MEMBER)] = g100.members[
            int(MEMBER)
        ]  # o Discord confirma o membro (a escrita sempre pergunta)
        self.oauth.guilds = [
            raw_guild(ALFA, "Alfa", owner=False, permissions="0")
        ]  # sem gerência: vira ``member_guild_ids``
        self.mem = self.new_session(MEMBER)
        self.outsider = self.new_session(STRANGER)  # tem o id na lista, mas o Discord diz que não é membro
        self.oauth.guilds = self.world.raw

    def m(self, method, path, body=None, **extra):
        extra.setdefault("session", self.mem)
        return self.call(method, path, body, **extra)

    def add_mine(self, kind, ident, **extra):
        return self.m("POST", MINE, {"kind": kind, "id": ident}, **extra)

    def remove_mine(self, kind, ident, **extra):
        return self.m("DELETE", f"{MINE}?kind={kind}&id={ident}", **extra)

    def mine_doc(self, user=MEMBER):
        return self.db.tables.get(CUSTOM_FAVORITES_DB, {}).get(f"{ALFA}:{user}")

    def make_member(self, user):
        guild = self.world.g100
        guild.add_member(int(user))
        guild.fetchable[int(user)] = guild.members[int(user)]


class MemberContract(MemberApiCase):
    """Todo par rota+status documentado sob ``/member`` é observado, e cada resposta valida contra o arquivo."""

    def test_contrato_da_lista_de_servidores(self):
        listed = self.m("GET", GUILDS)
        self.expect("GET", T_GUILDS, listed, 200)
        assert body_of(listed) == {"guilds": [{"id": str(ALFA), "name": "Alfa", "icon": None}]}
        self.expect("GET", T_GUILDS, self.m("GET", GUILDS, session="x" * 43), 401, "session_expired")
        with patch.object(handlers_member, "ready_bots_with_guild", side_effect=FAILURE):
            self.expect("GET", T_GUILDS, self.m("GET", GUILDS), 500, "bot_unavailable")
        while self.ctx.limits.user_read.allow(MEMBER):
            pass
        self.expect("GET", T_GUILDS, self.m("GET", GUILDS), 429, "rate_limited")

    def test_contrato_da_leitura(self):
        self.premium()
        self.expect("GET", T_LIST, self.m("GET", LIST), 200)
        self.expect("GET", T_LIST, self.m("GET", LIST, session=self.outsider), 404, "not_found")
        self.expect("GET", T_LIST, self.m("GET", LIST, session="x" * 43), 401, "session_expired")
        self.radio.premium.fail_with = RadioUnavailable()
        self.expect("GET", T_LIST, self.m("GET", LIST), 503, "bot_unavailable")
        self.radio.premium.fail_with = None
        with patch.object(MemberRoute, "member_access", side_effect=FAILURE):
            self.expect("GET", T_LIST, self.m("GET", LIST), 500, "bot_unavailable")
        while self.ctx.limits.user_read.allow(MEMBER):
            pass
        self.expect("GET", T_LIST, self.m("GET", LIST), 429, "rate_limited")

    def test_contrato_das_escritas(self):
        self.premium()
        custom = self.custom_station().id
        self.expect("POST", T_MINE, self.add_mine("custom", custom), 200)
        self.expect("DELETE", T_MINE, self.remove_mine("custom", custom), 200)
        self.expect("POST", T_MINE, self.add_mine("station", custom), 400, "validation_failed")
        self.expect("DELETE", T_MINE, self.m("DELETE", f"{MINE}?kind=custom"), 400, "validation_failed")
        plain = self.m("POST", MINE, raw_body="x", headers={"Content-Type": "text/plain"})
        self.expect("POST", T_MINE, plain, 415, "validation_failed")
        self.expect("POST", T_MINE, self.add_mine("custom", custom, session=self.outsider), 404, "not_found")
        self.expect("DELETE", T_MINE, self.remove_mine("custom", custom, session=self.outsider), 404, "not_found")
        self.expect("POST", T_MINE, self.add_mine("custom", custom, session="x" * 43), 401, "session_expired")
        self.expect("DELETE", T_MINE, self.remove_mine("custom", custom, session="x" * 43), 401, "session_expired")
        self.add_mine("custom", custom, key="chave-de-membro")
        self.expect("POST", T_MINE, self.add_mine("genre", GENRE, key="chave-de-membro"), 409, "conflict")
        self.remove_mine("custom", custom, key="chave-de-membro-2")
        self.expect("DELETE", T_MINE, self.remove_mine("genre", GENRE, key="chave-de-membro-2"), 409, "conflict")
        self.advance(61)
        self.radio.premium.docs.clear()
        self.expect("POST", T_MINE, self.add_mine("genre", GENRE), 403, "forbidden")

    def test_contrato_503_500_e_429(self):
        self.premium()
        custom = self.custom_station().id
        self.radio.premium.fail_with = RadioUnavailable()
        self.expect("POST", T_MINE, self.add_mine("genre", GENRE), 503, "bot_unavailable")
        self.radio.premium.fail_with = None
        self.db.fail = True
        self.expect("DELETE", T_MINE, self.remove_mine("custom", custom), 503, "bot_unavailable")
        self.db.fail = False
        with patch.object(MemberRoute, "member_access", side_effect=FAILURE):
            self.expect("POST", T_MINE, self.add_mine("custom", custom), 500, "bot_unavailable")
            self.expect("DELETE", T_MINE, self.remove_mine("custom", custom), 500, "bot_unavailable")
        while self.ctx.limits.user_write.allow(MEMBER):
            pass
        self.expect("POST", T_MINE, self.add_mine("custom", custom), 429, "rate_limited")
        self.expect("DELETE", T_MINE, self.remove_mine("custom", custom), 429, "rate_limited")

    def test_zz_todo_status_documentado_de_membro_foi_exercitado(self):
        casos = {name for name in dir(self) if name.startswith("test_") and not name.startswith("test_zz")}
        if not casos <= self.ran:
            pytest.skip("só vale com todos os casos da classe rodando (o ``-k`` deixa casos de fora)")
        faltando = {item for item in documented(self.spec) if item[1].startswith(API + "/member/")} - self.seen
        assert not faltando, f"documentados e nunca observados: {sorted(faltando)}"


class MemberRisk(MemberApiCase):
    # ------------------------------------------------------------------ T1, CC1: membro nunca alcança o caminho de gerente

    def test_t1_para_o_membro_toda_rota_de_gerente_do_servidor_e_404(self):
        """Pela tabela de rotas do arquivo: todo método de ``/guilds/{guild_id}/**`` com a sessão de membro dá o mesmo ``404``."""
        params = {
            "guild_id": str(ALFA),
            "radio_id": f"1:{ALFA}",
            "station_id": "a" * 24,
            "category": "player",
            "task_id": "a" * 32,
        }
        checked = 0
        for method, path, status in sorted(documented(self.spec)):
            if status != "404" or not path.startswith(API + "/guilds/{guild_id}"):
                continue
            concrete = re.sub(r"\{(\w+)\}", lambda match: params.get(match.group(1), "x"), path)[len(API) :]
            response = self.m(method, concrete, {} if method in ("POST", "PUT", "PATCH") else None)
            assert response.code == 404, f"{method} {path}: {response.code} {response.body[:120]}"
            checked += 1
            self.advance(4)  # os baldes por usuário (20 escritas, 120 leituras por minuto)
        assert checked > 10

    def test_cc1_todo_handler_de_membro_e_memberroute_e_nunca_radioroute(self):
        for _, handler, _ in member_routes():
            assert issubclass(handler, MemberRoute) and not issubclass(handler, RadioRoute), handler
        assert "ctx.authorizer" not in inspect.getsource(
            handlers_member
        )  # nenhuma rota de membro lê a decisão de gerente

    def test_cc1_o_caminho_de_gerente_numa_rota_de_membro_falha_fechado(self):
        handler = MemberRoute.__new__(MemberRoute)
        with self.assertLogs("empiremuse.api.handlers_member", level="ERROR"):
            with pytest.raises(Exception) as raised:
                asyncio.run(handler.guild_access(str(ALFA), write=False))
        assert getattr(raised.value, "status_code", None) == 500

    def test_cc1_as_rotas_de_membro_passam_com_a_decisao_de_gerente_quebrada(self):
        self.premium()
        custom = self.custom_station().id

        class Exploding:
            def __getattr__(self, name):
                raise AssertionError("rota de membro tocou o GuildAuthorizer")

        object.__setattr__(self.ctx, "authorizer", Exploding())
        assert self.m("GET", GUILDS).code == 200
        assert self.m("GET", LIST).code == 200
        assert self.add_mine("custom", custom).code == 200
        assert self.remove_mine("custom", custom).code == 200

    def test_nao_existe_rota_de_servidor_sob_member(self):
        assert self.m("POST", f"{LIST}/server", {"kind": "station", "id": "a" * 24}).code == 404
        assert self.m("GET", f"/member/guilds/{ALFA}").code == 404

    # ------------------------------------------------------------------ T3: BOLA

    def test_t3_um_membro_nunca_toca_a_lista_do_outro_e_o_corpo_nao_escolhe_usuario_nem_servidor(self):
        self.premium()
        custom = self.custom_station().id
        self.make_member(STRANGER)

        assert (
            self.m("POST", MINE, {"kind": "custom", "id": custom, "user_id": STRANGER, "guild_id": "800"}).code == 200
        )
        self.remove_mine("custom", custom, session=self.outsider)  # o DELETE do outro não toca a lista deste

        assert self.mine_doc()["stations"] == [custom]
        assert not (self.mine_doc(STRANGER) or {}).get("stations")
        assert body_of(self.m("GET", LIST, session=self.outsider))["mine"]["total"] == 0

    def test_t3_a_custom_de_outro_servidor_e_igual_a_uma_que_nao_existe(self):
        self.premium()
        elsewhere = self.custom_station(guild=800).id
        other_server = self.add_mine("custom", elsewhere)
        unknown = self.add_mine("custom", "e" * 24)
        assert (other_server.code, unknown.code) == (404, 404)
        assert without_request_id(other_server) == without_request_id(unknown)
        assert self.mine_doc() is None

    def test_t3_a_chave_de_um_nao_repete_a_resposta_do_outro_e_quem_saiu_nao_recebe_a_guardada(self):
        self.premium()
        custom = self.custom_station().id
        assert self.add_mine("custom", custom, key="chave-compartilhada").code == 200
        self.make_member(STRANGER)

        theirs = self.add_mine("custom", custom, key="chave-compartilhada", session=self.outsider)
        assert theirs.code == 200 and "Idempotent-Replayed" not in theirs.headers

        del self.world.g100.fetchable[int(MEMBER)]  # saiu do servidor
        replay = self.add_mine("custom", custom, key="chave-compartilhada")
        assert replay.code == 404  # a autorização fresca vem antes do ``claim``

    # ------------------------------------------------------------------ T4: oráculo

    def test_t4_um_id_fora_das_listas_nao_toca_o_discord_e_todo_404_tem_o_mesmo_corpo(self):
        before = {guild: guild.calls["fetch_member"] for guild in (self.world.g100, self.world.g800a)}
        outside = self.m("GET", "/member/guilds/800/radio-favorites")
        malformed = self.m("GET", "/member/guilds/99999999999999999999/radio-favorites")
        not_member = self.m("GET", LIST, session=self.outsider)
        assert self.world.g800a.calls["fetch_member"] == before[self.world.g800a]
        assert self.world.g100.calls["fetch_member"] == before[self.world.g100] + 1  # só o item da própria lista
        bodies = {json.dumps(without_request_id(r), sort_keys=True) for r in (outside, malformed, not_member)}
        assert {r.code for r in (outside, malformed, not_member)} == {404} and len(bodies) == 1

    # ------------------------------------------------------------------ T5: campos fechados e texto hostil

    def test_t5_os_campos_sao_exatamente_os_do_contrato_e_o_texto_hostil_sai_inerte(self):
        self.premium()
        hostile = self.custom_station(name=HOSTILE)
        assert self.add_mine("custom", hostile.id).code == 200
        body = body_of(self.m("GET", LIST))

        assert set(body) == {"guild", "supporter", "server", "mine", "catalog"}
        assert set(body["guild"]) == {"id", "name", "icon"}
        assert set(body["catalog"]) == {"stations", "genres"}
        assert set(body["catalog"]["stations"][0]) == {"kind", "id", "name"}
        for name in (body["catalog"]["stations"][0]["name"], body["mine"]["items"][0]["name"]):
            assert "@everyone" not in name and "discord.gg/" not in name and "‮" not in name, name
        flat = json.dumps(body)
        for absent in ("updated_by", "artists", "playlists", "stream", "invite", "bots", "channel"):
            assert absent not in flat, absent

    def test_t5_o_icone_so_sai_no_formato_da_cdn(self):
        good = f"https://cdn.discordapp.com/icons/{ALFA}/{'a' * 32}.png?size=64"
        assert handlers_member.safe_icon(good) == good
        for bad in ("https://evil.example/i.png", f"https://cdn.discordapp.com/icons/{ALFA}/x.png?size=64", None):
            assert handlers_member.safe_icon(bad) is None

    # ------------------------------------------------------------------ T6: abuso

    def test_t6_a_lista_de_servidores_nunca_consulta_o_discord(self):
        guilds = (self.world.g100, self.world.g800a, self.world.g500)
        before = sum(guild.calls["fetch_member"] for guild in guilds)
        assert self.m("GET", GUILDS).code == 200
        assert sum(guild.calls["fetch_member"] for guild in guilds) == before

    def test_t6_orcamento_de_consultas_esgotado_e_503_e_nao_vira_404_guardado(self):
        self.premium()
        self.ctx.member_authorizer._budget = MemberBudget(self.clock, burst=1, per_second=1.0, user_burst=1000)
        assert self.add_mine("genre", GENRE).code == 200
        refused = self.add_mine("genre", GENRE)  # o balde do bot esgotou (entre todos os usuários)
        assert (refused.code, error_code(refused)) == (503, "bot_unavailable")
        self.advance(1)
        assert self.add_mine("genre", GENRE).code == 200  # nada de negativo guardado

    # ------------------------------------------------------------------ T12 e §2 f: Limitado a Apoiadores

    def test_t12_sem_apoiador_adicionar_e_403_sem_gravar_e_listar_e_remover_seguem(self):
        custom = self.custom_station().id
        added = self.add_mine("custom", custom)
        body = body_of(self.m("GET", LIST))

        assert (added.code, error_code(added)) == (403, "forbidden") and self.mine_doc() is None
        assert body["supporter"] is False and body["catalog"] == {"stations": [], "genres": []}
        assert self.remove_mine("custom", custom).code == 200

    # ------------------------------------------------------------------ limite e repetição

    def test_limite_de_24_repeticao_e_remover_ausente(self):
        self.premium()
        stations = [self.custom_station(name=f"E{i}").id for i in range(25)]
        for ident in stations[:24]:
            assert self.add_mine("custom", ident).code == 200
            self.advance(4)  # 20 escritas por minuto
        full = self.add_mine("custom", stations[24])
        assert (full.code, body_of(full)["error"].get("fields")) == (400, ["limit"])
        assert self.add_mine("custom", stations[0]).code == 200 and len(self.mine_doc()["stations"]) == 24
        assert self.remove_mine("genre", GENRE).code == 200

    # ------------------------------------------------------------------ T2 e T10: membro desatualizado e apagamento

    def test_t2_quem_saiu_escreve_negado_na_hora(self):
        self.premium()
        assert self.m("GET", LIST).code == 200
        del self.world.g100.fetchable[
            int(MEMBER)
        ]  # saiu; o evento ainda não chegou ao gateway (a escrita confia só na API)
        assert self.add_mine("genre", GENRE).code == 404

    def test_t2_pending_e_negado(self):
        person = FakeMember()
        person.pending = True
        self.world.g100.members[int(MEMBER)] = self.world.g100.fetchable[int(MEMBER)] = person
        assert self.m("GET", LIST).code == 404

    def test_t10_depois_do_apagamento_401_e_o_cache_de_membro_sem_o_usuario(self):
        self.premium()
        assert self.m("GET", LIST).code == 200
        assert any(key[0] == MEMBER for key in self.ctx.member_authorizer._cache)

        self.store.destroy_user(MEMBER)

        assert self.m("GET", LIST).code == 401
        assert not any(key[0] == MEMBER for key in self.ctx.member_authorizer._cache)

    # ------------------------------------------------------------------ T11: logs

    def test_t11_nenhum_log_junta_usuario_e_servidor(self):
        self.premium()
        self.world.g100.fetchable[int(STRANGER)] = asyncio.TimeoutError()
        with self.assertLogs(level="DEBUG") as logs:
            logging.getLogger("empiremuse.api.probe").debug("probe")
            self.add_mine("genre", GENRE)
            self.m("GET", LIST, session=self.outsider)  # 503 (o Discord não respondeu)
            self.m("GET", "/member/guilds/800/radio-favorites")
        for line in logs.output:
            assert not (MEMBER in line and str(ALFA) in line), line
            assert not (STRANGER in line and str(ALFA) in line), line

    # ------------------------------------------------------------------ revisão do PR 264

    def test_cm2_falha_do_store_dentro_da_escrita_sai_com_o_status_e_o_rotulo_no_registro_de_uso(self):
        """Crivo CM2: a falha vem do store (dentro do ``work``), não de um ``patch`` antes dele; o registro de uso leva o status e o rótulo fixo."""
        from empiremuse.api import handlers_radio_favorites

        self.premium()
        custom = self.custom_station().id
        recorded = []
        store = self.radio.custom_favorites
        for failure, status in ((RadioUnavailable(), 503), (RuntimeError("falha do store"), 500)):
            for verb, attr in (("POST", "add"), ("DELETE", "remove")):
                recorded.clear()

                async def broken(*args, failure=failure, **kwargs):
                    raise failure

                with (
                    patch.object(store, attr, broken),
                    patch.object(
                        handlers_radio_favorites,
                        "record_panel_action",
                        lambda usage, label, code: recorded.append(
                            (label, code, type(usage).__name__, usage.bot, usage.guild)
                        ),
                    ),
                ):
                    response = self.add_mine("custom", custom) if verb == "POST" else self.remove_mine("custom", custom)
                assert response.code == status, (verb, status, response.body)
                # CS5: o objeto de uso leva o par bot/servidor que a decisão usou (CS3), nunca o usuário
                assert recorded == [("radios/favoritas", status, "UsageScope", self.world.bot1, self.world.g100)], (
                    verb,
                    recorded,
                )
                self.advance(4)

    def test_as3_toda_rota_da_aplicacao_sob_member_e_memberroute(self):
        """Alicerce AS3: o invariante do CC1 percorre a TABELA da aplicação (nada de rota de membro fora de ``member_routes``)."""
        from empiremuse.api.server import API_PREFIX, build_api_app

        app = build_api_app(self.ctx)
        found = [
            rule.target
            for rule in app.wildcard_router.rules
            if getattr(rule.matcher, "regex", None) is not None
            and rule.matcher.regex.pattern.startswith(API_PREFIX + "/member")
        ]
        assert len(found) == len(member_routes())
        assert all(issubclass(handler, MemberRoute) and not issubclass(handler, RadioRoute) for handler in found)
