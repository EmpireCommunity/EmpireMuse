"""E4c — categoria `jukebox` (configurar / redefinir): acesso, página, VALIDAÇÕES e a rota antiga.

Contrato do formulário: `POST /painel/servidores/<gid>/jukebox` com `acao` ∈ configurar | confirmar_configurar |
reiniciar | confirmar_reiniciar; configurar: `modo` ∈ existente|criar, `canal`, `titulo_forum`, `tipo` ∈ texto|voz|palco,
`categoria`. Validação falha = 422 SÍNCRONO (nenhuma tarefa, nenhum efeito no Discord, nada no banco). Canal existente
(que não é fórum) = 200 com a confirmação (as mensagens dele vão embora, spec final do dono); fórum e canal novo = 202 +
tarefa direto (ver test_jukebox_tasks.py). A rota antiga `/canal` só redireciona (GET 301) e recusa POST (404).
"""

import html
import re
from urllib.parse import urlencode

from tests.panel.helpers_jukebox import GID, JukeboxBase
from tests.panel.helpers_settings import NOT_FOUND_TEXT, UID, acao_form
from tests.panel.test_settings_http import Each, HttpBase
from tests.support.canal_da_jukebox import FULL_TEXT, perms

TASK_PATH = re.compile(r"/painel/servidores/\d+/jukebox/tarefas/")


class JukeboxPageBase(JukeboxBase, HttpBase):
    def jukebox_url(self, bot=None, gid=GID):
        return f"/painel/servidores/{gid}/jukebox" + (f"?bot={bot}" if bot is not None else "")

    def body(self, browser, gid=GID, bot=None):
        response = browser.get(self.jukebox_url(bot, gid))
        assert response.code == 200, f"GET jukebox: {response.code}"
        return response.body.decode()

    def setup_form(self, browser):
        form = acao_form(self.body(browser), "configurar")
        assert form is not None, "a página deve oferecer o formulário acao=configurar"
        return form

    def configure(self, browser, htmx=False, **fields):
        """Envia o formulário de configurar como o navegador, com os campos dados (passo 1)."""
        form = self.setup_form(browser)
        return self.submit(browser, self.jukebox_url(), form, htmx=htmx, **fields)

    def confirm_setup(self, browser, response, htmx=True, **fields):
        """Passo 2: o formulário acao=confirmar_configurar que veio na resposta do passo 1."""
        form = acao_form(response.body.decode(), "confirmar_configurar")
        assert form is not None, "a confirmação deve trazer o formulário acao=confirmar_configurar"
        return self.submit(browser, self.jukebox_url(), form, htmx=htmx, **fields)

    def assert_nothing_happened(self, response=None, status=422):
        if response is not None:
            assert response.code == status, f"status {response.code}"
            assert not TASK_PATH.search(response.body.decode()), "validação não pode criar tarefa"
        assert list(self.world.j) == [], f"nenhum efeito esperado, houve: {self.world.j.names()}"


# ============================================================================ acesso


class JukeboxAccessTest(JukeboxPageBase):
    def test_anonymous_post_and_poll_do_nothing(self):
        browser = self.browser()
        token = self.xsrf_header(browser)
        for method, path in (
            ("POST", self.jukebox_url()),
            ("GET", f"/painel/servidores/{GID}/jukebox/tarefas/{'a' * 32}"),
        ):
            response = browser.request(
                path,
                method,
                headers={"Content-Type": "application/x-www-form-urlencoded", **token},
                **({"body": "acao=configurar"} if method == "POST" else {}),
            )
            assert response.code in (302, 401, 403), f"{method}: {response.code}"
        assert list(self.world.j) == []

    def test_post_without_xsrf_is_403_and_nothing_happens(self):
        browser = self.logged_in()
        form = self.setup_form(browser)
        pairs = [p for p in form.pairs(modo="existente", canal="22") if p[0] != "_xsrf"]
        assert self.submit(browser, self.jukebox_url(), form, pairs=pairs).code == 403
        self.assert_nothing_happened()

    def test_write_after_permission_revoked_is_404_inside_the_cache_window(self):
        browser = self.logged_in()
        form = self.setup_form(browser)
        member = self.world.guild.members[UID]
        member.guild_permissions.administrator = member.guild_permissions.manage_guild = False
        response = self.submit(browser, self.jukebox_url(), form, gap=2, modo="existente", canal="22")
        assert response.code == 404 and NOT_FOUND_TEXT in response.body.decode()
        assert list(self.world.j) == []


class LegacyRouteTest(JukeboxPageBase):
    """A rota antiga da categoria só aponta para a nova: nada é lido nem executado por ela."""

    def test_get_on_the_old_path_is_a_permanent_redirect_to_jukebox(self):
        browser = self.logged_in()
        for old, new in (
            (f"/painel/servidores/{GID}/canal", f"/painel/servidores/{GID}/jukebox"),
            (
                f"/painel/servidores/{GID}/canal/tarefas/{'a' * 32}",
                f"/painel/servidores/{GID}/jukebox/tarefas/{'a' * 32}",
            ),
        ):
            response = browser.get(old)
            assert response.code == 301 and response.headers.get("Location") == new, (old, response.code)
        assert browser.get(f"/painel/servidores/{GID}/canal/outra").code == 404

    def test_post_on_the_old_path_is_404_and_nothing_happens(self):
        browser = self.logged_in()
        form = self.setup_form(browser)
        headers = {"Content-Type": "application/x-www-form-urlencoded", **self.xsrf_header(browser)}
        body = urlencode(form.pairs(modo="existente", canal="22"))
        response = browser.post(f"/painel/servidores/{GID}/canal", body=body, headers=headers)
        assert response.code == 404
        self.assert_nothing_happened()


# ============================================================================ página


class JukeboxPageTest(JukeboxPageBase):
    def test_creation_options_stage_only_with_community_and_categories_from_the_cache(self):
        form = self.setup_form(self.logged_in())
        assert set(form.choices("tipo")) == {"texto", "voz"}
        assert set(form.choices("categoria")) >= {"66"} and "" in form.choices("categoria")

    def test_the_page_speaks_jukebox_and_has_no_purge_choice_nor_old_terms(self):
        body = self.body(self.logged_in())
        form = acao_form(body, "configurar")
        assert form.value("limpar_mensagens") is None and "limpar_mensagens" not in body
        assert "Jukebox" in body and 'href="/painel/servidores/100/jukebox"' in body
        for old in ("Canal de pedidos", "canal de pedidos", "estrita", "request_channel", "24/7"):
            assert old not in body, old


# ============================================================================ validações (422 síncrono, sem efeitos)


class JukeboxSetupValidationTest(JukeboxPageBase):
    def check_422(self, **fields):
        browser = self.logged_in()
        response = self.configure(browser, **fields)
        self.assert_nothing_happened(response)
        body = response.body.decode()
        assert 'aria-invalid="true"' in body, "campo inválido sem aria-invalid"
        for leak in ("Traceback", "GenericError", "repr("):
            assert leak not in body
        return body

    def test_discord_updates_and_rules_channels_are_refused(self):
        self.world.guild.public_updates_channel = self.world.text
        body = self.check_422(modo="existente", canal="22")
        assert "atualizações" in html.unescape(body)
        self.world.guild.public_updates_channel, self.world.guild.rules_channel = None, self.world.text
        self.world.j.clear()
        body = self.check_422(modo="existente", canal="22")
        assert "regras" in html.unescape(body)

    def test_non_admin_bot_needs_manage_permissions_on_an_existing_channel(self):
        self.world.text._perms = perms(**{k: v for k, v in FULL_TEXT.items() if k != "manage_permissions"})
        body = html.unescape(self.check_422(modo="existente", canal="22")).lower()
        assert "gerenciar permissões" in body or "administrador" in body

    def test_existing_channel_needs_purge_permissions_because_the_purge_is_always(self):
        self.world.text._perms = perms(**{k: v for k, v in FULL_TEXT.items() if k != "manage_messages"})
        body = html.unescape(self.check_422(modo="existente", canal="22"))
        assert "Gerenciar Mensagens" in body

    def test_forum_title_is_4_to_30_characters(self):
        def check(item):
            title, status = item
            self.world.j.clear()
            response = self.configure(self.logged_in(), modo="existente", canal="55", titulo_forum=title)
            assert response.code == status, f"{len(title)} caracteres: {response.code}"
            if status == 202:
                self.settle()  # drena a tarefa em segundo plano antes do caso seguinte
            if status == 422:
                self.assert_nothing_happened(response)
            self.world.forum.thread_list.clear()

        Each.run([("abc", 422), ("abcd", 202), ("a" * 30, 202), ("a" * 31, 422), ("", 422), ("   ", 422)], check)

    def test_stage_needs_community(self):
        self.check_422(modo="criar", tipo="palco")


class JukeboxCommunityTest(JukeboxPageBase):
    world_kwargs = {"features": ("COMMUNITY",)}

    def test_stage_is_offered_and_accepted_with_community(self):
        browser = self.logged_in()
        assert "palco" in self.setup_form(browser).choices("tipo")
        assert self.configure(browser, modo="criar", tipo="palco").code == 202
