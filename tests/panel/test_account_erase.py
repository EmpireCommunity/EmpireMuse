"""E3 — Apagar meus dados no painel (``/painel/conta/apagar``): T12, T20 e R3 do contrato ``apagar-dados-contrato.md`` §10.

O serviço de apagamento é o REAL (E1) sobre bancos TinyMongo em pasta temporária; os caminhos de erro (limite, ocupado, PARTIAL) usam um
dublê no lugar de ``pool.erasure``. A fase de recheck (60 s depois) roda na hora: ``sleep`` do serviço devolve sem esperar.
"""

import asyncio
import copy
import re
import shutil
import tempfile
from types import SimpleNamespace

import pytest
import tornado.web

from empiremuse.panel.handlers.settings import WRITE_LIMIT_PER_MINUTE
from empiremuse.panel.services.account_erase import AccountEraseService
from empiremuse.panel.handlers.servers import MAX_BODY_BYTES
from empiremuse.rules.erasure import ERASE_MAX_SESSION_AGE_S, ErasureReport, ErasureStatus, consent_is_recent
from empiremuse.utils.db import LocalDatabase, global_db_models
from empiremuse.utils.erasure import ErasureBusy, ErasureRateLimited, ErasureService, ErasureTombstones
from empiremuse.utils.user_locks import UserLocks
from tests.panel.helpers import require
from tests.panel.helpers_account import OTHER_UID, AccountWorld
from tests.panel.helpers_login import make_fake_oauth
from tests.panel.helpers_settings import UID, SettingsBase
from tests.panel.test_settings_http import HttpBase

pytestmark = pytest.mark.critical

URL = "/painel/conta/apagar"
LOGIN_LINK = "/painel/login?next=/painel/conta/apagar"
FORM_APAGAR = re.compile(r'<form id="form-apagar".*?</form>', re.DOTALL)
SESSION_COOKIE = "painel_sessao"


async def _no_wait(_seconds: float) -> None:
    return None


class EraseBase(HttpBase):
    """Painel com o AccountWorld e um ``pool.erasure`` real sobre dois bancos locais (``self.real_db`` e ``self.real_local``)."""

    def get_app(self):
        panel = require("empiremuse.panel")
        settings = require("empiremuse.panel.settings").load_panel_settings(self.config_factory())
        self.settings, self.world = settings, AccountWorld()
        self.pool, self.db, self.events = self.world.pool, self.world.db, self.world.journal
        self.tmp = tempfile.mkdtemp(prefix="erase-panel-")
        self.real_db = LocalDatabase(f"{self.tmp}/principal")
        self.real_local = LocalDatabase(f"{self.tmp}/local")
        self.pool.user_locks = UserLocks()
        self.pool.erasure_tombstones = ErasureTombstones()
        self.pool.lastfm_sessions, self.pool.user_prefix_cache = {}, {}
        self.pool.database, self.pool.local_database = self.real_db, self.real_local
        self.pool.erasure = ErasureService(self.pool, sleep=_no_wait, rpc_closer=lambda _uid: False)
        self.fake = make_fake_oauth(settings, guilds=self.world.raw)
        app = panel.build_panel_app(self.pool, settings, oauth_client=self.fake, clock=self.clock)
        return panel.make_router(tornado.web.Application([]), app)

    def setUp(self):
        super().setUp()
        for uid in (UID, OTHER_UID):
            self.io_loop.run_sync(lambda uid=uid: self.seed_user(uid))

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def seed_user(self, uid):
        doc = copy.deepcopy(global_db_models["users"])
        doc.update(custom_prefix=f"p{uid % 10}", fav_links={"Rock": "https://example.com/rock"})
        await self.real_db.update_data(uid, doc, db_name="users", collection="global")
        await self.real_local.update_data(uid, {"tracks": [{"query": "x"}]}, db_name="users", collection="scrobbles")

    def logged_in(self):
        """Login com consentimento (``next`` = tela de apagar, ``prompt=consent``): o único que libera apagar."""
        browser = self.browser()
        assert self.login(browser, next_=URL).code == 302
        return browser

    def silent_login(self, next_=None):
        browser = self.browser()
        assert self.login(browser, next_=next_).code == 302
        return browser

    def stored(self, uid):
        return (
            self.io_loop.run_sync(lambda: self.real_db.find_doc(uid, db_name="users", collection="global")) is not None
        )

    def stored_queue(self, uid):
        return (
            self.io_loop.run_sync(lambda: self.real_local.find_doc(uid, db_name="users", collection="scrobbles"))
            is not None
        )

    def post_erase(self, browser, *, confirmacao="APAGAR", extra=(), gap=4, query="", **kw):
        return self.post_raw(browser, URL + query, [("confirmacao", confirmacao), *extra], gap=gap, **kw)

    def session_alive(self, browser):
        return browser.get("/painel/conta").code == 200

    def use_stub(self, **handlers):
        """Troca ``pool.erasure`` por um dublê; ``calls`` guarda cada chamada (usuário, fonte, lock do usuário preso?)."""
        calls = []
        locks = self.pool.user_locks

        async def erase_user(user_id, *, source):
            calls.append((user_id, source, locks.lock(user_id).locked()))
            outcome = handlers["outcome"]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        self.pool.erasure = SimpleNamespace(erase_user=erase_user)
        return calls


# ============================================================================ T12: a porta do painel


class EraseGateTest(EraseBase):
    def test_get_shows_the_confirmation_form_with_no_store(self):
        response = self.logged_in().get(URL)
        body = response.body.decode()
        assert response.code == 200 and response.headers["Cache-Control"] == "no-store"
        form = FORM_APAGAR.search(body)  # o logout do topo também tem _xsrf: só vale o do formulário de apagar
        assert form is not None, "a página deve oferecer o formulário de apagar"
        fragment = form.group(0)
        assert 'name="_xsrf"' in fragment and 'method="post"' in fragment and f'action="{URL}"' in fragment
        assert 'name="confirmacao"' in fragment and "APAGAR" in fragment
        assert self.stored(UID)

    def test_get_lists_what_goes_and_what_stays_with_an_empty_field_and_no_counts(self):
        body = self.logged_in().get(URL).body.decode()
        assert (
            "O que será apagado" in body
            and "O que fica" in body
            and "4 semanas" in body
            and "Não dá para desfazer" in body
        )
        assert 'value=""' in body and "banimento" not in body
        assert "entrar de novo" not in body, "login recente: sem o aviso"

    def test_get_with_a_stale_login_shows_no_form_only_the_notice_and_the_login_link(self):
        browser = self.logged_in()
        self.clock.advance(ERASE_MAX_SESSION_AGE_S + 1)
        body = browser.get(URL).body.decode()
        assert FORM_APAGAR.search(body) is None and 'name="confirmacao"' not in body
        assert "Por segurança, você precisa entrar de novo com o Discord antes de apagar seus dados." in body
        assert "Nada é apagado até lá." in body and "Entrar de novo com o Discord" in body
        assert f'href="{LOGIN_LINK}"' in body
        assert f'href="{URL}"' in browser.get("/painel/conta").body.decode()

    def test_only_the_erase_path_asks_discord_for_consent_every_other_login_stays_silent(self):
        silent = (
            "/painel/conta",
            None,
            URL + "x",
            "//evil.example/painel/conta/apagar",
            "https://evil.example/painel/conta/apagar",
            "/painel/../painel/conta/apagar",
        )
        for next_, prompt in [(URL, "consent"), *[(other, "none") for other in silent]]:
            _, params = self.start_login(self.browser(), next_)
            assert params["prompt"] == prompt, next_

    def test_silent_login_inside_30_minutes_sees_the_link_not_the_form_and_cannot_erase(self):
        for next_ in (None, "/painel/conta"):
            browser = self.silent_login(next_)
            body = browser.get(URL).body.decode()
            assert FORM_APAGAR.search(body) is None and f'href="{LOGIN_LINK}"' in body, next_
            response = self.post_erase(browser)
            assert response.code == 303 and response.headers["Location"] == URL, next_
            assert self.stored(UID) and self.stored_queue(UID) and self.session_alive(browser)

    def test_silent_login_after_an_old_consent_does_not_bring_the_form_back(self):
        self.logged_in()  # consentimento de agora a pouco
        self.clock.advance(ERASE_MAX_SESSION_AGE_S + 1)
        browser = self.silent_login()
        assert FORM_APAGAR.search(browser.get(URL).body.decode()) is None
        assert self.post_erase(browser).code == 303 and self.stored(UID)

    def test_consented_at_is_written_only_by_a_consent_login_and_a_new_session_never_inherits_it(self):
        store = self.pool.panel_sessions
        self.logged_in()
        self.silent_login()
        flags = sorted(session.consented_at is not None for session in store._sessions.values())
        assert flags == [False, True]

    def test_a_tampered_next_at_the_callback_cannot_mark_consent(self):
        browser = self.browser()
        _, params = self.start_login(browser, "/painel/conta")  # o consentimento é decidido aqui, no início do fluxo
        assert self.callback(browser, params["state"], next="/painel/conta/apagar").code == 302
        assert FORM_APAGAR.search(browser.get(URL).body.decode()) is None
        assert self.post_erase(browser).code == 303 and self.stored(UID)

    def test_after_a_consent_login_the_form_works_even_if_a_silent_session_came_first(self):
        silent = self.silent_login()
        assert self.post_erase(silent).code == 303
        browser = self.logged_in()
        assert FORM_APAGAR.search(browser.get(URL).body.decode()) is not None
        assert self.post_erase(browser).code == 200 and not self.stored(UID)

    def test_anonymous_is_sent_to_login_and_nothing_is_erased(self):
        browser = self.browser()
        assert browser.get(URL).code == 302
        response = self.post_erase(browser)  # com um token XSRF válido: quem barra é a falta de sessão, não o XSRF
        assert response.code == 302 and response.headers["Location"].startswith("/painel/login")
        assert self.stored(UID) and self.stored(OTHER_UID)

    def test_post_without_xsrf_is_403_and_erases_nothing(self):
        browser = self.logged_in()
        response = self.post_erase(browser, token=False)
        assert response.code == 403 and response.headers["Cache-Control"] == "no-store"
        assert self.stored(UID) and self.session_alive(browser)

    def test_wrong_confirmation_erases_nothing(self):
        browser = self.logged_in()
        for typed in ("", "apagar", " APAGAR", "APAGAR ", "APAGAR!", "APAGA"):
            response = self.post_erase(browser, confirmacao=typed)
            assert response.code == 422, repr(typed)
            assert response.headers["Cache-Control"] == "no-store"
            assert 'name="confirmacao"' in response.body.decode()
        assert self.stored(UID) and self.stored_queue(UID) and self.session_alive(browser)

    def test_login_older_than_30_minutes_answers_303_to_the_page_and_erases_nothing(self):
        browser = self.logged_in()
        calls = self.use_stub(outcome=ErasureReport(ErasureStatus.OK, {}, ()))
        response = self.post_erase(browser, gap=ERASE_MAX_SESSION_AGE_S + 1)
        assert response.code == 303 and response.headers["Location"] == URL
        assert response.headers["Cache-Control"] == "no-store"
        assert calls == [] and self.stored(UID) and self.stored_queue(UID) and self.session_alive(browser)

    def test_body_over_the_limit_is_413_and_erases_nothing(self):
        browser = self.logged_in()
        calls = self.use_stub(outcome=ErasureReport(ErasureStatus.OK, {}, ()))
        response = self.post_erase(browser, extra=[("lixo", "x" * MAX_BODY_BYTES)])
        assert response.code == 413 and calls == [] and self.stored(UID) and self.session_alive(browser)

    def test_body_user_id_is_ignored_and_only_the_session_user_is_erased(self):
        browser = self.logged_in()
        response = self.post_erase(
            browser, extra=[("user_id", str(OTHER_UID)), ("id", str(OTHER_UID))], query=f"?user_id={OTHER_UID}"
        )
        assert response.code == 200
        assert not self.stored(UID) and self.stored(OTHER_UID) and self.stored_queue(OTHER_UID)

    def test_write_limiter_blocks_before_anything_is_checked_or_erased(self):
        browser = self.logged_in()
        calls = self.use_stub(outcome=ErasureReport(ErasureStatus.OK, {}, ()))
        for _ in range(WRITE_LIMIT_PER_MINUTE):
            self.post_erase(browser, confirmacao="x", gap=0)
        blocked = self.post_erase(browser, gap=0)
        assert blocked.code == 429 and blocked.headers["Retry-After"] == "60" and calls == []


# ============================================================================ resultados do serviço


class EraseOutcomesTest(EraseBase):
    def test_partial_clears_the_cookie_and_makes_no_promise_nor_shows_counts(self):
        browser = self.logged_in()
        report = ErasureReport(ErasureStatus.PARTIAL, {"users": 4242, "radios": 1}, ("radios",))
        self.use_stub(outcome=report)
        response = self.post_erase(browser)
        body = response.body.decode().lower()
        assert response.code == 200 and response.headers["Cache-Control"] == "no-store"
        assert "apagamos parte" in body and "4242" not in body and "radios" not in body
        assert "tente" not in body and "novamente" not in body, "PARTIAL não promete que repetir resolve"
        assert SESSION_COOKIE not in browser.cookies

    def test_unexpected_error_still_ends_the_session_and_clears_the_cookie(self):
        browser = self.logged_in()
        self.use_stub(outcome=RuntimeError("falha inesperada"))
        response = self.post_erase(browser)
        assert response.code == 500 and "falha inesperada" not in response.body.decode()
        assert SESSION_COOKIE not in browser.cookies and browser.get("/painel/conta").code == 302
        assert self.pool.panel_sessions.destroy_user(str(UID)) == 0, "a sessão do pedido foi destruída no store"

    def test_rate_limited_keeps_the_session_and_answers_429_with_retry_after(self):
        browser = self.logged_in()
        self.use_stub(outcome=ErasureRateLimited(1500))
        response = self.post_erase(browser)
        assert response.code == 429 and response.headers["Retry-After"] == "1500"
        assert "25 min" in response.body.decode() and self.session_alive(browser)

    def test_busy_keeps_the_session_and_answers_503(self):
        browser = self.logged_in()
        self.use_stub(outcome=ErasureBusy())
        response = self.post_erase(browser)
        assert response.code == 503 and response.headers["Retry-After"] == "60" and self.session_alive(browser)

    def test_panel_without_erasure_service_erases_nothing(self):
        browser = self.logged_in()
        del self.pool.erasure
        assert self.post_erase(browser).code == 503
        assert self.stored(UID) and self.session_alive(browser)

    def test_the_service_is_called_with_the_session_user_from_the_panel_and_without_the_user_lock(self):
        """R3: o serviço do painel NÃO segura o lock de usuário (``erase_user`` pega sozinho e não é reentrante)."""
        browser = self.logged_in()
        calls = self.use_stub(outcome=ErasureReport(ErasureStatus.OK, {}, ()))
        assert self.post_erase(browser).code == 200
        assert calls == [(UID, "panel", False)]


# ============================================================================ T20: fluxo completo e R3 de ponta a ponta


class EraseFlowTest(EraseBase):
    def test_login_then_erase_destroys_the_session_and_removes_the_data(self):
        browser = self.logged_in()
        other = self.other_browser()
        assert browser.get(URL).code == 200
        response = self.post_erase(
            browser
        )  # serviço real: se o painel segurasse o lock, o teste estouraria o tempo em vez de passar
        body = response.body.decode()
        assert response.code == 200 and "Seus dados foram apagados" in body and "Cancelar" not in body
        assert "usuario-nome" not in body, "a tela final sai deslogada"
        assert response.headers["Cache-Control"] == "no-store"
        cleared = [h for h in response.headers.get_list("Set-Cookie") if h.startswith(SESSION_COOKIE + "=")]
        assert cleared and cleared[0].startswith(f'{SESSION_COOKIE}="";'), cleared
        assert not self.stored(UID) and not self.stored_queue(UID)
        assert browser.get("/painel/conta").code == 302, "a sessão do pedido foi destruída"
        assert self.stored(OTHER_UID) and self.stored_queue(OTHER_UID) and other.get("/painel/conta").code == 200

    def other_browser(self):
        """Outro usuário (2002) logado: só a própria sessão deve sobreviver."""
        self.fake.user = {**self.fake.user, "id": str(OTHER_UID), "username": "outro"}
        browser = self.browser()
        assert self.login(browser).code == 302
        self.fake.user = {**self.fake.user, "id": str(UID), "username": "tester"}
        return browser

    def test_all_sessions_of_the_member_die_not_only_the_one_that_asked(self):
        first, second = self.logged_in(), self.logged_in()
        assert self.post_erase(first).code == 200
        assert second.get("/painel/conta").code == 302

    def test_second_run_after_new_login_is_ok_with_nothing_left(self):
        assert self.post_erase(self.logged_in()).code == 200
        again = self.post_erase(self.logged_in())
        assert again.code == 200 and "Seus dados foram apagados" in again.body.decode()

    def test_fresh_login_after_a_stale_redirect_completes_the_erasure(self):
        browser = self.logged_in()
        assert self.post_erase(browser, gap=ERASE_MAX_SESSION_AGE_S + 1).code == 303
        assert self.login(browser, next_="/painel/conta/apagar").headers["Location"] == URL
        assert self.post_erase(browser).code == 200 and not self.stored(UID)


# ============================================================================ regra pura de login recente


@pytest.mark.parametrize(
    ("age_s", "recent"),
    [(0, True), (ERASE_MAX_SESSION_AGE_S, True), (ERASE_MAX_SESSION_AGE_S + 1, False), (None, False)],
)
def test_login_with_consent_is_recent_up_to_30_minutes_and_a_silent_one_never_is(age_s, recent):
    consented_at = None if age_s is None else 5000.0 - age_s
    assert consent_is_recent(consented_at, 5000.0) is recent


def test_erase_user_that_takes_the_user_lock_itself_completes_through_the_service():
    """Direto no serviço: um ``erase_user`` que pega o lock (como o real) conclui, porque o serviço do painel não o segura antes."""

    async def scenario():
        pool = SimpleNamespace(user_locks=UserLocks())
        done = []

        async def erase_user(user_id, *, source):
            async with pool.user_locks.lock(user_id):  # como o ``erase_user`` real: pega o lock sozinho
                done.append((user_id, source))
            return ErasureReport(ErasureStatus.OK, {}, ())

        pool.erasure = SimpleNamespace(erase_user=erase_user)
        service = AccountEraseService(clock=lambda: 100.0)
        outcome = await asyncio.wait_for(service.erase(pool, user_id=7, consented_at=100.0, confirmation="APAGAR"), 2)
        return outcome, done

    outcome, done = asyncio.run(scenario())
    assert outcome.erased and done == [(7, "panel")]
