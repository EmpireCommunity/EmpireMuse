"""Item 21 (i18n F1) · Geral: idioma do servidor (``settings.language``) no painel.

Contrato ``.local/contrato-i18n-f1.md`` §5 e I-11/I-13: só valores iguais aos de ``pool.i18n.enabled`` ou vazio (automático),
erro sem ecoar o valor, cooldown de 10 s por servidor, ``apply_language`` depois de gravar, textos pelo ``_`` (``panel.json``).
"""

import html
from types import SimpleNamespace
from unittest.mock import patch

from empiremuse.i18n import get_i18n
from empiremuse.panel.handlers.settings import CATEGORY_FORMS
from empiremuse.panel.services import general
from tests.panel.test_settings_http import HttpBase

BAD_VALUES = ("../x", "pt-br", "en-US ", "xx-XX", "<b>pt-BR</b>")


class LanguageBase(HttpBase):
    """O ``pool.i18n`` real (catálogos de verdade) com ``apply_language`` gravando as chamadas."""

    def get_app(self):
        app = super().get_app()
        for form in CATEGORY_FORMS["geral"]:  # o formulário é único por processo: o cooldown não vaza entre testes
            if isinstance(form, general.LanguageForm):
                form._last_change = {}
        real = get_i18n()
        self.applied = []

        async def apply_language(gid):
            self.applied.append(gid)

        self.pool.i18n = SimpleNamespace(
            enabled=real.enabled,
            for_locale=real.for_locale,
            display_name=real.display_name,
            apply_language=apply_language,
        )
        return app

    def language_of(self, gid):
        return self.world.server_settings(gid).get("language")


class LanguageReadTest(LanguageBase):
    def test_form_offers_automatic_plus_every_enabled_locale_with_its_native_name(self):
        browser = self.logged_in()
        _, form, response = self.open_form(browser, 100, "geral", "language")
        real = get_i18n()
        assert form.choices("language") == [""] + sorted(real.enabled)
        body = response.body.decode()
        for code in real.enabled:
            assert html.escape(real.display_name(code)) in body, code

    def test_texts_come_from_the_panel_catalog_in_pt_br(self):
        browser = self.logged_in()
        _, _, response = self.open_form(browser, 100, "geral", "language")
        body = response.body.decode()
        tr = get_i18n().for_locale("pt-BR")
        assert tr("panel.language.title") == "Idioma do servidor"
        assert tr("panel.language.title") in body and tr("panel.language.auto") in body
        assert "panel.language." not in body, "chave crua no HTML: catálogo pt-BR incompleto"

    def test_catalog_keys_used_by_the_form_exist_in_both_required_locales(self):
        used = ("title", "label", "auto", "help", "save", "unknown", "cooldown", "unavailable")
        for locale in ("en-US", "pt-BR"):
            entries = get_i18n().entries(locale)
            missing = [k for k in used if f"panel.language.{k}" not in entries]
            assert not missing, f"{locale}: {missing}"


class LanguageWriteTest(LanguageBase):
    def test_saving_a_language_writes_only_that_field_and_applies_it(self):
        browser = self.logged_in()
        before = self.world.server_settings(100)
        response = self.save(browser, 100, "geral", "language", language="en-US")
        assert response.code == 200, f"status {response.code}"
        assert self.world.server_settings(100) == before | {"language": "en-US"}
        assert self.db.writes == [("servidor", 100)]
        assert self.applied == [100]
        audit = self.audit_text()
        assert "servidor=100" in audit and "-> en-US" in audit

    def test_empty_means_automatic(self):
        browser = self.logged_in()
        with patch.object(general, "monotonic", self.clock):
            assert self.save(browser, 100, "geral", "language", language="en-US").code == 200
            self.clock.advance(general.LANGUAGE_COOLDOWN_SECONDS)
            assert self.save(browser, 100, "geral", "language", language="").code == 200
        assert self.language_of(100) is None
        assert self.applied == [100, 100]

    def test_unchanged_submit_writes_nothing_and_applies_nothing(self):
        browser = self.logged_in()
        current = self.language_of(100) or ""
        assert self.save(browser, 100, "geral", "language", language=current).code == 200
        assert self.db.writes == [] and self.applied == []

    def test_invalid_values_are_refused_without_echo_and_without_effects(self):
        browser = self.logged_in()
        before = self.world.server_settings(100)
        for bad in BAD_VALUES:
            response = self.save(browser, 100, "geral", "language", language=bad)
            assert response.code == 422, f"{bad!r}: status {response.code}"
            body = response.body.decode()
            assert html.escape(bad) not in body and bad not in body, f"{bad!r} ecoado"
            assert "Escolha um idioma da lista." in body
        assert self.world.server_settings(100) == before
        assert self.db.writes == [] and self.applied == []

    def test_second_change_within_ten_seconds_is_refused_then_allowed(self):
        browser = self.logged_in()
        fake = self.clock  # o mesmo relógio falso do painel: ``submit`` avança 4 s entre POSTs
        with patch.object(general, "monotonic", fake):
            assert self.save(browser, 100, "geral", "language", language="en-US").code == 200
            response = self.save(browser, 100, "geral", "language", language="pt-BR")
            assert response.code == 422
            assert "Aguarde 10 segundos" in response.body.decode()
            assert self.language_of(100) == "en-US" and self.applied == [100]
            fake.advance(general.LANGUAGE_COOLDOWN_SECONDS)
            assert self.save(browser, 100, "geral", "language", language="pt-BR").code == 200
        assert self.language_of(100) == "pt-BR" and self.applied == [100, 100]

    def test_member_without_manage_guild_cannot_read_or_write(self):
        browser = self.logged_in()
        assert browser.get(self.url(600, "geral")).code == 404
        pairs = [("language", "en-US"), ("versao", "0" * 16)]
        assert self.post_raw(browser, self.url(600, "geral"), pairs).code == 404
        assert self.applied == []

    def test_a_saved_but_disabled_locale_stays_visible_and_is_not_rewritten(self):
        browser = self.logged_in()
        self.world.external_server_change(100, language="fr")  # gravado quando "fr" estava ligado
        _, form, response = self.open_form(browser, 100, "geral", "language")
        assert form.value("language") == "fr"
        assert "fr (tradução indisponível no momento)" in response.body.decode()
        assert self.submit(browser, self.url(100, "geral"), form).code == 200
        assert self.db.writes == [] and self.applied == []
