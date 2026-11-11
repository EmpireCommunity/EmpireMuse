"""R1 web: revisão velha não bloqueia reset; confirmação permanece vinculada e de uso único."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.i18n import get_i18n
from empiremuse.panel.services.radio_setup import RadioSetupService, TOKEN_SCOPE
from tests.cogs.radio_config import configure
from tests.cogs.test_radio_pr_b import world, ACTOR, GUILD, CHANNEL
from tests.panel.test_radio_setup import SetupBase

pytestmark = pytest.mark.critical


@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "alteration", [None, "radio", "user", "guild", "scope", "rev", "expired", "negative", "oversized"]
)
async def test_old_confirmation_resets_only_its_original_radio_actor_guild_and_scope(alteration):
    w = await world()
    radio = await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
    ctx = SimpleNamespace(pool=w.pool, guild_id=GUILD, user_id=str(ACTOR))
    now = [100.0]
    service = RadioSetupService("test-fixture-key-0123456789-abcdefgh", clock=lambda: now[0])
    tr = get_i18n().for_locale("pt-BR")
    token_user = str(ACTOR + 1) if alteration == "user" else str(ACTOR)
    token_guild = GUILD + 1 if alteration == "guild" else GUILD
    token_scope = "configure" if alteration == "scope" else TOKEN_SCOPE
    token_radio = f"3:{GUILD}" if alteration == "radio" else radio.id
    token = service.tokens.issue(token_user, token_guild, token_scope, f"{token_radio}:{radio.rev}")
    edited = await w.hub.store.save_config(replace(radio, channel_id=CHANNEL + 1), expected_rev=radio.rev)
    sent = {"rev": str(radio.rev + 1), "negative": "-1", "oversized": "1" * 13}.get(alteration, str(radio.rev))
    args = {"versao": [sent], "acao": ["confirmar_reiniciar"], "token": [token], "actor_id": ["999"]}
    if alteration == "expired":
        now[0] = 161.0
    original = w.hub.setup.reset
    w.hub.setup.reset = AsyncMock(side_effect=original)
    status, view = await service.reset(ctx, edited, args, "/reset", ("bot", "call"), tr)
    if alteration is None:
        assert status == 200 and await w.hub.store.get(radio.id) is None
        w.hub.setup.reset.assert_awaited_once_with(GUILD, ACTOR, radio.id, radio.rev)
        assert service.tokens.check(token, str(ACTOR), GUILD, TOKEN_SCOPE, f"{radio.id}:{radio.rev}") == "invalido"
    else:
        assert status in (409, 422) and await w.hub.store.get(radio.id) == edited
        w.hub.setup.reset.assert_not_awaited()


class StaleResetHttpTest(SetupBase):
    def test_old_web_confirmation_uses_the_session_actor_and_deletes_after_an_edit(self):
        radio = self.seed(bot_id=1)
        browser = self.logged_in()
        confirmation = self.reset_step_one(browser, radio)
        self.radio.store.docs[radio.id] = replace(radio, rev=radio.rev + 1)
        response = self.reset_confirm(browser, radio, confirmation, actor_id="999")
        assert response.code == 200 and radio.id not in self.radio.store.docs
        assert self.radio.setup.calls[-1][2] == int(self.fake.user["id"])

    def test_options_uses_the_guild_and_session_actor(self):
        self.grant()
        original = self.radio.setup.options
        self.radio.setup.options = AsyncMock(side_effect=original)
        response = self.logged_in().get(self.url() + "?actor_id=999")
        assert response.code == 200
        from tests.panel.test_radio_page import ALFA

        self.radio.setup.options.assert_awaited_once_with(ALFA, int(self.fake.user["id"]))
