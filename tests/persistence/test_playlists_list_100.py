"""Regressão do limite de 100: a lista do ``/playlists`` mostra TODAS, em mensagens de linhas inteiras que cabem no limite do Discord."""

from unittest.mock import AsyncMock

import pytest

from empiremuse.rules.limits import MAX_SERVER_PLAYLISTS
from empiremuse.rules.playlists import SCOPE_JUKEBOX
from empiremuse.utils import playlists_commands
from tests.persistence.test_playlists_escopo import fake_inter, member
from tests.persistence.test_server_playlists import GID, item, world  # noqa: F401

DISCORD_MESSAGE_MAX = 2000


@pytest.mark.regression
@pytest.mark.asyncio
async def test_listing_a_full_server_shows_every_playlist_in_messages_that_fit(world):  # noqa: F811
    names = [f"Playlist numero {n:03d} " + "x" * 12 for n in range(MAX_SERVER_PLAYLISTS)]
    for n, name in enumerate(names, start=1):
        await world.playlists.add(GID, item(name, order=n), scope=SCOPE_JUKEBOX)
    who, guild = member(owner=True)
    inter = fake_inter(world, who, guild)
    inter.followup = type("Followup", (), {"send": AsyncMock()})()

    await playlists_commands.list_(inter.bot, inter, SCOPE_JUKEBOX)

    sent = [inter.edit_original_message.await_args.args[0]]
    sent += [call.args[0] for call in inter.followup.send.await_args_list]
    assert len(sent) > 1
    assert all(len(text) <= DISCORD_MESSAGE_MAX for text in sent)
    assert all(any(name in text for text in sent) for name in names)
