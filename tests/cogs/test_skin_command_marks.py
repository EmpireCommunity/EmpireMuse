"""O ``/skin`` do 2.3 de ponta a ponta, com a tela de mentira: o que ele grava no servidor e no documento global.

- **Servidor:** a skin trocada vai com a marca de escolha (``rules/skins.with_choice_marks``), então a Clássica ``default``
  escolhida agora fica ao reabrir.
- **Global:** salvar sem trocar não grava escolha global, também com a skin do servidor em ``IGNORE_SKINS`` (o seletor abre
  no padrão e o "mostrado" é o mesmo ``opened_skin``).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.cogs import music_settings
from empiremuse.cogs.music_settings import MusicSettings, SkinSelector

GUILD = 4242
SKINS = dict.fromkeys(("default", "mini", "neon", "muse_text"), SimpleNamespace(preview=""))


class World:
    def __init__(self, *, controller: dict, global_data: dict, ignore: str = "") -> None:
        self.patched: list[dict] = []
        self.written: list[dict] = []
        self.controller, self.global_data = controller, {"custom_skins": {}, "custom_skins_static": {}, **global_data}
        avatar = SimpleNamespace(url="https://exemplo.invalid/a")
        self.bot = SimpleNamespace(
            player_skins=SKINS,
            player_static_skins=SKINS,
            config={"IGNORE_SKINS": ignore, "IGNORE_STATIC_SKINS": ignore},
            get_guild=lambda gid: SimpleNamespace(id=gid, me=None),
            get_data=self._get_data,
            get_global_data=self._get_global_data,
            update_global_data=self._update_global_data,
            get_color=lambda *args: 0,
            user=SimpleNamespace(
                id=1,
                display_name="Muse",
                display_avatar=SimpleNamespace(url=avatar.url, with_format=lambda fmt: avatar),
            ),
            pool=SimpleNamespace(
                default_skin="muse_text",
                default_static_skin="muse_text",
                servers=SimpleNamespace(patch=self._patch),
                get_all_bots=lambda: [],
            ),
        )

    async def _get_data(self, gid, *, db_name):
        return {"player_controller": dict(self.controller)}

    async def _get_global_data(self, gid, *, db_name):
        return dict(self.global_data)

    async def _update_global_data(self, gid, data, *, db_name):
        self.written.append(dict(data))

    async def _patch(self, gid, section, changes):
        self.patched.append(dict(changes))

    async def run(self, monkeypatch, **picks) -> None:
        """Abre o ``/skin``, escolhe ``picks`` (atributos do seletor) e salva."""
        saved = SimpleNamespace(
            guild_id=GUILD,
            author=SimpleNamespace(id=900, mention="<@900>"),
            response=SimpleNamespace(defer=AsyncMock(), is_done=lambda: True),
        )
        saved.edit_original_message = AsyncMock()

        class Pressed(SkinSelector):
            async def wait(self):
                self.interaction = saved
                for name, value in picks.items():
                    setattr(self, name, value)

        inter = SimpleNamespace(
            guild_id=GUILD,
            guild=None,
            bot=self.bot,
            author=SimpleNamespace(id=900, mention="<@900>"),
            response=SimpleNamespace(defer=AsyncMock()),
            store_message=SimpleNamespace(edit=AsyncMock()),
        )
        monkeypatch.setattr(music_settings, "select_bot_pool", AsyncMock(return_value=(inter, self.bot)))
        monkeypatch.setattr(music_settings, "update_inter", lambda *args: None)
        monkeypatch.setattr(music_settings, "SkinSelector", Pressed)
        monkeypatch.setattr(
            music_settings,
            "load_custom_skins",
            AsyncMock(return_value=SimpleNamespace(rejected=0, normal={}, static={})),
        )
        cog = MusicSettings.__new__(MusicSettings)
        cog.bot = self.bot
        await MusicSettings.change_skin.callback(cog, inter)


@pytest.mark.asyncio
@pytest.mark.regression
async def test_the_server_skin_command_writes_the_choice_mark_with_the_classic_default(monkeypatch):
    world = World(controller={"skin": "mini", "static_skin": "mini"}, global_data={"global_skin": False})

    await world.run(monkeypatch, skin_selected="default")

    assert world.patched == [{"skin": "default", "skin_chosen": True}]


@pytest.mark.asyncio
@pytest.mark.regression
async def test_the_global_skin_command_saved_unchanged_writes_no_choice_with_the_server_skin_ignored(monkeypatch):
    world = World(controller={"skin": "neon", "static_skin": "neon"}, global_data={"global_skin": True}, ignore="neon")

    await world.run(monkeypatch)

    assert len(world.written) == 1
    assert not {"player_skin", "player_skin_static"} & world.written[0].keys()
