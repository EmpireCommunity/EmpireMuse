"""/radio favoritas: autorização real em execução e resposta privada só do servidor."""

from unittest.mock import AsyncMock

import pytest

from empiremuse.cogs.radio import Radio
from empiremuse.utils.music.interactions.fav_menu_server import ServerRadioFavoritesView
from tests.cogs.test_radio_comandos import GUILD, build, fresh_cooldown
from tests.persistence.test_radio_favoritas import station

pytestmark = [pytest.mark.critical, pytest.mark.asyncio]


@pytest.mark.parametrize("role", ["manager", "administrator", "owner", "member", "absent"])
async def test_command_rechecks_guild_authority_and_never_reads_personal_favorites(role: str) -> None:
    w = build(manage_guild=role == "manager")
    for bot in w.bots:
        guild = bot.get_guild(GUILD)
        guild.get_member(7).guild_permissions.administrator = role == "administrator"
        guild.owner_id = 7 if role == "owner" else 0
        if role == "absent":
            guild.get_member = lambda uid: None
            guild.fetch_member = AsyncMock(side_effect=ConnectionError)
    w.inter.send = AsyncMock()
    w.hub.favorites.list_all = AsyncMock(return_value=([], []))
    w.hub.user_favorites.stations = AsyncMock(side_effect=AssertionError("personal data must stay private"))
    w.hub.user_favorites.filters = AsyncMock(side_effect=AssertionError("personal data must stay private"))

    await Radio.radio_favoritas.callback(w.cog, w.inter)

    w.inter.response.defer.assert_awaited_once_with(ephemeral=True)
    w.inter.send.assert_awaited_once()
    sent = w.inter.send.await_args
    assert sent.kwargs["ephemeral"] is True
    if role in {"manager", "administrator", "owner"}:
        w.hub.favorites.list_all.assert_awaited_once_with(GUILD)
        assert isinstance(sent.kwargs["view"], ServerRadioFavoritesView)
        sent.kwargs["view"].stop()
    else:
        w.hub.favorites.list_all.assert_not_awaited()
        assert "Gerenciar Servidor" in sent.args[0]
    w.hub.user_favorites.stations.assert_not_awaited()
    w.hub.user_favorites.filters.assert_not_awaited()


async def test_command_opens_server_list_only_with_neutralized_names() -> None:
    w = build()
    w.inter.send = AsyncMock()
    await w.hub.favorites.add(GUILD, 7, station(1, "@everyone https://evil.example Rádio"))

    await Radio.radio_favoritas.callback(w.cog, w.inter)

    sent = w.inter.send.await_args
    assert "Rádio" in sent.args[0] and "@everyone" not in sent.args[0] and "https://" not in sent.args[0]
    assert sent.kwargs["ephemeral"] is True
    assert sent.kwargs["allowed_mentions"].everyone is False
    sent.kwargs["view"].stop()
