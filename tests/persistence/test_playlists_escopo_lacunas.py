"""Lacunas do escopo de playlists (PR#199) achadas por mutação: o teste de permissão usava o nome "x", que não existe, e a recusa por falta de
permissão ficava igual à recusa por "não achei" (edit e remove sem a checagem passavam); a estação podia resolver playlist da Jukebox sem teste
falhar; o autocomplete não tinha teste de permissão."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.rules.playlists import SCOPE_JUKEBOX, SCOPE_RADIO
from empiremuse.utils import playlists_commands
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.radio.custom_ports import CustomRadioPorts
from tests.persistence.test_playlists_escopo import fake_inter, member
from tests.persistence.test_server_playlists import BASE, GID, item, names, world  # noqa: F401

pytestmark = pytest.mark.critical


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [SCOPE_JUKEBOX, SCOPE_RADIO])
async def test_a_member_without_the_permission_cannot_edit_or_remove_an_existing_playlist(world, scope):  # noqa: F811
    await world.playlists.add(GID, item("Existe"), scope=scope)
    who, guild = member()
    inter = fake_inter(world, who, guild)

    with pytest.raises(GenericError):
        await playlists_commands.remove(inter.bot, inter, scope, "Existe")
    with pytest.raises(GenericError):
        await playlists_commands.edit(inter.bot, inter, scope, "Existe")

    assert names(await world.playlists.list(GID, scope)) == ["Existe"]  # a playlist continua lá
    inter.response.send_modal.assert_not_awaited()  # e o modal de edição nem abriu


@pytest.mark.asyncio
async def test_the_name_autocomplete_shows_names_only_to_who_may_manage_and_only_of_its_scope(world):  # noqa: F811
    await world.playlists.add(GID, item("Da Jukebox"), scope=SCOPE_JUKEBOX)
    await world.playlists.add(GID, item("Da Radio"), scope=SCOPE_RADIO)
    denied = fake_inter(world, *member())
    allowed = fake_inter(world, *member(manage_guild=True))

    assert await playlists_commands.jukebox_names(denied, "") == []
    assert await playlists_commands.radio_names(denied, "") == []
    assert await playlists_commands.jukebox_names(allowed, "") == ["Da Jukebox"]
    assert await playlists_commands.radio_names(allowed, "") == ["Da Radio"]


@pytest.mark.asyncio
async def test_a_station_resolves_only_radio_playlists_never_one_of_the_jukebox(world):  # noqa: F811
    await world.playlists.add(GID, item("Mesmo Nome")._replace(url=f"{BASE}jukebox"), scope=SCOPE_JUKEBOX)
    await world.playlists.add(GID, item("Mesmo Nome")._replace(url=f"{BASE}radio"), scope=SCOPE_RADIO)
    await world.playlists.add(GID, item("So Jukebox")._replace(url=f"{BASE}so-jukebox"), scope=SCOPE_JUKEBOX)
    asked: list[str] = []

    async def get_tracks(url, *args, **kwargs):
        asked.append(url)
        return SimpleNamespace(tracks=[]), None

    music = SimpleNamespace(get_tracks=get_tracks)
    ports = object.__new__(CustomRadioPorts)
    ports.pool = SimpleNamespace(servers=world.store)
    ports.radio = SimpleNamespace(guild_id=GID)
    ports._bot = lambda: SimpleNamespace(get_cog=lambda name: music)
    ports._guild = lambda: SimpleNamespace(id=GID, me=None)
    ports._channel = lambda: None
    ports._player = lambda: None

    await ports._playlist_tracks(["Mesmo Nome", "So Jukebox"])

    assert asked == [f"{BASE}radio"]  # a de mesmo nome do escopo rádio; a que só existe na Jukebox é pulada
