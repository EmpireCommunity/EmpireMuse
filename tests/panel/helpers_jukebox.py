"""Dublês da E4c (categoria `jukebox`): um servidor 100 com um bot (7000) e canais REAIS de disnake.

Reaproveita os dublês de Discord de tests/support/canal_da_jukebox.py (os mesmos da caracterização) e o FakeDB do
painel (tests/panel/helpers_settings.py), que registra mutação do cache, gravação do próprio dict e ids não-int.
"""

import asyncio
import copy
import re
from types import SimpleNamespace

import tornado.web

from tests.support.canal_da_jukebox import (
    BOT_ID,
    BOT_MENTION,
    FakeBot,
    FakeCategoryChannel,
    FakeForum,
    FakeGuild,
    FakeMessage,
    FakeStage,
    FakeText,
    FakeThreadInfo,
    FakeVoice,
    Journal,
    perms,
)
from tests.panel.helpers import require
from tests.panel.helpers_login import make_fake_oauth
from tests.panel.helpers_servers import raw_guild
from tests.panel.helpers_settings import FakeDB, SettingsBase, SettingsWorld, UID, WorldDatabase
from empiremuse.utils.servers import ServerStore

GID = 100
PANEL_USER = str(UID)
TASK_ID = re.compile(r"[A-Za-z0-9_-]{32}")


class PanelGuild(FakeGuild):
    """Servidor do cache do bot, com o que a autorização do painel (E3) lê."""

    def __init__(self, journal, bot_perms, features=()):
        super().__init__(journal, bot_perms, features)
        self.id, self.name, self.owner_id, self.icon = GID, "Alfa", 999, None
        self.roles = []
        self.members = {UID: SimpleNamespace(guild_permissions=SimpleNamespace(administrator=True, manage_guild=True))}

    def get_member(self, user_id):
        return self.members.get(user_id)

    async def fetch_member(self, user_id):
        raise AssertionError("o membro deveria estar no cache")


class PanelBot(FakeBot):
    def __init__(self, journal, guild, world):
        super().__init__(journal, guild, None)
        self.world, self.ready = world, True
        self.application_id = BOT_ID
        self.identifier, self.exclusive_guild_id = "TOKEN", None  # o único bot é o principal
        self.user = SimpleNamespace(
            id=BOT_ID,
            name="Bot Um",
            mention=BOT_MENTION,
            display_avatar=SimpleNamespace(url="https://cdn.discordapp.com/avatars/7000/x.png"),
        )
        self.config = world.config

    def is_ready(self):
        return self.ready

    async def get_data(self, id_, *, db_name):
        return await self.world.db.get(str(self.user.id), id_, db_name)

    async def update_data(self, id_, data, *, db_name):
        await self.world.db.update(str(self.user.id), id_, data, db_name)

    async def get_global_data(self, id_, *, db_name):
        return await self.world.db.get("global", id_, db_name)

    async def update_global_data(self, id_, data, *, db_name):
        await self.world.db.update("global", id_, data, db_name)


class PanelPool:
    def __init__(self, bots, config):
        self.bots, self.config, self.database = list(bots), config, None
        self.guild_prefix_cache, self.default_skin, self.default_static_skin = {}, "default", "default"

    def get_guild_bots(self, guild_id):
        return [b for b in self.bots if b.get_guild(int(guild_id)) is not None]

    def get_all_bots(self):
        return list(self.bots)

    def api_calls(self):
        return 0


class ChannelWorld:
    """Canais do servidor 100: #geral (11, dentro da categoria Música 66), #jukebox (22), #outro (23), voz (33),
    palco (44), fórum (55) e um canal de OUTRO servidor (99) no cache do bot."""

    def __init__(self, *, features=(), admin=False, manage_channels=True, manage_threads=True, history=True):
        self.j = Journal()
        bot_perms = perms(
            administrator=admin,
            manage_channels=manage_channels,
            manage_threads=manage_threads,
            read_message_history=history,
        )
        self.config = {"INVITE_PERMISSIONS": 332892794064}
        self.guild = PanelGuild(self.j, bot_perms, features)
        self.db = FakeDB(self.j)
        self.bot = PanelBot(self.j, self.guild, self)
        self.pool = PanelPool([self.bot], self.config)
        self.bot.pool = self.pool
        g = self.guild
        self.category = FakeCategoryChannel(self.j, 66, "Música", g)
        self.lobby = FakeText(self.j, 11, "geral", g, category=self.category)
        self.text = FakeText(self.j, 22, "jukebox", g)
        self.other = FakeText(self.j, 23, "outro", g)
        self.voice = FakeVoice(self.j, 33, "voz", g)
        self.stage = FakeStage(self.j, 44, "palco", g)
        self.forum = FakeForum(self.j, 55, "fórum", g)
        self.foreign = FakeText(self.j, 99, "alheio", SimpleNamespace(id=999, me=g.me))
        for channel in (
            self.category,
            self.lobby,
            self.text,
            self.other,
            self.voice,
            self.stage,
            self.forum,
            self.foreign,
        ):
            self.bot.channels[channel.id] = channel
        g.channels = {c.id: c for c in self.bot.channels.values() if c is not self.foreign}
        self.raw = [raw_guild(GID, "Alfa", permissions="8")]
        self.next_message = 9000
        self.gate = None  # asyncio.Event: se definido, o envio do embed ocioso espera por ele
        self.cancelled = False
        self.fail_with = None  # exceção a levantar no envio do embed ocioso
        self.entered = 0
        self.db.seed(str(BOT_ID), GID, SettingsWorld.bot_doc())
        self.db.docs[(str(BOT_ID), GID)]["player_controller"].update({"channel": None, "message_id": None})
        self.db.baseline[(str(BOT_ID), GID)] = copy.deepcopy(self.db.docs[(str(BOT_ID), GID)])
        self.db.seed("global", GID, SettingsWorld.global_doc())
        self.database = self.pool.database = WorldDatabase(self.db)  # o store real; só o banco é de mentira
        self.pool.servers = ServerStore(self.pool)

    # ---- o send_idle_embed real monta embeds enormes: só registramos e devolvemos a mensagem
    async def fake_send_idle_embed(self, target, text="", *, bot, force=False, guild_data=None):
        self.entered += 1
        label = target.label if hasattr(target, "label") else f"mensagem-{target.id}"
        self.j.add("send_idle_embed", label, force)
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.fail_with is not None:
                raise self.fail_with
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if isinstance(target, FakeMessage):
            return target
        self.next_message += 1
        return target.add_message(self.next_message, self.guild.me)

    # ---- estado do banco: a Jukebox mora na seção ``jukebox`` do documento único do servidor
    @property
    def bot_doc(self):
        """O documento por bot (não é mais lido nem gravado pelo painel)."""
        return self.db.doc(str(BOT_ID), GID)

    @property
    def jukebox_doc(self):
        return copy.deepcopy(self.database.servers[GID]["jukebox"])

    async def premigrate(self):
        await self.pool.servers.get(GID)
        await self.pool.servers.jukebox.get(GID)
        await self.pool.servers.jukebox.elect(GID)  # servidor já em uso: o bot do painel já foi eleito
        self.db.reads.clear()
        self.db.writes.clear()
        self.j.clear()

    def configure(self, channel=None, message_id=None):
        """Grava channel/message_id no documento único (como o /jukebox setup deixaria)."""
        doc = self.database.servers[GID]
        doc["jukebox"].update({"channel": channel, "message_id": message_id})
        doc["rev"] += 1
        self.pool.servers._docs.clear()

    def configured_text(self, thread=True):
        message = self.text.add_message(555, self.guild.me, FakeThreadInfo(self.j, "song-requests") if thread else None)
        self.configure("22", "555")
        return message

    def configured_forum(self):
        post = self.forum.add_post(900, "post de pedidos")
        self.bot.channels[900] = post
        self.configure("900", "900")
        return post


class JukeboxBase(SettingsBase):
    """Painel montado com o ChannelWorld; o send_idle_embed do módulo compartilhado é trocado pelo dublê."""

    world_kwargs: dict = {}

    def get_app(self):
        panel = require("empiremuse.panel")
        settings = require("empiremuse.panel.settings").load_panel_settings(self.config_factory())
        self.settings, self.world = settings, ChannelWorld(**self.world_kwargs)
        self.pool, self.db, self.events = self.world.pool, self.world.db, self.world.j
        self.fake = make_fake_oauth(settings, guilds=self.world.raw)
        self.io_loop.run_sync(self.world.premigrate)
        jukebox_channel = require("empiremuse.utils.music.jukebox_channel")
        self.monkeypatch.setitem(
            jukebox_channel._apply_jukebox_channel.__kwdefaults__, "send_idle_embed", self.world.fake_send_idle_embed
        )
        app = panel.build_panel_app(self.pool, settings, oauth_client=self.fake, clock=self.clock)
        self.panel_app = app
        return panel.make_router(tornado.web.Application([]), app)

    # ---- tempo: deixa as tarefas assíncronas andarem
    def settle(self, seconds=0.05):
        self.io_loop.run_sync(lambda: asyncio.sleep(seconds))
