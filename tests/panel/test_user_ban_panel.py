"""E5 (R8.3): o painel recusa o usuário banido no login e na sessão aberta (MJ-E3); login fechado com a lista não carregada (D2).

Mesma infraestrutura do fluxo de login: Tornado de teste, OAuth e relógio falsos; o pool só leva a lista de banidos.
"""

from types import SimpleNamespace

import pytest
import tornado.web

from empiremuse.rules.user_ban import UserBanRecord
from empiremuse.utils.user_ban import UserBans
from tests.panel.helpers import local_config, require
from tests.panel.test_login_flow import LoginBase

pytestmark = pytest.mark.critical

USUARIO_ID = 1001  # o ``id`` do usuário padrão do dublê de OAuth é o TEXTO "1001" (``PanelUser.id`` é str)
OUTRO_ID = 2002
MOTIVO = "MOTIVO-SECRETO-DO-DONO"


def lista(*banidos, carregada=True):
    bans = UserBans(SimpleNamespace(), owners=frozenset({999}))
    bans._records = {uid: UserBanRecord(uid, 1, MOTIVO, 999) for uid in banidos}
    bans._loaded = carregada
    return bans


class PainelComLista(LoginBase):
    """``LoginBase`` com o pool levando ``user_bans`` (o ``fake_pool`` padrão não tem)."""

    banidos: tuple = ()
    carregada = True

    def get_app(self):
        config = self.config_factory()
        settings = require("empiremuse.panel.settings").load_panel_settings(config)
        self.fake = require("tests.panel.helpers_login").make_fake_oauth(settings, **self.fake_kwargs)
        self.bans = lista(*self.banidos, carregada=self.carregada)
        pool = SimpleNamespace(bots=[], database=None, user_bans=self.bans)
        self.settings = settings
        self.panel = require("empiremuse.panel").build_panel_app(
            pool, settings, oauth_client=self.fake, clock=self.clock
        )
        return require("empiremuse.panel").make_router(tornado.web.Application([]), self.panel)

    def sessoes(self):
        return self.panel.settings["session_store"]


class TestLoginDeBanido(PainelComLista):
    banidos = (USUARIO_ID,)

    def test_the_callback_refuses_a_banned_user_with_a_short_403_and_no_session(self):
        navegador = self.browser()

        resposta = self.login(navegador)

        assert resposta.code == 403
        corpo = resposta.body.decode()
        assert (
            MOTIVO not in corpo and "/apagar_meus_dados" in corpo
        )  # curto, sem motivo; o apagamento segue pelo bot (D1)
        assert not [
            h for h in resposta.headers.get_list("Set-Cookie") if h.startswith(self.session_cookie_name() + "=")
        ]
        assert self.sessoes().destroy_user("1001") == 0  # nenhuma sessão nasceu

    def test_the_banned_user_cannot_reach_the_erasure_page_either(self):
        navegador = self.browser()
        self.login(navegador)

        resposta = navegador.get("/painel/conta/apagar")

        assert resposta.code != 200  # sem isenção no painel (D1): o painel continua fechado

    def test_a_ban_during_the_login_is_caught_at_the_callback(self):
        self.bans._records = {}  # ainda não banido ao iniciar o login
        navegador = self.browser()
        _, params = self.start_login(navegador)
        self.bans._records = {USUARIO_ID: UserBanRecord(USUARIO_ID, 1, MOTIVO, 999)}  # banido com o login em voo

        resposta = self.callback(navegador, params["state"])

        assert resposta.code == 403


class TestSessaoAberta(PainelComLista):
    def test_an_open_session_turns_anonymous_on_the_next_request_after_the_ban(self):
        navegador = self.browser()
        assert self.login(navegador).code == 302
        assert "Tester Global" in self.index_html(navegador)  # logado

        self.bans._records = {USUARIO_ID: UserBanRecord(USUARIO_ID, 1, MOTIVO, 999)}
        html = self.index_html(navegador)

        assert "Tester Global" not in html  # anônimo na requisição seguinte
        assert self.sessoes().destroy_user("1001") == 0  # a sessão foi destruída no store

    def test_a_cleanup_listener_failure_does_not_keep_a_banned_session_alive(self):
        def broken(user_id):
            raise RuntimeError("falha")

        self.sessoes().add_user_listener(broken)  # ``destroy_user`` levanta ``UserCleanupError`` depois de destruir
        navegador = self.browser()
        assert self.login(navegador).code == 302

        self.bans._records = {USUARIO_ID: UserBanRecord(USUARIO_ID, 1, MOTIVO, 999)}
        html = self.index_html(navegador)

        assert "Tester Global" not in html  # o erro de limpeza é tolerado: a requisição segue anônima, sem 500
        self.sessoes()._user_listeners.clear()
        assert self.sessoes().destroy_user("1001") == 0  # as sessões já tinham caído

    def test_every_session_of_the_banned_user_dies_and_others_survive(self):
        dele, dela = self.browser(), self.browser()
        self.login(dele)
        self.fake.user = {**self.fake.user, "id": str(OUTRO_ID), "global_name": "Outra Pessoa"}
        self.login(dela)

        self.bans._records = {USUARIO_ID: UserBanRecord(USUARIO_ID, 1, MOTIVO, 999)}

        assert "Tester Global" not in self.index_html(dele)
        assert "Outra Pessoa" in self.index_html(dela)

    def test_an_unbanned_user_logs_in_again(self):
        self.bans._records = {USUARIO_ID: UserBanRecord(USUARIO_ID, 1, MOTIVO, 999)}
        assert self.login(self.browser()).code == 403

        self.bans._records = {}

        assert self.login(self.browser()).code == 302


class TestBancoForaNoBoot(PainelComLista):
    carregada = False  # D2: a lista não carregou

    def test_login_and_callback_are_closed_with_503_until_the_list_loads(self):
        navegador = self.browser()
        self.clock.advance(61)
        assert navegador.get("/painel/login").code == 503
        assert self.callback(navegador, "x").code == 503

        self.bans._loaded = True  # a nova tentativa carregou

        assert self.login(self.browser()).code == 302


class TestPoolSemLista(LoginBase):
    """Painel isolado (sem ``user_bans`` no pool, como nos demais testes do painel): o login segue como sempre."""

    def test_login_works_without_a_ban_list(self):
        assert self.login(self.browser()).code == 302
