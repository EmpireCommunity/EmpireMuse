"""Caracterização (VERDE contra o código ATUAL) de /setup e /reset — modules/music_settings.py:375-1010.

Objetivo: congelar o comportamento antes de extrair a lógica para `utils/music/jukebox_channel.py` (E4c, opção A).
Mede efeitos no Discord e no banco, a ORDEM entre eles, mensagens de erro e limites. Nada aqui corrige defeitos:
comportamentos ruins estão registrados de propósito e marcados `INVERTER_APOS_E4C` quando a E4c vai corrigi-los.
"""

import asyncio

import pytest

import empiremuse.cogs.music_settings as ms
from tests.support.jukebox import (
    GUILD_ID,
    CommandInter,
    FakeBot,
    FakeCategory,
    FakeDB,
    FakeForum,
    FakeGuild,
    FakeMessage,
    FakePlayer,
    FakeStage,
    FakeText,
    FakeThreadInfo,
    FakeVoice,
    Journal,
    perms,
)


class Scenario:
    """Servidor com um bot, o canal do comando (#geral, em uma categoria) e um canal de cada tipo."""

    def __init__(
        self,
        monkeypatch,
        *,
        admin=False,
        manage_channels=True,
        manage_threads=True,
        features=(),
        db_channel=None,
        db_message=None,
        history=True,
    ):
        self.j = Journal()
        bot_perms = perms(
            administrator=admin,
            manage_channels=manage_channels,
            manage_threads=manage_threads,
            read_message_history=history,
        )
        self.guild = FakeGuild(self.j, bot_perms, features)
        self.db = FakeDB(self.j, db_channel, db_message)
        self.bot = FakeBot(self.j, self.guild, self.db)
        self.category = FakeCategory(self.j, self.guild)
        self.lobby = FakeText(self.j, 11, "geral", self.guild, category=self.category)
        self.text = FakeText(self.j, 22, "pedidos", self.guild)
        self.voice = FakeVoice(self.j, 33, "voz", self.guild)
        self.stage = FakeStage(self.j, 44, "palco", self.guild)
        self.forum = FakeForum(self.j, 55, "fórum", self.guild)
        for channel in (self.lobby, self.text, self.voice, self.stage, self.forum):
            self.bot.channels[channel.id] = channel
        self.cog = ms.MusicSettings(self.bot)
        self.next_message = 9000
        monkeypatch.setattr(ms, "send_idle_embed", self.fake_send_idle_embed)

    # ---- o send_idle_embed real monta embeds enormes: aqui só registramos a chamada e devolvemos a mensagem
    async def fake_send_idle_embed(self, target, text="", *, bot, force=False, guild_data=None):
        label = target.label if hasattr(target, "label") else f"mensagem-{target.id}"
        self.j.add("send_idle_embed", label, force, bot is self.bot)
        if isinstance(target, FakeMessage):
            return target
        self.next_message += 1
        return target.add_message(self.next_message, self.guild.me)

    # ---- execução
    def command_inter(self):
        return CommandInter(self.j, self.bot, self.guild, self.lobby)

    def setup(self, target=None, purge="no", inter=None):
        self.inter = inter or self.command_inter()
        return asyncio.run(self.cog.setup.callback(self.cog, self.inter, target=target, purge_messages=purge))

    def reset(self, delete="não", inter=None):
        self.inter = inter or self.command_inter()
        return asyncio.run(self.cog.reset.callback(self.cog, self.inter, delete_channel=delete))

    # ---- consultas
    @property
    def saved(self):
        return self.db.doc["player_controller"]

    def final_embed_text(self):
        edits = self.j.of("inter.edit_original_message")
        return edits[-1][3]["embed"].description if edits else None


@pytest.fixture
def world(monkeypatch):
    return Scenario(monkeypatch)


def configured(world, thread=True):
    """Canal de pedidos #pedidos (22) com a mensagem 555 (e thread) gravados no banco."""
    message = world.text.add_message(555, world.guild.me, FakeThreadInfo(world.j, "song-requests") if thread else None)
    world.db.doc["player_controller"].update({"channel": "22", "message_id": "555"})
    return message


class TestResetTextChannel:
    def test_player_is_reset_after_the_discord_cleanup_the_database_and_the_reply(self, world):
        configured(world)
        player = FakePlayer(world.j, world.guild, world.text, static=True, message=object())
        world.bot.music.players[GUILD_ID] = player
        world.reset()
        names = [
            n
            for n in world.j.names()
            if n
            in ("db.update", "inter.edit_original_message", "player.process_hint", "player.invoke_np", "message.edit")
        ]
        assert names == [
            "message.edit",
            "db.update",
            "inter.edit_original_message",
            "player.process_hint",
            "player.invoke_np",
        ]
        assert player.static is False and player.message is None and player.text_channel is world.lobby
        assert world.j.of("player.invoke_np") == [("player.invoke_np", True)]
