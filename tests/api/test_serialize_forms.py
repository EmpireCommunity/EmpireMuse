"""B2 (contrato T-456 §4 m5, §14 "Segredos"): a serialização sobre os formulários REAIS do painel.

Prova que (1) nenhuma resposta contém segredo, (2) todo formulário do painel tem forma registrada (campo novo não
vaza e formulário novo não passa em silêncio) e (3) o painel genérico continua igual: serializar não muda o
``Formulario`` nem o HTML da página da conta.
"""

import asyncio
import copy
import json
import re

import pytest

from empiremuse.panel.handlers.settings import CATEGORY_FORMS
from empiremuse.panel.services.account import AccountService
from empiremuse.panel.services.account_base import AccountContext
from empiremuse.panel.services.base import FormContext, Formulario
from empiremuse.panel.services.service import SettingsService
from empiremuse.rules import panel_serialize as ps
from tests.panel.helpers_account import RPC_TOKEN_OLD, SESSIONKEY, AccountWorld
from tests.panel.helpers_settings import UID, SettingsWorld
from tests.panel.test_account_page import AccountCase

pytestmark = pytest.mark.critical

XSRF = re.compile(r"(xsrf[^>]*?(?:value|content)=\")[^\"]+", re.IGNORECASE)


def read_account(world):
    service = AccountService(SettingsService())
    ctx = AccountContext(world.pool, world.bot, UID)
    return service, ctx, asyncio.run(service.read_all(ctx))


def read_guild(world):
    service = SettingsService()
    ctx = FormContext(pool=world.pool, bot=world.bot1, guild=world.g100, guild_id=100, user_id=str(UID))
    forms = [form for group in CATEGORY_FORMS.values() for form in group]

    async def run():
        return {form.key: await service.read(form, ctx, "/x") for form in forms}

    return asyncio.run(run())


class TestSecrets:
    def test_account_forms_carry_no_lastfm_key_and_no_stored_rpc_token(self):
        world = AccountWorld()
        world.link_lastfm()
        world.set_doc(token=RPC_TOKEN_OLD)
        _, _, forms = read_account(world)
        dumped = json.dumps([ps.serialize_form(form, ps.ACCOUNT) for form in forms.values()])
        assert SESSIONKEY not in dumped and RPC_TOKEN_OLD not in dumped
        assert ps.serialize_form(forms["lastfm"], ps.ACCOUNT)["extras"] == {"vinculada": True, "usuario": "usuario_lfm"}
        assert ps.serialize_form(forms["rpc"], ps.ACCOUNT)["extras"]["configurado"] is True

    def test_the_new_rpc_token_comes_once_and_never_again(self):
        world = AccountWorld()
        service, ctx, forms = read_account(world)
        section = service.section("rpc")
        outcome = asyncio.run(service.submit(section, ctx, "rpc_token_gerar", {"versao": [forms["rpc"].versao]}))
        assert outcome.status == 200
        once = ps.new_rpc_token(outcome.form)
        assert once is not None and once[1] == {"Cache-Control": "no-store"}
        token = once[0]["token"]
        assert len(token) >= 32
        assert token not in json.dumps(ps.serialize_form(outcome.form, ps.ACCOUNT))
        again = asyncio.run(service.read(section, ctx))
        assert ps.new_rpc_token(again) is None
        assert token not in json.dumps(ps.serialize_form(again, ps.ACCOUNT))


class TestEveryRealFormIsRegistered:
    def test_every_settings_form_and_account_section_has_a_shape(self):
        registered = set(ps.registered_forms())
        for group in CATEGORY_FORMS.values():
            for form in group:
                assert (ps.GUILD, form.key) in registered, f"formulário sem forma na API: {form.key}"
        for section in AccountService(SettingsService()).sections:
            assert (ps.ACCOUNT, section.key) in registered, f"seção sem forma na API: {section.key}"

    def test_real_guild_forms_serialize_to_json_with_their_fields(self):
        world = SettingsWorld()
        asyncio.run(world.premigrate())
        forms = read_guild(world)
        for key, form in forms.items():
            body = ps.serialize_form(form, ps.GUILD)
            json.dumps(body)  # dado puro
            assert body["key"] == key and body["version"] == form.versao
        player = ps.serialize_form(forms["player"], ps.GUILD)
        assert set(player["values"]) == {
            "default_player_volume",
            "autoplay",
            "check_other_bots_in_vc",
            "enable_restrict_mode",
        }
        assert player["extras"]["volumes"][0] == 5

    def test_real_account_forms_serialize_to_json(self):
        _, _, forms = read_account(AccountWorld())
        bodies = {key: ps.serialize_form(form, ps.ACCOUNT) for key, form in forms.items()}
        json.dumps(bodies)
        favoritos = bodies["favoritos"]["extras"]["itens"]
        assert favoritos == [{"nome": "Rock", "url": "https://example.com/rock", "link": True}]
        assert bodies["prefixo"]["values"] == {"custom_prefix": "!"}


class TestGenericPanelIsUnchanged(AccountCase):
    def test_serializing_changes_neither_the_form_nor_the_rendered_page(self):
        browser = self.logged_in()
        before_html = XSRF.sub(r"\1X", self.body(browser))
        service = AccountService(SettingsService())
        ctx = AccountContext(self.pool, self.world.bot, UID)
        forms = self.io_loop.run_sync(lambda: service.read_all(ctx))
        snapshot = copy.deepcopy(forms)
        for form in forms.values():
            ps.serialize_form(form, ps.ACCOUNT)
            ps.error_fields(form)
            ps.new_rpc_token(form)
        assert all(isinstance(form, Formulario) for form in forms.values())
        assert forms == snapshot, "serializar não pode alterar o formulário que o painel genérico renderiza"
        assert XSRF.sub(r"\1X", self.body(browser)) == before_html
        assert self.db.writes == []
