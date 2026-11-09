"""Prazo de confirmação do reset no Discord; o reset não mantém cópia."""

import pytest

from tests.cogs.radio_config import configure
from tests.cogs.test_radio_comandos import CHANNEL, GUILD, build  # noqa: F401
from tests.cogs.test_radio_comandos import premium
from empiremuse.cogs import radio as radio_cog

pytestmark = pytest.mark.critical

ACTOR = 7


@pytest.mark.asyncio
async def test_the_reset_confirmation_buttons_time_out_after_60_seconds():
    w = build()
    await premium(w)
    await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
    await radio_cog.Radio.radio_reset.callback(w.cog, w.inter, channel=None, bot=None)

    view = w.inter.edit_original_message.await_args.kwargs["view"]

    assert view.timeout == 60
