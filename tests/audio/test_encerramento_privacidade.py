"""Encerramento e ocioso do player em call privada (D10): a última ação (que menciona quem agiu) não vai ao canal público."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import disnake
import pytest

from empiremuse.utils.music.models import state_steps

pytestmark = pytest.mark.critical

ACTION = "<@42> parou o player"


def call(*, private: bool, channel_id: int = 0):
    perms = SimpleNamespace(view_channel=not private, connect=True)
    return SimpleNamespace(id=channel_id, permissions_for=lambda _role: perms, guild=SimpleNamespace(default_role=0))


def make_player(channel, *, static: bool = False):
    return SimpleNamespace(
        static=static,
        skin="muse_text",
        skin_static="muse_text",
        guild_id=1,
        guild=SimpleNamespace(me=SimpleNamespace(voice=SimpleNamespace(channel=channel))),
        bot=SimpleNamespace(get_color=lambda _me: 0, pool=SimpleNamespace(player_skins={}, player_static_skins={})),
        tr=lambda key, **_kw: key,
        text_channel=None,
        lyric_embed=None,
        command_log=ACTION,
        message=SimpleNamespace(id=1, edit=AsyncMock(), components=[]),
        channel_cleanup=AsyncMock(),
        allowed_mentions=None,
        has_thread=False,
    )


@pytest.mark.asyncio
@pytest.mark.regression
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("path", ["interaction", "thread", "idle", "lyrics"])
async def test_the_end_and_idle_embeds_hide_the_last_action_in_a_private_call(monkeypatch, path, private):
    player = make_player(call(private=private), static=path == "thread")
    idle = AsyncMock()
    monkeypatch.setattr(state_steps, "send_idle_embed", idle)
    monkeypatch.setattr(state_steps, "_cu_archive_jukebox_thread", AsyncMock())
    if path == "lyrics":  # a tela de letra aberta na mensagem: o encerramento edita o embed dela
        player.lyric_embed, player.message.components = disnake.Embed(description="letra"), [object()]
        monkeypatch.setattr(state_steps, "_cu_disabled_view_children", lambda _self: [])

    if path == "interaction":
        await state_steps._cu_close_message_from_interaction(player, SimpleNamespace(message=SimpleNamespace(id=2)))
    elif path == "thread":
        await state_steps._cu_close_thread_message(player)
    elif path == "lyrics":
        await state_steps._cu_close_controller_message(player)
    else:
        await state_steps._cu_static_session(player)

    shown = idle.await_args.args[1] if path == "idle" else player.message.edit.await_args.kwargs["embed"].description
    assert (ACTION in shown) is not private


@pytest.mark.asyncio
@pytest.mark.regression
@pytest.mark.parametrize(("destination", "shown"), [(700, True), (701, False)])
async def test_the_idle_embed_of_a_private_call_shows_the_last_action_only_in_the_call_own_chat(
    monkeypatch, destination, shown
):
    player = make_player(call(private=True, channel_id=700))
    player.text_channel = SimpleNamespace(id=700)  # o player no chat da própria call
    idle = AsyncMock()
    monkeypatch.setattr(state_steps, "send_idle_embed", idle)

    await state_steps._cu_static_session(player, SimpleNamespace(channel=SimpleNamespace(id=destination)))

    assert (ACTION in idle.await_args.args[1]) is shown
