"""Item 8 (J1) · Canal de pedidos: a chave Jukebox estrita (``settings.request_channel_strict``) no painel.

Formulário ``acao=estrito`` na página do canal: mesma autorização e o mesmo limitador dos outros formulários do canal,
versão otimista do próprio campo, gravação só da chave pelo documento único e nenhum efeito no Discord. Documento antigo
(sem a chave) aparece desligado.
"""

from empiremuse.panel.services.base import CONFLICT_TEXT, SAVED_TEXT
from tests.panel.helpers_channel import GID
from tests.panel.helpers_settings import acao_form
from tests.panel.test_channel_page import CanalBase


class StrictBase(CanalBase):
    def strict_form(self, browser):
        form = acao_form(self.body(browser), "estrito")
        assert form is not None, "a página do canal deve oferecer o formulário acao=estrito"
        return form

    def toggle(self, browser, on, *, htmx=True, **overrides):
        form = self.strict_form(browser)
        value = "on" if on else None  # checkbox desmarcado = campo ausente
        return self.submit(browser, self.canal_url(), form, htmx=htmx, request_channel_strict=value, **overrides)

    def stored(self):
        return self.world.database.servers[GID]["settings"]

    def seed(self, value):
        """``None`` apaga a chave (documento de antes da J1); outro valor grava como o bot deixaria."""
        doc = self.world.database.servers[GID]
        if value is None:
            doc["settings"].pop("request_channel_strict", None)
        else:
            doc["settings"]["request_channel_strict"] = value
        doc["rev"] += 1
        self.pool.servers._docs.clear()


class StrictFormTest(StrictBase):
    def test_old_document_without_the_key_shows_the_toggle_off(self):
        self.seed(None)
        form = self.strict_form(self.logged_in())
        assert "request_channel_strict" not in self.stored()
        assert form.choices("request_channel_strict") == ["on"] and form.values("request_channel_strict") == []

    def test_turning_on_writes_only_the_flag_audits_the_field_name_and_touches_no_channel(self):
        browser = self.logged_in()
        response = self.toggle(browser, True)
        body = response.body.decode()
        assert response.code == 200 and SAVED_TEXT in body and "<html" not in body.lower()
        assert self.stored()["request_channel_strict"] is True
        assert self.world.request_doc["channel"] is None, "a seção request não muda"
        assert "categoria=canal campos=request_channel_strict" in self.audit_text()
        discord = [event for event in self.world.j if event[0] != "db"]  # só a gravação do documento único
        assert discord == [], f"nenhum efeito no Discord esperado, houve: {discord}"

    def test_turning_off_writes_false_and_the_page_shows_it_unchecked(self):
        self.seed(True)
        browser = self.logged_in()
        assert self.strict_form(browser).values("request_channel_strict") == ["on"]
        assert self.toggle(browser, False, htmx=False).code == 200
        assert self.stored()["request_channel_strict"] is False
        assert self.strict_form(browser).values("request_channel_strict") == []

    def test_stale_version_is_409_and_nothing_is_written(self):
        self.seed(None)
        browser = self.logged_in()
        response = self.toggle(browser, True, versao="0" * 16)
        assert response.code == 409 and CONFLICT_TEXT in response.body.decode()
        assert "request_channel_strict" not in self.stored()
