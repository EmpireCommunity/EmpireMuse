"""E4c — categoria `jukebox`: EXECUÇÃO de configurar/redefinir como tarefas com acompanhamento.

Contrato:
- POST válido de configurar (fórum ou canal novo) ou de confirmar_configurar/confirmar_reiniciar = 202 + bloco da tarefa:
  elemento com `data-estado` ∈ pendente|executando|ok|erro e link/hx-get para `/painel/servidores/<gid>/jukebox/tarefas/<id>`
  (id urlsafe de 32 caracteres); enquanto não final, `hx-trigger="every 2s"`; nos estados finais NÃO há hx-trigger.
- configurar num canal existente tem dois passos: `acao=configurar` devolve a confirmação (200, sem efeitos) com `token`
  assinado; `acao=confirmar_configurar` com o token cria a tarefa, que SEMPRE limpa as mensagens do canal (spec do dono).
- redefinir em dois passos: `acao=reiniciar` devolve a confirmação (200, sem efeitos); `acao=confirmar_reiniciar` cria a
  tarefa. O canal e as mensagens dos usuários ficam.
- O painel usa `apply_jukebox_channel(...)` e `jukebox_reset(...)` de utils/music/jukebox_channel.py com o default de
  `send_idle_embed` (os testes trocam esse default) e entrega cópias profundas dos dados (FakeDB audita).
"""

import asyncio
import html
import re
from urllib.parse import urlencode

from tests.panel.helpers import COOKIE_SECRET, OAUTH_SECRET
from tests.panel.helpers_jukebox import GID, PANEL_USER, TASK_ID
from tests.panel.helpers_login import DEFAULT_USER
from tests.panel.helpers_settings import NOT_FOUND_TEXT, UID, acao_form
from tests.panel.test_jukebox_page import JukeboxPageBase
from tests.support.canal_da_jukebox import FakePlayer

TASK_URL = re.compile(r"/painel/servidores/(\d+)/jukebox/tarefas/([A-Za-z0-9_-]{32})")
FINAL = ("ok", "erro")


def task_ref(body):
    match = TASK_URL.search(html.unescape(body))
    return match.group(0) if match else None


def estado(body):
    match = re.search(r'data-estado="([a-z]+)"', body)
    return match.group(1) if match else None


def polls_every_2s(body):
    return bool(re.search(r'hx-trigger="[^"]*every 2s[^"]*"', body)) and "hx-get=" in body


class TasksBase(JukeboxPageBase):
    def start(self, browser, htmx=True, **fields):
        """Passo 1 e, quando a resposta é a confirmação (canal existente), também o passo 2."""
        response = self.configure(browser, htmx=htmx, **fields)
        if response.code == 200 and acao_form(response.body.decode(), "confirmar_configurar") is not None:
            assert list(self.world.j) == [], "a confirmação não pode ter efeito no Discord"
            return self.confirm_setup(browser, response, htmx=htmx)
        return response

    def poll(self, browser, ref, htmx=True):
        return browser.get(ref, headers={"HX-Request": "true"} if htmx else {})

    def finish(self, browser, response, loops=40):
        """Deixa a tarefa andar e devolve (estado final, corpo) do último polling."""
        assert response.code == 202, f"POST deveria criar a tarefa (202), veio {response.code}"
        ref = task_ref(response.body.decode())
        assert ref, "a resposta 202 deve apontar para a tarefa"
        for _ in range(loops):
            self.settle(0.02)
            polled = self.poll(browser, ref)
            assert polled.code == 200, f"polling: {polled.code}"
            if estado(polled.body.decode()) in FINAL:
                return estado(polled.body.decode()), polled.body.decode()
        raise AssertionError("a tarefa não terminou")

    def run_setup(self, browser=None, **fields):
        browser = browser or self.logged_in()
        return self.finish(browser, self.start(browser, **fields))

    def reset_confirmation(self, browser, **fields):
        form = acao_form(self.body(browser), "reiniciar")
        assert form is not None, "página configurada deve oferecer o formulário acao=reiniciar"
        return self.submit(browser, self.jukebox_url(), form, htmx=True, **fields)

    def confirm_reset(self, browser, confirmation, **fields):
        form = acao_form(confirmation.body.decode(), "confirmar_reiniciar")
        assert form is not None, "a confirmação deve trazer o formulário acao=confirmar_reiniciar"
        return self.submit(browser, self.jukebox_url(), form, htmx=True, **fields)


# ============================================================================ configurar: dois passos e a limpeza


class SetupConfirmationTest(TasksBase):
    def test_existing_channel_answers_with_a_confirmation_and_nothing_happens_yet(self):
        browser = self.logged_in()
        response = self.configure(browser, htmx=True, modo="existente", canal="22")
        body = response.body.decode()
        assert response.code == 200 and not TASK_URL.search(body)
        form = acao_form(body, "confirmar_configurar")
        assert form is not None and form.value("token") and form.value("canal") == "22"
        assert "apagadas" in html.unescape(body)
        self.assert_nothing_happened()

    def test_confirming_purges_the_channel_and_configures(self):
        state, _ = self.run_setup(modo="existente", canal="22")
        assert state == "ok"
        assert self.world.j.of("channel.purge"), "configurar num canal existente sempre limpa as mensagens"
        assert self.world.jukebox_doc["channel"] == "22"

    def test_confirmation_token_is_single_use_and_bound_to_the_channel(self):
        browser = self.logged_in()
        confirmation = self.configure(browser, htmx=True, modo="existente", canal="22")
        state, _ = self.finish(browser, self.confirm_setup(browser, confirmation))
        assert state == "ok"
        again = self.confirm_setup(browser, confirmation)
        assert again.code in (409, 422), again.code
        other = self.confirm_setup(
            browser, self.configure(browser, htmx=True, modo="existente", canal="22"), canal="23"
        )
        assert other.code in (409, 422) and self.world.jukebox_doc["channel"] == "22"

    def test_new_channel_and_forum_need_no_confirmation(self):
        browser = self.logged_in()
        assert self.configure(browser, htmx=True, modo="criar", tipo="texto").code == 202
        self.settle(0.2)
        assert self.configure(browser, htmx=True, modo="existente", canal="55", titulo_forum="Pedidos").code == 202


# ============================================================================ tarefa de configurar: execução e efeitos


class SetupExecutionTest(TasksBase):
    def test_success_message_is_pt_br_and_has_no_exception_text(self):
        state, body = self.run_setup(modo="existente", canal="22")
        text = html.unescape(re.sub(r"<[^>]+>", " ", body))
        assert state == "ok" and "<#22>" not in body and "Traceback" not in text and "repr" not in text.lower()

    def test_forum_creates_a_post_with_the_given_title_and_saves_the_post_id(self):
        self.run_setup(modo="existente", canal="55", titulo_forum="Pedidos do bot")
        create = self.world.j.of("forum.create_thread")[0]
        assert create[2] == "Pedidos do bot" and create[4:] == (10080, 5)
        post = self.world.forum.created[0]
        assert self.world.jukebox_doc["channel"] == str(post.id)
        assert self.world.j.of("channel.purge") == [], "fórum não tem o que limpar"

    def test_active_player_is_moved_to_the_new_channel(self):
        player = FakePlayer(self.world.j, self.world.guild, self.world.lobby, static=False, message=None)
        self.world.bot.music.players[GID] = player
        self.run_setup(modo="existente", canal="22")
        assert player.static is True and player.text_channel is self.world.text


# ============================================================================ tarefa: estados, polling, acesso, limites


class TaskLifecycleTest(TasksBase):
    def test_post_returns_202_with_an_urlsafe_32_char_task_id_and_a_polling_block(self):
        self.world.gate = asyncio.Event()
        browser = self.logged_in()
        response = self.start(browser, modo="existente", canal="22")
        body = response.body.decode()
        assert response.code == 202 and "<html" not in body.lower()
        ref = task_ref(body)
        assert ref and TASK_ID.fullmatch(ref.rsplit("/", 1)[1])
        assert estado(body) in ("pendente", "executando") and polls_every_2s(body)
        assert html.unescape(re.search(r'hx-get="([^"]+)"', body).group(1)) == ref

    def test_task_poll_has_security_headers_and_no_store(self):
        browser = self.logged_in()
        ref = task_ref(self.start(browser, modo="existente", canal="22").body.decode())
        response = self.poll(browser, ref)
        assert response.headers.get("Cache-Control") == "no-store"
        assert response.headers.get("X-Content-Type-Options") == "nosniff"

    def test_another_user_cannot_read_the_task_even_with_the_id(self):
        browser = self.logged_in()
        ref = task_ref(self.start(browser, modo="existente", canal="22").body.decode())
        self.settle()
        self.fake.user = {**DEFAULT_USER, "id": "2002", "username": "outro", "global_name": "Outro"}
        self.world.guild.members[2002] = self.world.guild.members[UID]
        other = self.browser()
        assert self.login(other).code == 302
        response = other.get(ref, headers={"HX-Request": "true"})
        assert response.code == 404 and NOT_FOUND_TEXT in response.body.decode()
        assert browser.get(ref).code == 200, "o dono continua vendo"

    def test_poll_is_404_after_the_owner_lost_permission(self):
        browser = self.logged_in()
        ref = task_ref(self.start(browser, modo="existente", canal="22").body.decode())
        self.settle()
        member = self.world.guild.members[UID]
        member.guild_permissions.administrator = member.guild_permissions.manage_guild = False
        self.clock.advance(31)
        assert browser.get(ref).code == 404


class TaskConcurrencyTest(TasksBase):
    def test_two_simultaneous_setups_one_is_accepted_and_one_conflicts(self):
        browser = self.logged_in()
        confirmations = [
            acao_form(
                self.configure(browser, htmx=True, modo="existente", canal=c).body.decode(), "confirmar_configurar"
            )
            for c in ("22", "23")
        ]
        self.clock.advance(4)
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "HX-Request": "true",
            "Cookie": "; ".join(f"{k}={v}" for k, v in browser.cookies.items()),
            **self.xsrf_header(browser),
        }
        bodies = [urlencode(form.pairs()) for form in confirmations]
        path = self.jukebox_url()

        async def both():
            return await asyncio.gather(
                *[
                    self.http_client.fetch(
                        self.get_url(path),
                        method="POST",
                        headers=headers,
                        body=b,
                        raise_error=False,
                        follow_redirects=False,
                    )
                    for b in bodies
                ]
            )

        codes = sorted(r.code for r in self.io_loop.run_sync(both))
        assert codes == [202, 409], codes
        self.settle(0.2)
        assert len(self.db.writes) <= 1


# ============================================================================ redefinir: confirmação dupla, token


class ResetFlowTest(TasksBase):
    def test_reset_needs_manage_threads_up_front(self):
        self.world.configured_text()
        self.world.guild.me.guild_permissions = self.world.guild.me.guild_permissions.__class__(manage_threads=False)
        response = self.reset_confirmation(self.logged_in())
        assert response.code == 422 and "Gerenciar Tópicos" in html.unescape(response.body.decode())

    def test_confirmation_has_no_delete_channel_choice(self):
        self.world.configured_text()
        browser = self.logged_in()
        confirmation = self.reset_confirmation(browser)
        body = confirmation.body.decode()
        assert confirmation.code == 200 and "deletar" not in body and "confirmar_nome" not in body
        assert list(self.world.j) == []

    def test_reset_clears_the_section_and_keeps_the_channel(self):
        self.world.configured_text()
        browser = self.logged_in()
        state, _ = self.finish(browser, self.confirm_reset(browser, self.reset_confirmation(browser)))
        assert state == "ok"
        assert self.world.j.of("channel.delete") == [], "o canal e as mensagens dos usuários ficam"
        assert self.world.jukebox_doc["channel"] is None and self.world.jukebox_doc["message_id"] is None

    def test_forum_reset_clears_the_database(self):
        self.world.configured_forum()
        browser = self.logged_in()
        state, _ = self.finish(browser, self.confirm_reset(browser, self.reset_confirmation(browser)))
        assert state == "ok"
        assert self.world.jukebox_doc["channel"] is None and self.world.jukebox_doc["message_id"] is None
        assert self.world.bot_doc["player_controller"]["fav_links"], "o documento por bot nem é tocado"


class ResetTokenTest(TasksBase):
    def setUp(self):
        super().setUp()
        self.world.configured_text()

    def two_steps(self):
        browser = self.logged_in()
        return browser, self.reset_confirmation(browser)

    def test_token_is_single_use(self):
        browser, confirmation = self.two_steps()
        assert self.finish(browser, self.confirm_reset(browser, confirmation))[0] == "ok"
        self.world.configured_text()  # mesmo canal de volta: só o uso único impede o reuso
        again = self.confirm_reset(browser, confirmation)
        assert again.code in (409, 422), again.code
        assert len(self.db.writes) == 1


# ============================================================================ logs


class JukeboxAuditTest(TasksBase):
    def test_setup_audit_names_user_guild_bot_category_and_kind_but_no_values(self):
        self.world.text.name = "canal-SEGREDO"
        self.caplog.clear()
        browser = self.logged_in()
        self.finish(browser, self.start(browser, modo="existente", canal="55", titulo_forum="Titulo-SEGREDO-do-post"))
        audit = self.audit_text()
        for needle in (PANEL_USER, str(GID), "7000", "categoria=jukebox", "campos=configurar"):
            assert needle in audit, f"{needle!r} ausente da auditoria: {audit!r}"
        logs = self.all_log_text()
        for value in ("Titulo-SEGREDO", "canal-SEGREDO", OAUTH_SECRET, COOKIE_SECRET):
            assert value not in logs, value
