"""A prévia do editor de skin personalizada numa call privada (``skin_editor_preview``).

Risco coberto: a mensagem do editor é pública no canal; numa call privada (ou sem call conhecida) a prévia não pode
desenhar a faixa, a capa, quem pediu nem a fila do player de verdade. Com a visão mascarada ela recebe a visão; sem ela,
os dados de exemplo (``player=None``). Só o Discord e o conversor são dublês.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from empiremuse.utils.music.interactions import skin_editor_preview
from empiremuse.utils.music.interactions.skin_editor_preview import SkinEditorPreviewMixin

pytestmark = pytest.mark.critical

GUILD = 4242
MASKED = "empiremuse.utils.music.private_call"


def _voice(*, public: bool):
    perms = SimpleNamespace(view_channel=public, connect=public)
    guild = SimpleNamespace(default_role=object())
    return SimpleNamespace(guild=guild, permissions_for=lambda role: perms)


def _player(voice):
    me = SimpleNamespace(voice=SimpleNamespace(channel=voice) if voice is not None else None)
    return SimpleNamespace(guild=SimpleNamespace(me=me), current="faixa de verdade")


class Editor(SkinEditorPreviewMixin):
    def __init__(self, player):
        bot = SimpleNamespace(music=SimpleNamespace(players={GUILD: player}))
        self.bot = SimpleNamespace(pool=SimpleNamespace(get_all_bots=lambda: [bot]))
        self.ctx = SimpleNamespace(guild_id=GUILD)
        self.guild = object()
        self.message_data = {"embeds": []}


class Masked:
    def __init__(self, player):
        self.player = player


def _shown(monkeypatch, player):
    seen = []

    def convert(info, *, guild, ctx, player):
        seen.append(player)
        return {"content": "", "embeds": []}

    monkeypatch.setattr(skin_editor_preview, "skin_converter", convert)
    Editor(player).build_embeds()
    return seen[0]


@pytest.mark.regression
@pytest.mark.parametrize("voice", [_voice(public=False), None], ids=["privada", "sem-call"])
def test_a_private_call_preview_never_gets_the_real_player(monkeypatch, voice):
    player = _player(voice)

    monkeypatch.setitem(sys.modules, MASKED, None)  # sem a visão mascarada: o import falha
    assert _shown(monkeypatch, player) is None  # os dados de exemplo

    monkeypatch.setitem(sys.modules, MASKED, types.SimpleNamespace(PrivateCallView=Masked))
    shown = _shown(monkeypatch, player)
    assert isinstance(shown, Masked) and shown.player is player


def test_a_public_call_preview_uses_the_real_player(monkeypatch):
    player = _player(_voice(public=True))
    monkeypatch.setitem(sys.modules, MASKED, types.SimpleNamespace(PrivateCallView=Masked))
    assert _shown(monkeypatch, player) is player
