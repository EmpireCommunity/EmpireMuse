"""``/jukebox setup`` num canal existente apaga mensagens: só segue com a confirmação do autor (60 s); cancelar ou calar não apaga nada."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.utils.music import jukebox_confirm
from empiremuse.utils.music.jukebox_confirm import ConfirmJukeboxPurge, confirm_purge

AUTHOR = 7


def clicker(user_id):
    return SimpleNamespace(
        author=SimpleNamespace(id=user_id), response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock())
    )


def setup(view_action):
    message = SimpleNamespace(edit=AsyncMock())
    sent = {}

    async def send(text, view, **_):
        sent["view"] = view
        asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(view_action(view)))
        return message

    inter = SimpleNamespace(author=SimpleNamespace(id=AUTHOR, mention="<@7>"), followup=SimpleNamespace(send=send))
    return inter, SimpleNamespace(mention="<#1>"), message


@pytest.mark.asyncio
async def test_confirm_button_by_the_author_allows_the_purge():
    async def act(view):
        await view.confirm.callback(clicker(AUTHOR))

    inter, target, _ = setup(act)
    assert await confirm_purge(inter, target) is True


@pytest.mark.asyncio
async def test_cancel_does_not_allow_the_purge():
    async def act(view):
        await view.cancel.callback(clicker(AUTHOR))

    inter, target, _ = setup(act)
    assert await confirm_purge(inter, target) is False


@pytest.mark.asyncio
async def test_no_answer_in_time_does_not_allow_the_purge(monkeypatch):
    monkeypatch.setattr(jukebox_confirm, "CONFIRM_TIMEOUT_S", 0.05)

    async def act(view):
        return None

    inter, target, message = setup(act)
    assert await confirm_purge(inter, target) is False
    message.edit.assert_awaited()


@pytest.mark.asyncio
async def test_another_member_cannot_confirm():
    view = ConfirmJukeboxPurge(AUTHOR)
    other = clicker(99)
    assert await view.interaction_check(other) is False
    assert view.confirmed is None
