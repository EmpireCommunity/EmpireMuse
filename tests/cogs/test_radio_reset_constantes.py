"""Constantes do `/radio reset` (contrato-nomes-radios §2) com os números do contrato como LITERAIS.

Sem estes pinos, a cópia em ``global.config_backup`` podia expirar em 1 segundo e a confirmação podia durar qualquer tempo
sem nenhum teste falhar.
"""

import datetime

import pytest

from tests.cogs.radio_config import configure
from tests.cogs.test_radio_comandos import CHANNEL, GUILD, build  # noqa: F401
from tests.cogs.test_radio_comandos import premium
from empiremuse.cogs import radio as radio_cog

pytestmark = pytest.mark.critical

ACTOR = 7


@pytest.mark.asyncio
async def test_the_reset_backup_expires_30_days_after_it_was_taken():
    w = build()
    await premium(w)
    radio = await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)

    await w.hub.setup.reset(GUILD, ACTOR, radio.id, radio.rev)

    backup = next(iter(w.db.tables["config_backup"].values()))
    taken = datetime.datetime.fromtimestamp(backup["at"], tz=datetime.timezone.utc)
    assert abs((backup["expire_at"] - taken) - datetime.timedelta(days=30)) < datetime.timedelta(seconds=2)


@pytest.mark.asyncio
async def test_the_reset_confirmation_buttons_time_out_after_60_seconds():
    w = build()
    await premium(w)
    await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
    await radio_cog.Radio.radio_reset.callback(w.cog, w.inter, bot=None)

    view = w.inter.edit_original_message.await_args.kwargs["view"]

    assert view.timeout == 60
