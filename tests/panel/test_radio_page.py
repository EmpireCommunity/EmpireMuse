"""Item 8 (R1 §3 "Página web", R-6) · Rádios: lista e regras do chat da call no painel.

Caminhos de risco: id da rota com sufixo ``:{guild_id}`` (outro servidor = 404), premium e ``manage_messages`` no canal
(senão 403, nada gravado), allowlist ``chat.*`` (``enabled``, ``channel_id`` e ``source`` ignorados), validação por campo
(422 sem gravar), compare-and-set em ``rev`` (409) e auditoria só com nomes de campo.
"""

import html

from empiremuse.panel.handlers.settings import MAX_BODY_BYTES, WRITE_LIMIT_PER_MINUTE
from empiremuse.panel.handlers.radio import RadiosHandler
from empiremuse.panel.services.base import CONFLICT_TEXT, SAVED_TEXT
from empiremuse.panel.services.radio_live import StationNames
from tests.panel.helpers_radio import UNAVAILABLE, install, make_channel, radio_state
from tests.panel.helpers_settings import parse_forms
from tests.panel.test_settings_http import Each, HttpBase

ALFA, OMEGA = 100, 800  # Alfa: bot 1, usuário administrador · Ômega: bots 1 e 2
CHANNEL = 123


class RadioBase(HttpBase):
    def get_app(self):
        app = super().get_app()
        self.radio = install(self.pool)
        RadiosHandler.page.live.names = StationNames()  # o cache de nomes é da classe: não vaza entre testes
        self.world.channels[CHANNEL] = make_channel(CHANNEL, "Sala <i>DJ</i>", ALFA)
        self.world.channels[456] = make_channel(456, "Sala 2", OMEGA, manage_messages=False)
        self.world.channels[789] = make_channel(789, "Sala Ômega", OMEGA)
        return app

    def url(self, gid=ALFA, radio_id=None):  # noqa: D102 - o helper da base tem outra assinatura
        return f"/painel/servidores/{gid}/radios" + (f"/{radio_id}" if radio_id else "")

    def page(self, browser, gid=ALFA):
        response = browser.get(self.url(gid))
        assert response.code == 200, f"GET radios: {response.code}"
        return response.body.decode()

    def chat_forms(self, body):
        return [f for f in parse_forms(body) if f.has("voice_delete_after_s")]

    def seed_alfa(self, **kwargs):
        self.radio.premium.grant(ALFA)
        radio = self.radio.store.seed(1, ALFA, CHANNEL, **kwargs)
        self.radio.service.states[radio.id] = radio_state(title="Rock <b>FM</b>")
        return radio

    def save(self, browser, radio, *, htmx=True, **fields):
        form = next(f for f in self.chat_forms(self.page(browser)) if f.value("versao") == str(radio.rev))
        return self.submit(browser, self.url(), form, htmx=htmx, **fields)


# ============================================================================ página


class RadioPageTest(RadioBase):
    def test_without_premium_shows_the_notice_and_no_form_even_with_a_radio(self):
        radio = self.radio.store.seed(1, ALFA, CHANNEL)
        self.radio.service.states[radio.id] = radio_state()
        body = self.page(self.logged_in())
        assert "Este servidor não tem rádios liberadas" in body
        assert self.chat_forms(body) == []

    def test_premium_without_any_radio_on_still_allows_editing(self):
        self.radio.premium.grant(ALFA, active=0)
        radio = self.radio.store.seed(1, ALFA, CHANNEL, enabled=False)
        body = self.page(self.logged_in())
        assert "Ligadas agora: 0 de 3." in body and "Desligada" in body
        assert [f.action for f in self.chat_forms(body)] == [self.url(ALFA, radio.id)]

    def test_lists_state_and_title_escaped_with_a_form_only_where_the_user_manages_messages(self):
        self.radio.premium.grant(OMEGA, active=2)
        editable = self.radio.store.seed(1, OMEGA, 789)
        read_only = self.radio.store.seed(2, OMEGA, 456)
        self.radio.service.states[editable.id] = radio_state(title="Rock <b>FM</b>")
        self.radio.service.states[read_only.id] = radio_state(playing=False, paused_by_owner=True)
        body = self.page(self.logged_in(), OMEGA)
        assert "Ligadas agora: 2 de 3." in body
        assert "Rock &lt;b&gt;FM&lt;/b&gt;" in body and "<b>FM</b>" not in body
        assert "Pausada pelo dono" in body and "Tocando" in body
        forms = self.chat_forms(body)
        assert [f.action for f in forms] == [self.url(OMEGA, editable.id)]
        assert forms[0].value("voice_delete_after_s") == "30"
        assert "Gerenciar Mensagens" in body  # a rádio do bot 2 fica só para ler

    def test_navigation_offers_the_radios_category(self):
        body = self.page(self.logged_in())
        assert f'href="{self.url()}"' in body and 'aria-current="page"' in body


# ============================================================================ gravação (R-6)


class RadioChatTest(RadioBase):
    def test_saving_writes_only_chat_bumps_rev_audits_field_names_and_ignores_other_fields(self):
        radio = self.seed_alfa()
        browser = self.logged_in()
        response = self.save(
            browser,
            radio,
            voice_delete_after_s="45",
            stage_max_age_s="600",
            enabled=None,
            channel_id="999",
            source="x",
            kind="live",
        )
        body = response.body.decode()
        assert response.code == 200 and SAVED_TEXT in body and "<html" not in body.lower()
        saved = self.radio.store.docs[radio.id]
        assert saved.chat == {"voice_delete_after_s": 45, "stage_max_messages": 7, "stage_max_age_s": 600}
        assert (saved.enabled, saved.channel_id, saved.source, saved.kind, saved.rev) == (
            True,
            CHANNEL,
            radio.source,
            "custom",
            4,
        )
        assert self.radio.store.updates == [(radio.id, "1001")]
        assert [r.chat for r in self.radio.manager.chat_updates] == [
            saved.chat
        ]  # o varredor em execução recebe o chat novo
        audit = self.audit_text()
        assert "escopo=radio:1 categoria=radios campos=voice_delete_after_s" in audit and "45" not in audit
        assert self.chat_forms(self.page(browser))[0].value("versao") == "4"

    def test_route_ids_outside_this_server_or_malformed_are_404_and_write_nothing(self):
        self.seed_alfa()
        self.radio.premium.grant(OMEGA)
        other = self.radio.store.seed(1, OMEGA, CHANNEL)
        browser = self.logged_in()
        form = self.chat_forms(self.page(browser))[0]
        pairs = form.pairs(voice_delete_after_s="45")

        def check(radio_id):
            response = self.post_raw(browser, self.url(ALFA, radio_id), pairs, htmx=True)
            assert response.code == 404, f"status {response.code}"

        Each.run([other.id, "2:100", "1:1000", "1:100/x", "abc", "1:100%00", "../1:100"], check)
        assert self.radio.store.updates == [] and self.radio.store.docs[other.id].rev == 3

    def test_invalid_values_are_422_per_field_without_writing(self):
        radio = self.seed_alfa()
        browser = self.logged_in()

        def check(case):
            field, value = case
            response = self.save(browser, radio, **{field: value})
            body = html.unescape(response.body.decode())
            assert response.code == 422, f"status {response.code}"
            assert f'name="{field}"' in body and "Use um número inteiro de" in body
            assert self.radio.store.updates == []

        Each.run(
            [
                ("voice_delete_after_s", "4"),
                ("voice_delete_after_s", "601"),
                ("stage_max_messages", "abc"),
                ("stage_max_messages", "-3"),
                ("stage_max_age_s", "1e3"),
                ("stage_max_age_s", ""),
            ],
            check,
        )

    def test_stale_rev_is_409_and_nothing_is_written(self):
        radio = self.seed_alfa()
        browser = self.logged_in()
        response = self.save(browser, radio, versao="2", voice_delete_after_s="45")
        assert response.code == 409 and CONFLICT_TEXT in response.body.decode()
        assert self.radio.store.updates == [] and self.radio.manager.chat_updates == []

    def test_without_manage_messages_in_the_channel_or_premium_is_403_and_nothing_is_written(self):
        radio = self.seed_alfa()
        browser = self.logged_in()
        form = self.chat_forms(self.page(browser))[0]
        pairs = form.pairs(voice_delete_after_s="45")
        self.world.channels[CHANNEL] = make_channel(CHANNEL, "Sala", ALFA, manage_messages=False)
        denied = self.post_raw(browser, self.url(ALFA, radio.id), pairs, htmx=True)
        self.world.channels[CHANNEL] = make_channel(CHANNEL, "Sala", ALFA)
        self.radio.premium.docs.clear()
        no_premium = self.post_raw(browser, self.url(ALFA, radio.id), pairs, htmx=True)
        for response in (denied, no_premium):
            assert response.code == 403, f"status {response.code}"
            assert "permissão necessária" in response.body.decode()
        assert self.radio.store.updates == []


# ============================================================================ banco das rádios fora do ar


class RadioUnavailableTest(RadioBase):
    def test_database_outage_is_503_on_the_page_and_on_the_form(self):
        radio = self.seed_alfa()
        browser = self.logged_in()
        form = self.chat_forms(self.page(browser))[0]
        pairs = form.pairs(voice_delete_after_s="45")
        self.radio.premium.fail_with = UNAVAILABLE()
        page = browser.get(self.url())
        post = self.post_raw(browser, self.url(ALFA, radio.id), pairs, htmx=True)
        for response in (page, post):
            assert response.code == 503, f"status {response.code}"
            assert "não estão disponíveis agora" in response.body.decode()
        assert self.radio.store.updates == []


class RadioSweeperFailureTest(RadioBase):
    def test_a_sweeper_failure_does_not_undo_the_save_and_only_warns(self):
        radio = self.seed_alfa()
        self.radio.manager.fail_with = RuntimeError("varredor caiu")
        response = self.save(self.logged_in(), radio, voice_delete_after_s="45")
        assert response.code == 200 and SAVED_TEXT in response.body.decode()
        assert self.radio.store.docs[radio.id].chat["voice_delete_after_s"] == 45
        assert "varredor não foi atualizado (RuntimeError)" in self.all_log_text()


# ============================================================================ limites do handler (corpo e escritas por minuto)


class RadioChatLimitsTest(RadioBase):
    def test_body_over_the_limit_is_413_and_writes_nothing(self):
        radio = self.seed_alfa()
        browser = self.logged_in()
        form = self.chat_forms(self.page(browser))[0]
        pairs = form.pairs(voice_delete_after_s="45") + [("enchimento", "x" * (MAX_BODY_BYTES + 1))]
        response = self.post_raw(browser, self.url(ALFA, radio.id), pairs, htmx=True)
        assert response.code == 413
        assert self.radio.store.updates == [] and self.radio.manager.chat_updates == []

    def test_writes_past_the_per_minute_limit_are_429_with_retry_after_and_write_nothing(self):
        radio = self.seed_alfa()
        browser = self.logged_in()
        form = self.chat_forms(self.page(browser))[0]
        for _ in range(WRITE_LIMIT_PER_MINUTE):  # inválidos de propósito: contam como escrita e não gravam
            assert self.submit(browser, self.url(), form, htmx=True, gap=0, voice_delete_after_s="4").code == 422
        blocked = self.submit(browser, self.url(), form, htmx=True, gap=0, voice_delete_after_s="45")
        assert blocked.code == 429 and blocked.headers.get("Retry-After") == "60"
        assert self.radio.store.updates == [] and self.radio.store.docs[radio.id].rev == radio.rev
