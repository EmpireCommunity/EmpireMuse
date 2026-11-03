"""``/jukebox setup`` num canal existente apaga mensagens: só segue com a confirmação do autor (60 s); cancelar ou calar não apaga nada."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.i18n import get_i18n
from empiremuse.utils.music import jukebox_confirm
from empiremuse.utils.music.jukebox_confirm import ConfirmJukeboxPurge, confirm_purge

AUTHOR = 7


def tr_for(locale):
    return get_i18n().for_locale(locale)


async def _for_guild(_guild):
    return tr_for("pt-BR")


def clicker(user_id):
    return SimpleNamespace(
        author=SimpleNamespace(id=user_id), response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock())
    )


def setup(view_action, done=True):
    message = SimpleNamespace(edit=AsyncMock())
    sent = {}

    async def send(text, view, **_):
        sent["text"] = text
        asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(view_action(view)))
        return message

    inter = SimpleNamespace(
        author=SimpleNamespace(id=AUTHOR, mention="<@7>"),
        guild=None,
        bot=SimpleNamespace(pool=SimpleNamespace(i18n=SimpleNamespace(for_guild=_for_guild))),
        followup=SimpleNamespace(send=send),
        response=SimpleNamespace(is_done=lambda: done, defer=AsyncMock()),
    )
    return inter, SimpleNamespace(mention="<#1>"), message, sent


@pytest.mark.asyncio
async def test_confirm_button_by_the_author_allows_the_purge():
    async def act(view):
        await view.confirm.callback(clicker(AUTHOR))

    inter, target, _, _s = setup(act)
    assert await confirm_purge(inter, target) is True


@pytest.mark.asyncio
async def test_cancel_does_not_allow_the_purge():
    async def act(view):
        await view.cancel.callback(clicker(AUTHOR))

    inter, target, _, _s = setup(act)
    assert await confirm_purge(inter, target) is False


@pytest.mark.asyncio
async def test_no_answer_in_time_does_not_allow_the_purge(monkeypatch):
    monkeypatch.setattr(jukebox_confirm, "CONFIRM_TIMEOUT_S", 0.05)

    async def act(view):
        return None

    inter, target, message, _s = setup(act)
    assert await confirm_purge(inter, target) is False
    message.edit.assert_awaited()


@pytest.mark.asyncio
async def test_another_member_cannot_confirm():
    view = ConfirmJukeboxPurge(AUTHOR, tr_for("pt-BR"))
    other = clicker(99)
    assert await view.interaction_check(other) is False
    assert view.confirmed is None


@pytest.mark.parametrize("locale, word", [("pt-BR", "TODAS"), ("en-US", "ALL")])
def test_confirmation_text_states_the_real_scope_whole_channel(locale, word):
    """O texto não pode dizer que só as mensagens do bot somem: a limpeza é do canal inteiro."""
    text = tr_for(locale)("jukebox.setup.purge.ask", user="<@7>", channel="<#1>", seconds=60)
    assert word in text and "bot" not in text.lower()


@pytest.mark.asyncio
async def test_unacknowledged_select_interaction_is_deferred_before_the_followup():
    async def act(view):
        await view.cancel.callback(clicker(AUTHOR))

    inter, target, _, _s = setup(act, done=False)
    await confirm_purge(inter, target)
    inter.response.defer.assert_awaited_once_with(ephemeral=True)
