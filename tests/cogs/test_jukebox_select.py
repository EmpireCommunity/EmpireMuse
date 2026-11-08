"""Select compartilhado e navegação efêmera da Jukebox (T-435)."""

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from empiremuse.music_cog.controller_dispatch import ControllerDispatchMixin
from empiremuse.music_cog.play_menus import PlayMenusMixin
from empiremuse.rules.playlists import Playlist
from empiremuse.utils.music.guild_pin import GUILD_PIN, GUILD_PIN_PAGE, build_guild_pin
from empiremuse.utils.music.interactions.jukebox_panel import render_panel
from empiremuse.utils.server_playlists import select_options


def tr(key, **kwargs):
    return key


def playlists():
    return tuple(Playlist(i, f"Lista {i}", "", (), f"https://example.com/{i}", "🎧") for i in range(100))


def world(value, custom_id=GUILD_PIN):
    items = playlists()
    bot = NS(
        bot_ready=True,
        pool=NS(
            i18n=NS(for_guild=AsyncMock(return_value=tr)),
            servers=NS(playlists=NS(list=AsyncMock(return_value=items))),
        ),
    )
    select = build_guild_pin(bot, tr, items)
    button = disnake.ui.Button(label="Pedir", custom_id="musicplayer_add_song")
    message = NS(components=[disnake.ui.ActionRow(button)._underlying, disnake.ui.ActionRow(select)._underlying])
    inter = NS(
        data=NS(custom_id=custom_id, values=[value]),
        guild=NS(id=1),
        guild_id=1,
        user=NS(voice=True),
        message=message,
        send=AsyncMock(),
        edit_original_message=AsyncMock(),
        response=NS(edit_message=AsyncMock(), defer=AsyncMock()),
        followup=NS(send=AsyncMock()),
    )
    cog = NS(play=NS(callback=AsyncMock()))
    handler = NS(bot=bot, request_executor=AsyncMock(return_value=cog))
    return handler, inter, cog


@pytest.mark.regression
@pytest.mark.asyncio
async def test_100_playlists_never_build_an_over_25_select_in_any_server_menu():
    items = playlists()
    assert len(select_options(None, items, limit=100)) == 25
    assert len(build_guild_pin(None, tr, items).options) == 25
    bot, guild = MagicMock(), MagicMock()
    bot.get_color.return_value = 0
    bot.app_emojis = {}
    bot.pool.emoji_data = {}
    guild.me.display_avatar.replace.return_value.url = "https://example.com/avatar"
    payload = render_panel(bot, guild, [], tr=tr, playlists=items)
    select = payload["components"][-1].children[0]
    assert len(select.options) == 25 and select.options[-1].value == "jb:page:2"
    bot.pool.servers.playlists.list = AsyncMock(return_value=items)
    req = NS(
        bot=bot,
        guild=NS(id=1),
        guild_data={"loaded": True},
        inter=None,
        emoji=None,
        is_pin=False,
        query=">> [📌 Favoritos do servidor 📌] <<",
        user_data={},
    )
    await PlayMenusMixin._play_fav_options(NS(bot=bot), req)
    assert len(req.fav_opts) == 25
    assert req.fav_opts[-1]["option"].value == "> pin: Lista 24"


@pytest.mark.asyncio
@pytest.mark.parametrize("custom_id", [GUILD_PIN, GUILD_PIN_PAGE])
async def test_navigation_clamps_and_reloads_without_playing_or_editing_other_controls(custom_id):
    handler, inter, cog = world("jb:page:999", custom_id)
    await ControllerDispatchMixin.guild_pin(handler, inter)
    handler.bot.pool.servers.playlists.list.assert_awaited()
    cog.play.callback.assert_not_awaited()
    edited = inter.edit_original_message.call_args.kwargs["components"]
    if custom_id == GUILD_PIN:
        assert edited[0].children[0].custom_id == "musicplayer_add_song"
        assert edited[1].children[0].options[0].value == "pl:Lista 0"
        sent = inter.followup.send.call_args.kwargs
        assert sent["ephemeral"] is True
        select = sent["components"][0]
    else:
        inter.followup.send.assert_not_awaited()
        select = edited[0]
    assert select.custom_id == GUILD_PIN_PAGE
    assert select.options[-1].value == "pl:Lista 99"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["jb:live", "jb:unknown", "jb:page:x", "jb:page:-1"])
async def test_unknown_controls_are_refused_without_playing(value):
    handler, inter, cog = world(value)
    await ControllerDispatchMixin.guild_pin(handler, inter)
    inter.send.assert_awaited_once()
    handler.request_executor.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("value, name", [("pl:jb:live", "jb:live"), ("Rock", "Rock"), ("pl:Lista 99", "Lista 99")])
async def test_playlist_names_and_legacy_values_follow_the_existing_store_resolution(value, name):
    handler, inter, cog = world(value)
    await ControllerDispatchMixin.guild_pin(handler, inter)
    assert cog.play.callback.call_args.kwargs["query"] == f"> pin: {name}"


def test_fixed_entries_only_appear_on_first_page_and_no_empty_select_is_built():
    fixed = [
        disnake.SelectOption(label="Rádio", value="jb:live"),
        disnake.SelectOption(label="Servidor", value="jb:server"),
    ]
    first = build_guild_pin(None, tr, playlists(), 1, fixed)
    second = build_guild_pin(None, tr, playlists(), 2, fixed)
    assert len(first.options) == 25 and first.options[2].value == "pl:Lista 0"
    assert second.options[0].value == "jb:page:1" and second.options[1].value == "pl:Lista 22"
    assert all(option.value not in {"jb:live", "jb:server"} for option in second.options)
    assert build_guild_pin(None, tr, ()) is None
    assert len(build_guild_pin(None, tr, (), fixed_entries=fixed).options) == 2


@pytest.mark.asyncio
async def test_a_page_whose_playlists_were_deleted_is_replaced_with_a_message():
    handler, inter, cog = world("jb:page:2", GUILD_PIN_PAGE)
    handler.bot.pool.servers.playlists.list.return_value = ()
    await ControllerDispatchMixin.guild_pin(handler, inter)
    edited = inter.edit_original_message.call_args.kwargs
    assert edited["components"] == [] and edited["content"]
    cog.play.callback.assert_not_awaited()


@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    [
        "player.idle.server_songs_placeholder",
        "player.panel.playlists.placeholder",
        "player.play_a_song_playlist_from.placeholder",
    ],
)
async def test_the_caller_keeps_its_hint_and_paginated_pages_keep_the_page_placeholder(key):
    from empiremuse.utils.music.guild_pin import load_guild_pin

    handler, inter, cog = world("jb:page:2")
    handler.bot.pool.servers.playlists.list.return_value = playlists()[:1]
    single = await load_guild_pin(handler.bot, tr, 1, placeholder_key=key)
    assert single.placeholder == key
    ephemeral = await load_guild_pin(handler.bot, tr, 1, placeholder_key="jukebox.select.page.placeholder")
    assert ephemeral.placeholder == "jukebox.select.page.placeholder"
    many = build_guild_pin(None, tr, playlists(), placeholder_key=key)
    assert many.placeholder == "jukebox.select.page.placeholder"
