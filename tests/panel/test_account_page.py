"""E5 — Conta do usuário: acesso, navegação, prefixo pessoal (U-01) e regras transversais (versão, XSRF, limites,
htmx, concorrência). Contrato: BRIEFING-PAINEL-E5.md; interpretações em .qa-e5-interp.txt.

`GET /painel/conta` e `POST` na mesma URL com `acao` ∈ prefixo | favorito_adicionar | favorito_editar |
favorito_remover | favoritos_importar | integracao_remover | integracoes_importar | lastfm_scrobble | rpc_token_gerar.
Os dados são SEMPRE os do usuário da sessão (um `user_id` vindo do formulário/URL é ignorado).
"""

from tests.panel.helpers_account import AccountBase, OTHER_UID
from tests.panel.helpers_login import DEFAULT_USER
from tests.panel.helpers_settings import UID, acao_form
from tests.panel.test_settings_http import HttpBase

URL = "/painel/conta"


class AccountCase(AccountBase, HttpBase):
    def body(self, browser):
        response = browser.get(URL)
        assert response.code == 200, f"GET {URL}: {response.code}"
        return response.body.decode()

    def form(self, browser, acao, alvo=None):
        form = acao_form(self.body(browser), acao, alvo)
        assert form is not None, f"a página deve oferecer o formulário acao={acao!r} alvo={alvo!r}"
        return form

    def act(self, browser, acao, *, alvo=None, htmx=False, gap=4, **overrides):
        form = self.form(browser, acao, alvo)
        return self.submit(browser, URL, form, htmx=htmx, gap=gap, **overrides)

    def other_user(self):
        """Segundo navegador, logado como o usuário 2002 (com o _xsrf do próprio navegador)."""
        self.fake.user = {**DEFAULT_USER, "id": str(OTHER_UID), "username": "outro", "global_name": "Outro"}
        browser = self.browser()
        assert self.login(browser).code == 302
        return browser

    def assert_untouched(self, uid=UID, before=None):
        assert self.world.doc(uid) == (before if before is not None else self.world.doc(uid))
        assert self.db.writes == [], f"nada deveria ser gravado: {self.db.writes}"


# ============================================================================ acesso e navegação


class AccountAccessTest(AccountCase):
    def test_each_user_sees_only_their_own_data(self):
        mine = self.body(self.logged_in())
        theirs = self.body(self.other_user())
        assert "Rock" in mine and "Jazz" not in mine
        assert "Jazz" in theirs and "Rock" not in theirs


# ============================================================================ prefixo pessoal (U-01)


class UserPrefixTest(AccountCase):
    def test_empty_prefix_removes_it_like_resetuserprefix(self):
        response = self.act(self.logged_in(), "prefixo", custom_prefix="")
        assert response.code == 200 and self.world.doc()["custom_prefix"] == ""
        assert self.pool.user_prefix_cache == {UID: ""}

    def test_length_limit_matches_the_command(self):
        browser = self.logged_in()
        for ok in ("a" * 5, "😀" * 5, "?"):
            assert self.act(browser, "prefixo", custom_prefix=ok).code == 200, ok
            assert self.world.doc()["custom_prefix"] == ok
        before = self.world.doc()
        for bad in ("a" * 6, "😀" * 6, "a" * 500):
            assert self.act(browser, "prefixo", custom_prefix=bad).code == 422, bad
        assert self.world.doc() == before

    def test_other_fields_posted_with_the_prefix_form_are_ignored(self):
        browser = self.logged_in()
        form = self.form(browser, "prefixo")
        before = self.world.doc()
        pairs = form.pairs(custom_prefix="$") + [
            ("token", "x" * 50),
            ("fav_links", "{}"),
            ("ver", "9.9"),
            ("lastfm", "x"),
            ("integration_links", "{}"),
        ]
        assert self.submit(browser, URL, form, pairs=pairs).code in (200, 422)
        after = self.world.doc()
        for key in ("token", "fav_links", "ver", "lastfm", "integration_links", "extra_flag", "last_tracks"):
            assert after[key] == before[key], key


# ============================================================================ regras transversais


class AccountProtocolTest(AccountCase):
    def test_version_is_independent_between_forms_of_the_page(self):
        browser = self.logged_in()
        first = self.form(browser, "prefixo").value("versao")
        self.world.set_doc(fav_links={"Novo": "https://example.com/n"})
        assert self.form(browser, "prefixo").value("versao") == first, "prefixo não depende de favoritos"

    def test_rate_limited_htmx_write_answers_429_with_the_form_fragment_and_retry_after(self):
        browser = self.logged_in()
        form = self.form(browser, "prefixo")
        for _ in range(20):
            self.submit(browser, URL, form, htmx=True, gap=0, custom_prefix="a b")
        blocked = self.submit(browser, URL, form, htmx=True, gap=0, custom_prefix="$")
        body = blocked.body.decode()
        assert blocked.code == 429 and blocked.headers.get("Retry-After") == "60" and self.db.writes == []
        assert "<html" not in body.lower() and 'name="custom_prefix"' in body

    def test_audit_names_user_action_and_fields_but_never_values(self):
        browser = self.logged_in()
        self.caplog.clear()
        assert self.act(browser, "prefixo", custom_prefix="Zq9!").code == 200
        text = self.audit_text()
        assert str(UID) in text and "prefixo" in text and "custom_prefix" in text, text
        assert "Zq9!" not in self.all_log_text()


# ============================================================================ sessão encerrada durante a escrita (BK-2)


class _ErasedWhileWaiting:
    """O lock do usuário visto por uma escrita que ficou na fila enquanto o apagamento rodava: quando ela enfim entra, as sessões
    do membro já foram destruídas."""

    def __init__(self, lock, erase):
        self._lock, self._erase = lock, erase

    async def __aenter__(self):
        self._erase()
        await self._lock.acquire()

    async def __aexit__(self, *exc):
        self._lock.release()


class ErasureWindowTest(AccountCase):
    def test_a_write_queued_behind_the_erasure_does_not_save_and_ends_the_session(self):
        from unittest.mock import patch

        browser = self.logged_in()
        form = self.form(browser, "prefixo")
        before = self.world.doc(UID)
        service = self.panel_app.settings["settings_service"]
        store = self.panel_app.settings["session_store"]
        queued = _ErasedWhileWaiting(service.lock("users", UID), lambda: store.destroy_user(str(UID)))

        with patch.object(service, "lock", lambda scope, user_id: queued):
            response = self.submit(browser, URL, form, custom_prefix="?")

        assert response.code == 401 and "Sessão encerrada" in response.body.decode()
        self.assert_untouched(before=before)
        assert browser.get(URL).code != 200  # o cookie da sessão morta não abre mais a conta
