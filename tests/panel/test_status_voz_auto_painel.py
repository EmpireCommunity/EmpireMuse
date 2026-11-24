"""2.1.2 (VW1/I6) — painel › Geral › status de voz: o MESMO valor do Discord (permanente + modelo, global do servidor para todo o pool).

Checkbox "Status permanente" + texto gravam ``voice_status_permanent`` e ``voice_channel_status`` no global do servidor, que o Discord lê;
texto vazio usa o modelo padrão (não desliga); ``effects`` aplica a todos os bots do pool por ``apply_permanent_voice_status`` e nunca mexe em
``stage_title_event``; XSRF e versão otimista como os outros formulários.
"""

import asyncio
import importlib
from types import ModuleType

import pytest

from tests.panel.helpers_settings import assentar
from tests.panel.test_settings_http import HttpBase, external_change

MODELO = "Ouvindo {track.title}"
OUTRO = "{track.author} - {track.title}"
CAMPO_PERMANENTE = "voice_status_permanent"
CAMPO_MODELO = "voice_channel_status"
INTACTOS = ("prefix", "custom_skins", "custom_skins_static", "listen_along_invites", "extra_flag", "global_skin")


def regras() -> ModuleType:
    return importlib.import_module("empiremuse.rules.voice_status")


pytestmark = pytest.mark.critical


class FormularioDoStatusTest(HttpBase):
    def test_the_checkbox_reflects_the_saved_permanent(self) -> None:
        casos = (
            ("nunca-escolhido-vem-marcado", {CAMPO_PERMANENTE: None, CAMPO_MODELO: ""}, True),
            ("salvo-desligado-vem-desmarcado", {CAMPO_PERMANENTE: False}, False),
        )
        for nome, salvo, marcado in casos:
            with self.subTest(nome):
                external_change(self.db, "global", 100, **salvo)

                _, form, _ = self.open_form(self.logged_in(), 100, "geral", CAMPO_PERMANENTE)

                assert (form.values(CAMPO_PERMANENTE) != []) is marcado


class GravacaoTest(HttpBase):
    def test_checkbox_and_text_write_the_same_global_the_discord_reads(self) -> None:
        browser = self.logged_in()
        antes = self.db.doc("global", 100)

        resposta = self.save(browser, 100, "geral", CAMPO_PERMANENTE, **{CAMPO_PERMANENTE: "on", CAMPO_MODELO: MODELO})

        assert resposta.code == 200, f"status {resposta.code}"
        depois = self.db.doc("global", 100)
        assert depois[CAMPO_PERMANENTE] is True and depois[CAMPO_MODELO] == MODELO
        cfg = regras().resolve_voice_status(depois)
        assert (cfg.permanent, cfg.template) == (True, MODELO)
        for chave in INTACTOS:
            assert depois[chave] == antes[chave], chave
        assert self.db.writes == [("global", 100)]

    def test_checkbox_and_text_combinations_are_stored_as_the_discord_reads_them(self) -> None:
        casos = (
            ("desmarcado-mantem-o-texto", {CAMPO_PERMANENTE: None, CAMPO_MODELO: OUTRO}, False, OUTRO),
            ("texto-vazio-nao-desliga", {CAMPO_PERMANENTE: "on", CAMPO_MODELO: ""}, True, ""),
            ("vazio-e-desmarcado", {CAMPO_PERMANENTE: None, CAMPO_MODELO: ""}, False, ""),
            ("flag-como-texto", {CAMPO_PERMANENTE: "false", CAMPO_MODELO: MODELO}, True, MODELO),  # presente = marcado
        )
        for nome, post, permanente, modelo in casos:
            with self.subTest(nome):
                external_change(self.db, "global", 100, **{CAMPO_PERMANENTE: True, CAMPO_MODELO: MODELO})

                assert self.save(self.logged_in(), 100, "geral", CAMPO_PERMANENTE, **post).code == 200

                depois = self.db.doc("global", 100)
                assert depois[CAMPO_PERMANENTE] is permanente and depois[CAMPO_MODELO] == modelo
                assert regras().resolve_voice_status(depois).template == (modelo or regras().DEFAULT_VOICE_TEMPLATE)

    def test_an_invalid_text_is_rejected_and_nothing_is_written(self) -> None:
        browser = self.logged_in()
        antes = self.db.doc("global", 100)

        for ruim in ("Tocando agora", "{foo}", "‮{track.title}", "{track.title}" + "x" * 500):
            resposta = self.save(
                browser, 100, "geral", CAMPO_PERMANENTE, **{CAMPO_PERMANENTE: "on", CAMPO_MODELO: ruim}
            )
            assert resposta.code == 422, f"{ruim[:20]!r}: {resposta.code}"

        assert self.db.doc("global", 100) == antes and self.db.writes == []

    def test_the_form_cannot_write_other_global_fields(self) -> None:
        browser = self.logged_in()
        url, form, _ = self.open_form(browser, 100, "geral", CAMPO_PERMANENTE)
        antes = self.db.doc("global", 100)
        pares = form.pairs(**{CAMPO_PERMANENTE: "on", CAMPO_MODELO: MODELO}) + [
            ("custom_skins", "AAAA"),
            ("extra_flag", "x"),
            ("listen_along_invites", "x"),
            ("ver", "9.9"),
        ]

        assert self.submit(browser, url, form, pairs=pares).code in (200, 422)

        depois = self.db.doc("global", 100)
        for chave in ("prefix", "ver", "custom_skins", "extra_flag", "listen_along_invites"):
            assert depois[chave] == antes[chave], chave


class EfeitosTest(HttpBase):
    def assentar_laco(self) -> None:
        """Deixa rodar as tarefas por player que o ``effects`` agendou (o apply nunca é esperado em linha)."""
        self.io_loop.run_sync(assentar)

    def test_every_player_of_every_bot_of_the_guild_gets_the_new_permanent(self) -> None:
        for ligado in (True, False):
            with self.subTest(ligado=ligado):
                post = {CAMPO_PERMANENTE: "on" if ligado else None, CAMPO_MODELO: MODELO}

                assert self.save(self.logged_in(), 800, "geral", CAMPO_PERMANENTE, **post).code == 200

                for jogador in (self.world.p800a, self.world.p800b):
                    chamada = jogador.called("apply_permanent_voice_status")[-1]
                    assert chamada["cfg"].permanent is ligado
                    assert not ligado or chamada["cfg"].template == MODELO

    def test_a_guild_without_an_active_player_just_saves(self) -> None:
        browser = self.logged_in()

        assert (
            self.save(
                browser, 200, "geral", CAMPO_PERMANENTE, bot=2, **{CAMPO_PERMANENTE: "on", CAMPO_MODELO: MODELO}
            ).code
            == 200
        )

        assert self.db.doc("global", 200)[CAMPO_MODELO] == MODELO

    def test_a_rejected_save_applies_nothing(self) -> None:
        browser = self.logged_in()

        assert self.save(browser, 100, "geral", CAMPO_PERMANENTE, **{CAMPO_MODELO: "sem placeholder"}).code == 422
        self.assentar_laco()

        assert self.world.p100.called("apply_permanent_voice_status") == []

    def test_the_post_answers_while_the_apply_of_every_player_is_still_running(self) -> None:
        portao = asyncio.Event()
        self.world.p800a.portao = self.world.p800b.portao = portao
        browser = self.logged_in()
        try:
            resposta = self.save(
                browser, 800, "geral", CAMPO_PERMANENTE, **{CAMPO_PERMANENTE: "on", CAMPO_MODELO: MODELO}
            )
            self.assentar_laco()

            assert resposta.code == 200, "o painel esperou o apply em linha"
            assert len(self.world.p800a.called("apply_permanent_voice_status")) == 1
            assert len(self.world.p800b.called("apply_permanent_voice_status")) == 1, (
                "um apply preso segura o do outro bot"
            )
        finally:
            portao.set()
            self.assentar_laco()


class XsrfEVersaoTest(HttpBase):
    def test_a_post_without_the_xsrf_token_is_403_and_writes_nothing(self) -> None:
        browser = self.logged_in()
        url, form, _ = self.open_form(browser, 100, "geral", CAMPO_PERMANENTE)
        pares = [p for p in form.pairs(**{CAMPO_PERMANENTE: "on", CAMPO_MODELO: MODELO}) if p[0] != "_xsrf"]

        sem = self.submit(browser, url, form, pairs=pares)
        errado = self.submit(browser, url, form, pairs=pares + [("_xsrf", "token-errado")])

        assert sem.code == 403 and errado.code == 403 and self.db.writes == []
        assert self.world.p100.called("apply_permanent_voice_status") == []

    def test_a_change_of_the_two_fields_by_the_discord_meanwhile_is_a_conflict(self) -> None:
        browser = self.logged_in()
        url, form, _ = self.open_form(browser, 100, "geral", CAMPO_PERMANENTE)
        external_change(self.db, "global", 100, **{CAMPO_PERMANENTE: False})

        resposta = self.submit(browser, url, form, **{CAMPO_PERMANENTE: "on", CAMPO_MODELO: MODELO})

        assert resposta.code == 409 and self.db.writes == []

    def test_a_change_of_another_global_field_is_not_a_conflict_and_survives(self) -> None:
        browser = self.logged_in()
        url, form, _ = self.open_form(browser, 100, "geral", CAMPO_PERMANENTE)
        external_change(self.db, "global", 100, prefix="?")

        resposta = self.submit(browser, url, form, **{CAMPO_PERMANENTE: "on", CAMPO_MODELO: MODELO})

        assert resposta.code == 200
        depois = self.db.doc("global", 100)
        assert depois["prefix"] == "?" and depois[CAMPO_MODELO] == MODELO
