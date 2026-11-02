"""E4c — categoria `canal` (G-06 setup / G-07 reset): acesso, página e VALIDAÇÕES (grupos 2 e 3 da PROPOSTA-E4c).

Contrato do formulário (interpretações em .qa-e4c-interp.txt): `POST /painel/servidores/<gid>/canal[?bot=]` com
`acao` ∈ configurar | reset | confirmar_reset; configurar: `modo` ∈ existente|criar, `canal`, `limpar_mensagens`,
`titulo_forum`, `tipo` ∈ texto|voz|palco, `categoria`. Validação falha = 422 SÍNCRONO (nenhuma tarefa, nenhum efeito
no Discord, nada no banco); sucesso = 202 + tarefa (ver test_channel_tasks.py).
"""

import html
import re

from tests.panel.helpers_channel import ChannelBase, GID
from tests.panel.helpers_settings import NOT_FOUND_TEXT, UID, acao_form
from tests.panel.test_settings_http import Each, HttpBase
from tests.support.jukebox import perms, FULL_TEXT

TASK_PATH = re.compile(r"/painel/servidores/\d+/canal/tarefas/")


class CanalBase(ChannelBase, HttpBase):
    def canal_url(self, bot=None, gid=GID):
        return f"/painel/servidores/{gid}/canal" + (f"?bot={bot}" if bot is not None else "")

    def body(self, browser, gid=GID, bot=None):
        response = browser.get(self.canal_url(bot, gid))
        assert response.code == 200, f"GET canal: {response.code}"
        return response.body.decode()

    def setup_form(self, browser):
        form = acao_form(self.body(browser), "configurar")
        assert form is not None, "a página deve oferecer o formulário acao=configurar"
        return form

    def configure(self, browser, htmx=False, **fields):
        """Envia o formulário de setup como o navegador, com os campos dados."""
        form = self.setup_form(browser)
        return self.submit(browser, self.canal_url(), form, htmx=htmx, **fields)

    def assert_nothing_happened(self, response=None, status=422):
        if response is not None:
            assert response.code == status, f"status {response.code}"
            assert not TASK_PATH.search(response.body.decode()), "validação não pode criar tarefa"
        assert list(self.world.j) == [], f"nenhum efeito esperado, houve: {self.world.j.names()}"


# ============================================================================ acesso


class CanalAccessTest(CanalBase):
    def test_anonymous_post_and_poll_do_nothing(self):
        browser = self.browser()
        token = self.xsrf_header(browser)
        for method, path in (("POST", self.canal_url()), ("GET", f"/painel/servidores/{GID}/canal/tarefas/{'a' * 32}")):
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
        assert self.submit(browser, self.canal_url(), form, pairs=pairs).code == 403
        self.assert_nothing_happened()

    def test_write_after_permission_revoked_is_404_inside_the_cache_window(self):
        browser = self.logged_in()
        form = self.setup_form(browser)
        member = self.world.guild.members[UID]
        member.guild_permissions.administrator = member.guild_permissions.manage_guild = False
        response = self.submit(browser, self.canal_url(), form, gap=2, modo="existente", canal="22")
        assert response.code == 404 and NOT_FOUND_TEXT in response.body.decode()
        assert list(self.world.j) == []


# ============================================================================ página


class CanalPageTest(CanalBase):
    def test_creation_options_stage_only_with_community_and_categories_from_the_cache(self):
        form = self.setup_form(self.logged_in())
        assert set(form.choices("tipo")) == {"texto", "voz"}
        assert set(form.choices("categoria")) >= {"66"} and "" in form.choices("categoria")


# ============================================================================ validações (422 síncrono, sem efeitos)


class CanalSetupValidationTest(CanalBase):
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


class CanalCommunityTest(CanalBase):
    world_kwargs = {"features": ("COMMUNITY",)}

    def test_stage_is_offered_and_accepted_with_community(self):
        browser = self.logged_in()
        assert "palco" in self.setup_form(browser).choices("tipo")
        assert self.configure(browser, modo="criar", tipo="palco").code == 202
