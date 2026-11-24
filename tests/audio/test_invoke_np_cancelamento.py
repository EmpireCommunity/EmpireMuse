"""PR #31 (7ad9366c) — `LavalinkPlayer.invoke_np`: o CancelledError da edição da mensagem do player é silenciado.

Antes da correção o cancelamento imprimia um traceback e deixava `updating` preso em True, travando as atualizações seguintes.
Sem Discord real: o player é um SimpleNamespace e a mensagem, um AsyncMock.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.regression

DATA = {"content": "np", "components": [], "embeds": []}


class FakePlayer(SimpleNamespace):
    """Como o ``LavalinkPlayer``: aceita referência fraca e hash por identidade (o estado de render é um ``WeakKeyDictionary``)."""

    __hash__ = object.__hash__


def public_call_guild():
    """O bot numa call pública (``@everyone`` vê e entra): a skin recebe o player, sem a máscara da call privada."""
    perms = SimpleNamespace(view_channel=True, connect=True)
    channel = SimpleNamespace(permissions_for=lambda _role: perms, guild=SimpleNamespace(default_role=None))
    return SimpleNamespace(me=SimpleNamespace(voice=SimpleNamespace(channel=channel)))


def make_player(edit):
    skin = SimpleNamespace(load=lambda player: dict(DATA))
    return FakePlayer(
        is_closing=False,
        text_channel=MagicMock(),
        current=SimpleNamespace(title="faixa"),
        updating=False,
        has_lyrics=False,
        retry_setup_hints=False,
        static=False,
        skin="padrao",
        bot=SimpleNamespace(pool=SimpleNamespace(player_skins={"padrao": skin})),
        last_data=None,
        controller_mode=True,
        temp_embed=None,
        message=SimpleNamespace(edit=edit),
        guild=public_call_guild(),
        allowed_mentions=None,
        ignore_np_once=True,
        has_thread=False,
        paused=False,
    )


def invoke_np(player):
    from empiremuse.utils.music.models import LavalinkPlayer

    return asyncio.run(LavalinkPlayer.invoke_np(player))


def test_message_edit_is_attempted_before_the_cancellation():
    edit = AsyncMock(side_effect=asyncio.CancelledError)
    player = make_player(edit)

    invoke_np(player)

    edit.assert_awaited_once()
