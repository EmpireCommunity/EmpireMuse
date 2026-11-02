"""Spec final do dono · Jukebox: as opções A (``settings.jukebox_auto_delete``) e B (``settings.jukebox_text_requests``).

Formulário ``acao=opcoes`` na página da Jukebox: mesma autorização e o mesmo limitador dos outros formulários da página,
versão otimista dos próprios campos, gravação só das chaves pelo documento único e nenhum efeito no Discord. Documento
antigo (sem as chaves) aparece com A desligada e B ligada (o comportamento de hoje).
"""

from empiremuse.panel.services.base import CONFLICT_TEXT, SAVED_TEXT
from tests.panel.helpers_jukebox import GID
from tests.panel.helpers_settings import acao_form
from tests.panel.test_jukebox_page import JukeboxPageBase

A, B = "jukebox_auto_delete", "jukebox_text_requests"


class OptionsBase(JukeboxPageBase):
    def options_form(self, browser):
        form = acao_form(self.body(browser), "opcoes")
        assert form is not None, "a página da Jukebox deve oferecer o formulário acao=opcoes"
        return form

    def save(self, browser, *, auto_delete, text_requests, htmx=True, **overrides):
        form = self.options_form(browser)
        fields = {A: "on" if auto_delete else None, B: "on" if text_requests else None}  # desmarcado = ausente
        return self.submit(browser, self.jukebox_url(), form, htmx=htmx, **fields, **overrides)

    def stored(self):
        return self.world.database.servers[GID]["settings"]

    def seed(self, **values):
        """``None`` apaga a chave (documento de antes); outro valor grava como o bot deixaria."""
        doc = self.world.database.servers[GID]
        for key, value in values.items():
            if value is None:
                doc["settings"].pop(key, None)
            else:
                doc["settings"][key] = value
        doc["rev"] += 1
        self.pool.servers._docs.clear()


class OptionsFormTest(OptionsBase):
    def test_old_document_without_the_keys_shows_a_off_and_b_on(self):
        self.seed(**{A: None, B: None})
        form = self.options_form(self.logged_in())
        assert A not in self.stored() and B not in self.stored()
        assert form.choices(A) == ["on"] and form.values(A) == []
        assert form.choices(B) == ["on"] and form.values(B) == ["on"]

    def test_saving_writes_only_the_two_flags_audits_the_fields_and_touches_no_channel(self):
        browser = self.logged_in()
        response = self.save(browser, auto_delete=True, text_requests=False)
        body = response.body.decode()
        assert response.code == 200 and SAVED_TEXT in body and "<html" not in body.lower()
        assert self.stored()[A] is True and self.stored()[B] is False
        assert self.world.jukebox_doc["channel"] is None, "a seção jukebox não muda"
        audit = self.audit_text()
        assert "categoria=jukebox" in audit and A in audit and B in audit
        discord = [event for event in self.world.j if event[0] != "db"]  # só a gravação do documento único
        assert discord == [], f"nenhum efeito no Discord esperado, houve: {discord}"

    def test_migrated_auto_delete_shows_checked_and_can_be_turned_off(self):
        self.seed(**{A: True})
        browser = self.logged_in()
        assert self.options_form(browser).values(A) == ["on"]
        assert self.save(browser, auto_delete=False, text_requests=True, htmx=False).code == 200
        assert self.stored()[A] is False and self.stored()[B] is True
        assert self.options_form(browser).values(A) == []

    def test_stale_version_is_409_and_nothing_is_written(self):
        self.seed(**{A: None, B: None})
        browser = self.logged_in()
        response = self.save(browser, auto_delete=True, text_requests=True, versao="0" * 16)
        assert response.code == 409 and CONFLICT_TEXT in response.body.decode()
        assert A not in self.stored() and B not in self.stored()
