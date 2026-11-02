"""Lacunas do delta 1ce32bd2 (PR#199): o motivo do `/radio setup` sem id de bot e a lista exata do aviso da Jukebox."""

import pytest

from empiremuse.cogs import radio as radio_cog
from empiremuse.i18n import get_i18n
from empiremuse.utils.music.errors import GenericError
from tests.cogs.test_jukebox_setup_permissoes_canal import text_without
from tests.cogs.test_jukebox_setup_reset import Scenario
from tests.cogs.test_radio_setup_comandos import GUILD, build, fresh_cooldown, premium, run_setup  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("premium_on", "manage_guild", "snippet"),
    [
        (False, True, "premium"),
        (True, False, "Gerenciar Servidor"),
    ],
)
async def test_the_none_entry_explains_the_real_reason_when_the_actor_may_not_configure(
    premium_on, manage_guild, snippet
):
    w = build(manage_guild=manage_guild)
    if premium_on:
        await premium(w)
    else:
        await w.hub.control.load()

    with pytest.raises(GenericError) as refused:
        await run_setup(w, bot=radio_cog.NO_CHOICE)

    assert snippet in str(refused.value) and await w.hub.store.list_for_guild(GUILD) == []


def test_the_jukebox_warning_lists_only_what_the_bot_still_lacks_in_the_channel(monkeypatch):
    world = Scenario(monkeypatch)
    world.bot.pool.i18n = get_i18n()
    world.text._perms = text_without("manage_permissions", "attach_files")

    world.setup(world.text)

    text = world.final_embed_text()
    assert "Anexar Arquivos" in text  # falta e o setup daria
    assert "Inserir links" not in text and "embed_links" not in text  # o bot já tem
