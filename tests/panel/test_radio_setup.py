"""Contrato-nomes-radios §4.3 · Rádios configuráveis pelo painel: criar, configurar (tipo, canal, bot reserva) e redefinir.

Caminhos de risco (ALTO, passada do Vigil): só premium (403 antes de qualquer chamada ao bot), id da rota de outro
servidor (404), allowlist ``kind``/``channel_id``/``bot_id`` com forma validada aqui e regra no bot (422 no campo), CAS em
``rev`` (409), recusas do bot traduzidas (403 ``manage_guild``, 422 por ``code``), redefinir em dois passos com token
assinado de uso único preso à rádio e à versão, e auditoria só com ids.
"""

import html
import re

from empiremuse.panel.services.base import CONFLICT_TEXT, SAVED_TEXT
from empiremuse.rules.errors import RuleError
from empiremuse.utils.radio.store import RadioInvalid
from tests.panel.helpers_radio import FORBIDDEN, make_channel, radio_state
from tests.panel.helpers_settings import parse_forms
from tests.panel.test_radio_page import ALFA, CHANNEL, OMEGA, RadioBase

VOICE, STAGE = 123, 321


class SetupBase(RadioBase):
    def get_app(self):
        app = super().get_app()
        self.world.channels[STAGE] = make_channel(STAGE, "Palco", ALFA)
        self.radio.setup.bots[ALFA] = ((2, "Bot Dois"), (3, "Bot Três"))
        self.radio.setup.channels[ALFA] = ((VOICE, "Sala <i>DJ</i>", "voice"), (STAGE, "Palco", "stage"))
        self.radio.setup.bots[OMEGA] = ((2, "Bot Dois"),)
        self.radio.setup.channels[OMEGA] = ((789, "Sala Ômega", "voice"),)
        return app

    def grant(self, gid=ALFA):
        self.radio.premium.grant(gid, active=0)

    def new_form(self, body):
        return next((f for f in parse_forms(body) if f.action and f.action.endswith("/radios/nova")), None)

    def config_form(self, body, radio):
        return next((f for f in parse_forms(body) if f.action and f.action.endswith(f"/{radio.id}/config")), None)

    def reset_form(self, body, radio, acao="reiniciar"):
        return next(
            (
                f
                for f in parse_forms(body)
                if f.action and f.action.endswith(f"/{radio.id}/reset") and f.value("acao") == acao
            ),
            None,
        )

    def create(self, browser, *, htmx=True, **fields):
        form = self.new_form(self.page(browser))
        assert form is not None, "a página premium deve ter o formulário de nova rádio"
        return self.submit(browser, self.url(), form, htmx=htmx, **fields)

    def configure(self, browser, radio, *, htmx=True, **fields):
        form = self.config_form(self.page(browser), radio)
        assert form is not None, "cada rádio deve ter o formulário de configuração"
        return self.submit(browser, self.url(), form, htmx=htmx, **fields)

    def reset_step_one(self, browser, radio, **fields):
        form = self.reset_form(self.page(browser), radio)
        assert form is not None, "cada rádio deve ter o formulário de redefinir"
        return self.submit(browser, self.url(), form, htmx=True, **fields)

    def reset_confirm(self, browser, radio, confirmation, **fields):
        form = self.reset_form(confirmation.body.decode(), radio, "confirmar_reiniciar")
        assert form is not None, "a confirmação deve trazer o formulário acao=confirmar_reiniciar"
        return self.submit(browser, self.url(), form, htmx=True, **fields)

    def seed(self, bot_id=1, gid=ALFA, channel=CHANNEL, **kwargs):
        radio = self.radio.store.seed(bot_id, gid, channel, **kwargs)
        self.radio.service.states[radio.id] = radio_state()
        return radio


# ============================================================================ página


class SetupPageTest(SetupBase):
    def test_without_premium_there_is_no_setup_form_and_post_is_403_before_the_bot(self):
        self.seed()
        browser = self.logged_in()
        body = self.page(browser)
        assert self.new_form(body) is None and "radios/nova" not in body and "/config" not in body
        pairs = [("kind", "custom"), ("channel_id", str(VOICE)), ("bot_id", "2")]
        response = self.post_raw(browser, self.url() + "/nova", pairs, htmx=True)
        assert response.code == 403 and self.radio.setup.calls == []

    def test_premium_page_lists_the_bots_and_channels_the_bot_offers_escaped(self):
        self.grant()
        body = self.page(self.logged_in())
        form = self.new_form(body)
        assert form is not None and set(form.choices("kind")) == {"live", "custom"}
        assert form.choices("channel_id") == ["", str(VOICE), str(STAGE)]
        assert form.choices("bot_id") == ["", "2", "3"]
        assert "Sala &lt;i&gt;DJ&lt;/i&gt; (voz)" in body and "Palco (palco)" in body

    def test_existing_radio_has_its_bot_preselected_even_when_not_free(self):
        self.grant()
        radio = self.seed(bot_id=1)
        form = self.config_form(self.page(self.logged_in()), radio)
        assert form.value("bot_id") == "1" and "1" in form.choices("bot_id")
        assert form.value("channel_id") == str(CHANNEL) or form.value("channel_id") == str(VOICE)
        assert form.value("kind") == "custom" and form.value("versao") == str(radio.rev)

    def test_at_the_maximum_the_new_form_gives_way_to_a_notice(self):
        self.grant()
        for bot in (1, 2, 3):
            self.seed(bot_id=bot)
        body = self.page(self.logged_in())
        assert self.new_form(body) is None and "máximo de 3 rádios" in body


# ============================================================================ criar


class CreateTest(SetupBase):
    def test_create_calls_the_bot_with_ints_and_shows_the_new_radio(self):
        self.grant()
        browser = self.logged_in()
        response = self.create(browser, kind="live", channel_id=str(STAGE), bot_id="2")
        body = response.body.decode()
        assert response.code == 200 and "Rádio criada" in body and "<html" not in body.lower()
        kind, gid, actor, kwargs = self.radio.setup.calls[-1]
        assert (kind, gid, actor) == ("configure", ALFA, int(self.fake.user["id"]))
        assert kwargs == {"radio_id": None, "channel_id": STAGE, "bot_id": 2, "kind": "live", "rev": None}
        assert f"2:{ALFA}" in self.radio.store.docs
        page = self.page(browser)
        assert self.config_form(page, self.radio.store.docs[f"2:{ALFA}"]) is not None

    def test_malformed_fields_are_422_without_calling_the_bot(self):
        self.grant()
        browser = self.logged_in()
        for fields in (
            {"kind": "loop", "channel_id": str(VOICE), "bot_id": "2"},
            {"kind": "live", "channel_id": "abc", "bot_id": "2"},
            {"kind": "live", "channel_id": str(VOICE), "bot_id": "-1"},
        ):
            response = self.create(browser, **fields)
            assert response.code == 422 and 'aria-invalid="true"' in response.body.decode(), fields
        assert self.radio.setup.calls == [] and self.radio.store.docs == {}

    def test_bot_refusals_land_on_the_named_field(self):
        self.grant()
        browser = self.logged_in()
        self.seed(bot_id=2)
        response = self.create(browser, kind="custom", channel_id=str(VOICE), bot_id="2")  # já tem rádio nesse bot
        body = html.unescape(response.body.decode())
        assert response.code == 422 and "já tem uma rádio" in body
        assert re.search(r'name="bot_id"[^>]*aria-invalid="true"', body)
        response = self.create(browser, kind="custom", channel_id="999", bot_id="3")  # canal fora da lista do bot
        assert response.code == 422 and "voz ou de palco" in html.unescape(response.body.decode())

    def test_manage_guild_refusal_from_the_bot_is_403_and_nothing_is_written(self):
        self.grant()
        self.radio.setup.fail_with = FORBIDDEN("manage_guild")
        response = self.create(self.logged_in(), kind="custom", channel_id=str(VOICE), bot_id="2")
        assert response.code == 403 and "Gerenciar Servidor" in html.unescape(response.body.decode())
        assert self.radio.store.docs == {}

    def test_audit_has_ids_only(self):
        self.grant()
        self.caplog.clear()
        self.create(self.logged_in(), kind="custom", channel_id=str(VOICE), bot_id="2")
        audit = self.audit_text()
        assert f"servidor={ALFA}" in audit and "escopo=radio:2" in audit and "campos=criada" in audit
        assert "Sala" not in self.all_log_text() and "Bot Dois" not in self.all_log_text()


# ============================================================================ configurar


class ConfigureTest(SetupBase):
    def test_other_servers_radio_is_404_and_untouched(self):
        self.grant()
        self.grant(OMEGA)
        other = self.seed(bot_id=1, gid=OMEGA, channel=789)
        browser = self.logged_in()
        pairs = [("versao", str(other.rev)), ("kind", "live"), ("channel_id", str(VOICE)), ("bot_id", "2")]
        response = self.post_raw(browser, self.url(ALFA, other.id) + "/config", pairs, htmx=True)
        assert response.code == 404 and self.radio.setup.calls == []

    def test_stale_version_is_409_and_nothing_changes(self):
        self.grant()
        radio = self.seed(bot_id=1, enabled=False)
        response = self.configure(self.logged_in(), radio, versao="1", kind="live", channel_id=str(STAGE), bot_id="1")
        assert response.code == 409 and CONFLICT_TEXT in response.body.decode()
        assert self.radio.setup.calls == [] and self.radio.store.docs[radio.id] == radio

    def test_changing_channel_and_kind_writes_through_the_bot(self):
        self.grant()
        radio = self.seed(bot_id=1, enabled=False)
        response = self.configure(self.logged_in(), radio, kind="live", channel_id=str(STAGE), bot_id="1")
        assert response.code == 200 and SAVED_TEXT in response.body.decode()
        saved = self.radio.store.docs[radio.id]
        assert saved.kind == "live" and saved.channel_id == STAGE and saved.rev == radio.rev + 1
        assert self.radio.setup.calls[-1][3]["rev"] == radio.rev

    def test_a_radio_that_is_on_refuses_changes_with_the_bots_message(self):
        self.grant()
        radio = self.seed(bot_id=1, enabled=True)
        body = self.page(self.logged_in())
        assert "desligue-a antes" in body
        response = self.configure(self.logged_in(), radio, kind="custom", channel_id=str(STAGE), bot_id="1")
        assert response.code == 422 and "Desligue a rádio" in html.unescape(response.body.decode())
        assert self.radio.store.docs[radio.id] == radio


# ============================================================================ redefinir


class ResetTest(SetupBase):
    def test_step_one_only_shows_the_confirmation(self):
        self.grant()
        radio = self.seed(bot_id=1)
        confirmation = self.reset_step_one(self.logged_in(), radio)
        body = html.unescape(confirmation.body.decode())
        assert confirmation.code == 200 and "Sim, redefinir" in body and "Bot Um" in body
        assert self.reset_form(confirmation.body.decode(), radio, "confirmar_reiniciar").value("token")
        assert self.radio.setup.calls == [] and radio.id in self.radio.store.docs

    def test_confirming_resets_through_the_bot_and_the_token_is_single_use(self):
        self.grant()
        radio = self.seed(bot_id=1)
        browser = self.logged_in()
        confirmation = self.reset_step_one(browser, radio)
        response = self.reset_confirm(browser, radio, confirmation)
        assert response.code == 200 and "Rádio redefinida" in response.body.decode()
        assert self.radio.setup.calls[-1] == (
            "reset",
            ALFA,
            int(self.fake.user["id"]),
            {"radio_id": radio.id, "rev": radio.rev},
        )
        assert radio.id not in self.radio.store.docs
        self.seed(bot_id=1)  # a mesma rádio volta com o mesmo rev: só o uso único impede o reuso
        again = self.reset_confirm(browser, radio, confirmation)
        assert again.code in (409, 422) and radio.id in self.radio.store.docs

    def test_tampered_or_foreign_token_is_refused(self):
        self.grant()
        radio = self.seed(bot_id=1)
        browser = self.logged_in()
        confirmation = self.reset_step_one(browser, radio)
        assert self.reset_confirm(browser, radio, confirmation, token="abc~def").code == 422
        other = self.seed(bot_id=2, channel=STAGE)
        other_confirmation = self.reset_step_one(browser, other)
        token = self.reset_form(other_confirmation.body.decode(), other, "confirmar_reiniciar").value("token")
        assert self.reset_confirm(browser, radio, confirmation, token=token).code in (409, 422)
        assert radio.id in self.radio.store.docs and other.id in self.radio.store.docs

    def test_without_htmx_the_page_comes_back_with_the_notice(self):
        self.grant()
        radio = self.seed(bot_id=1)
        browser = self.logged_in()
        confirmation = self.reset_step_one(browser, radio)
        form = self.reset_form(confirmation.body.decode(), radio, "confirmar_reiniciar")
        response = self.submit(browser, self.url(), form, htmx=False)
        body = response.body.decode()
        assert response.code == 200 and "<html" in body.lower() and "Rádio redefinida" in body
