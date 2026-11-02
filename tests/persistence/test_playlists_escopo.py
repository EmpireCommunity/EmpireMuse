"""Playlists por escopo (jukebox | rádio): isolamento nos dois sentidos, cota por escopo, o escopo que vale é o pedido (nunca o do item nem o do
arquivo), a estação só vê as de rádio e a permissão (dono/Administrador/Gerenciar Servidor, SEM premium) é conferida nos dois escopos."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from empiremuse.rules.errors import RuleError
from empiremuse.rules.limits import MAX_SERVER_PLAYLISTS, PLAYLIST_FILE_MAX_BYTES
from empiremuse.rules.playlists import SCOPE_JUKEBOX, SCOPE_RADIO, Playlist, parse_file
from empiremuse.i18n import get_i18n
from empiremuse.utils import playlists_commands
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.radio.station_commands import playlist_names
from tests.persistence.test_server_playlists import BASE, GID, item, names, run, world  # noqa: F401

pytestmark = pytest.mark.critical


@pytest.mark.asyncio
async def test_each_scope_only_sees_its_own_playlists_and_the_same_name_can_live_in_both(world):  # noqa: F811
    p = world.playlists
    await p.add(GID, item("Rock"), scope=SCOPE_JUKEBOX)
    await p.add(GID, item("Rock"), scope=SCOPE_RADIO)
    await p.add(GID, item("So Radio"), scope=SCOPE_RADIO)

    assert names(await p.list(GID, SCOPE_JUKEBOX)) == ["Rock"]
    assert names(await p.list(GID, SCOPE_RADIO)) == ["Rock", "So Radio"]
    assert await p.find(GID, "So Radio") is None  # o padrão é a Jukebox: nunca acha a de rádio
    assert (await p.find(GID, "So Radio", scope=SCOPE_RADIO)).scope == SCOPE_RADIO

    await p.remove(GID, "Rock", scope=SCOPE_RADIO)
    assert names(await p.list(GID, SCOPE_JUKEBOX)) == ["Rock"]
    assert names(await p.list(GID, SCOPE_RADIO)) == ["So Radio"]
    assert isinstance(await p.remove(GID, "So Radio", scope=SCOPE_JUKEBOX), RuleError)


@pytest.mark.asyncio
async def test_the_requested_scope_wins_over_the_scope_of_the_item_and_of_the_file(world):  # noqa: F811
    p = world.playlists
    await p.add(GID, item("Dentro")._replace(scope=SCOPE_RADIO), scope=SCOPE_JUKEBOX)
    raw = json.dumps({"playlists": [{"name": "Arq", "url": f"{BASE}arq", "scope": "radio"}]})
    await p.import_file(GID, parse_file(raw, max_bytes=PLAYLIST_FILE_MAX_BYTES), scope=SCOPE_JUKEBOX)

    assert names(await p.list(GID, SCOPE_JUKEBOX)) == ["Dentro", "Arq"]
    assert await p.list(GID, SCOPE_RADIO) == ()


@pytest.mark.asyncio
async def test_the_quota_is_per_scope_and_a_stored_item_without_scope_reads_as_jukebox(world):  # noqa: F811
    p = world.playlists
    for n in range(MAX_SERVER_PLAYLISTS):
        assert not isinstance(await p.add(GID, item(f"J{n}"), scope=SCOPE_JUKEBOX), RuleError)
    assert isinstance(await p.add(GID, item("Extra"), scope=SCOPE_JUKEBOX), RuleError)
    assert not isinstance(await p.add(GID, item("Radio"), scope=SCOPE_RADIO), RuleError)

    def drop_scope(doc):
        doc["playlists"] = [{k: v for k, v in raw.items() if k != "scope"} for raw in doc["playlists"]]
        return True

    await world.store.mutate_doc(GID, ("playlists",), drop_scope)
    assert len(await p.list(GID, SCOPE_JUKEBOX)) == MAX_SERVER_PLAYLISTS + 1  # sem o campo = jukebox
    assert await p.list(GID, SCOPE_RADIO) == ()


@pytest.mark.asyncio
async def test_the_station_editor_lists_only_radio_playlists(world):  # noqa: F811
    await world.playlists.add(GID, item("Da Jukebox"), scope=SCOPE_JUKEBOX)
    await world.playlists.add(GID, item("Da Radio"), scope=SCOPE_RADIO)
    inter = SimpleNamespace(bot=SimpleNamespace(pool=SimpleNamespace(servers=world.store)), guild_id=GID)

    assert await playlist_names(inter) == ["Da Radio"]


def member(*, owner=False, administrator=False, manage_guild=False):
    who = MagicMock(spec=disnake.Member)
    who.id = 9
    who.guild_permissions = SimpleNamespace(administrator=administrator, manage_guild=manage_guild)
    guild = SimpleNamespace(owner_id=9 if owner else 1)
    return who, guild


def fake_inter(world, who, guild):  # noqa: F811
    return SimpleNamespace(
        author=who,
        guild=guild,
        guild_id=GID,
        bot=SimpleNamespace(
            pool=SimpleNamespace(
                servers=world.store,
                i18n=SimpleNamespace(for_guild=AsyncMock(return_value=get_i18n().for_locale("pt-BR"))),
            )
        ),
        response=SimpleNamespace(defer=AsyncMock(), send_modal=AsyncMock(), is_done=lambda: True),
        edit_original_message=AsyncMock(),
        text_values={"name": "Nova", "url": f"{BASE}nova", "description": "", "genres": "rock, pop", "icon": ""},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [SCOPE_JUKEBOX, SCOPE_RADIO])
async def test_a_member_without_the_permission_is_refused_in_every_command_of_both_scopes(world, scope):  # noqa: F811
    who, guild = member()
    inter = fake_inter(world, who, guild)
    bot = inter.bot

    for call in (
        playlists_commands.list_(bot, inter, scope),
        playlists_commands.create(bot, inter, scope),
        playlists_commands.edit(bot, inter, scope, "x"),
        playlists_commands.remove(bot, inter, scope, "x"),
    ):
        with pytest.raises(GenericError):
            await call
    tr = get_i18n().for_locale("pt-BR")
    with pytest.raises(GenericError):  # o modal reconfere a permissão no envio
        await playlists_commands.PlaylistModal(bot, tr, scope).callback(inter)
    assert await world.playlists.list(GID, scope) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("flags", [{"owner": True}, {"administrator": True}, {"manage_guild": True}])
async def test_owner_admin_or_manage_server_creates_a_radio_playlist_without_premium(world, flags):  # noqa: F811
    who, guild = member(**flags)
    inter = fake_inter(world, who, guild)  # o pool de teste nem tem premium: nada o consulta
    tr = get_i18n().for_locale("pt-BR")

    await playlists_commands.PlaylistModal(inter.bot, tr, SCOPE_RADIO).callback(inter)

    saved = await world.playlists.list(GID, SCOPE_RADIO)
    assert names(saved) == ["Nova"] and saved[0].genres == ("rock", "pop")
    assert await world.playlists.list(GID, SCOPE_JUKEBOX) == ()
