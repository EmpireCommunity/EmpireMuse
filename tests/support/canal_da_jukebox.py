"""Dublês para caracterizar /setup e /reset (modules/music_settings.py:375-1010) SEM Discord.

Canais, threads e fóruns são subclasses REAIS de disnake (os `isinstance` do comando funcionam) construídas sem
`__init__`; permissões são `disnake.Permissions` reais (a iteração devolve os nomes canônicos, como em produção).
Tudo o que muda o mundo vai para um `Journal` ordenado, para provar a ordem banco × Discord.
"""

import asyncio
import copy
from types import SimpleNamespace

import disnake

GUILD_ID = 5000
BOT_ID = 7000
ACTOR_ID = 9000
ACTOR_MENTION = f"<@{ACTOR_ID}>"
BOT_MENTION = f"<@{BOT_ID}>"


class Journal(list):
    def add(self, *event):
        self.append(event)

    def names(self):
        return [e[0] for e in self]

    def of(self, name):
        return [e for e in self if e[0] == name]

    def index(self, name, *args):
        return next(i for i, e in enumerate(self) if e[0] == name and e[1 : 1 + len(args)] == args)


def perms(**flags):
    return disnake.Permissions(**flags)


FULL_TEXT = dict(
    view_channel=True,
    send_messages=True,
    embed_links=True,
    read_message_history=True,
    manage_permissions=True,
    manage_threads=True,
    create_public_threads=True,
    send_messages_in_threads=True,
    manage_messages=True,
)


# ============================================================================ mensagens e threads (planos)


class FakeThreadInfo:
    """Thread de uma mensagem (message.thread): só o que o comando usa."""

    def __init__(self, journal, label, owner_id=BOT_ID, locked=False, archived=False, perms_=None):
        self.journal, self.label = journal, label
        self.owner_id, self.locked, self.archived = owner_id, locked, archived
        self._perms = perms_ or perms(manage_threads=True)
        self.fail = None

    def permissions_for(self, member):
        return self._perms

    async def edit(self, **kwargs):
        self.journal.add("thread.edit", self.label, kwargs)
        if self.fail:
            raise self.fail

    async def delete(self, **kwargs):
        self.journal.add("thread.delete", self.label, kwargs)


class FakeMessage:
    def __init__(self, journal, id_, channel, author, thread=None):
        self.journal, self.id, self.channel, self.author, self.thread = journal, id_, channel, author, thread
        self.guild = channel.guild
        self.fail_edit = None
        self.fail_delete = None

    async def edit(self, **kwargs):
        self.journal.add("message.edit", self.id, kwargs)
        if self.fail_edit:
            raise self.fail_edit

    async def delete(self):
        self.journal.add("message.delete", self.id)
        if self.fail_delete:
            raise self.fail_delete

    async def create_thread(self, *, name, auto_archive_duration=None):
        self.journal.add("message.create_thread", self.id, name, auto_archive_duration)
        self.thread = FakeThreadInfo(self.journal, f"thread-de-{self.id}")
        return self.thread


# ============================================================================ canais (classes reais de disnake)


class ChannelMixin:
    """Comportamento comum; precisa vir ANTES da classe do disnake na herança."""

    def _setup(self, journal, id_, name, guild, perms_, category=None):
        self.journal, self.id, self.name, self.guild = journal, id_, name, guild
        self._perms, self._category = perms_, category
        self.messages = {}  # id -> FakeMessage
        self.history_list = []  # mais novo primeiro
        self.fail_edit = None
        self.fail_delete = None
        self.fail_purge = None
        self.label = f"#{name}"

    @property
    def category(self):
        return self._category

    def permissions_for(self, member, /):
        return self._perms

    async def edit(self, **kwargs):
        self.journal.add("channel.edit", self.label, kwargs)
        if self.fail_edit:
            raise self.fail_edit

    async def delete(self, *, reason=None):
        self.journal.add("channel.delete", self.label, reason)
        if self.fail_delete:
            raise self.fail_delete

    async def purge(self, *, limit=None, check=None, **kwargs):
        self.journal.add("channel.purge", self.label, limit, check)
        if self.fail_purge:
            raise self.fail_purge
        return []

    async def history(self, *, limit=None, **kwargs):
        self.journal.add("channel.history", self.label, limit)
        for message in list(self.history_list):
            yield message

    async def fetch_message(self, id_, /):
        message = self.messages.get(id_)
        if message is None:
            raise disnake.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")
        return message

    def add_message(self, id_, author, thread=None):
        message = FakeMessage(self.journal, id_, self, author, thread)
        self.messages[id_] = message
        self.history_list.insert(0, message)
        return message


class FakeText(ChannelMixin, disnake.TextChannel):
    def __init__(self, journal, id_, name, guild, perms_=None, category=None):
        self._setup(journal, id_, name, guild, perms_ or perms(**FULL_TEXT), category)


class FakeVoice(ChannelMixin, disnake.VoiceChannel):
    def __init__(self, journal, id_, name, guild, perms_=None, category=None):
        self._setup(journal, id_, name, guild, perms_ or perms(**FULL_TEXT), category)


class FakeStage(ChannelMixin, disnake.StageChannel):
    def __init__(self, journal, id_, name, guild, perms_=None, category=None):
        self._setup(journal, id_, name, guild, perms_ or perms(**FULL_TEXT), category)


class FakeThread(ChannelMixin, disnake.Thread):
    """Post de fórum (ou thread): parent é o fórum."""

    def __init__(self, journal, id_, name, guild, parent, owner_id=BOT_ID, locked=False, archived=False):
        self._setup(journal, id_, name, guild, perms(**FULL_TEXT))
        self._parent, self.owner_id, self.locked, self.archived = parent, owner_id, locked, archived

    @property
    def parent(self):
        return self._parent

    async def edit(self, **kwargs):
        self.journal.add("thread.edit", self.label, kwargs)
        if self.fail_edit:
            raise self.fail_edit


class FakeForum(ChannelMixin, disnake.ForumChannel):
    def __init__(self, journal, id_, name, guild, perms_=None, category=None):
        self._setup(journal, id_, name, guild, perms_ or perms(**FULL_TEXT), category)
        self.thread_list, self.archived_list = [], []
        self.created = []

    @property
    def threads(self):
        return list(self.thread_list)

    async def archived_threads(self, *, limit=None, **kwargs):
        self.journal.add("forum.archived_threads", self.label, limit)
        for thread in self.archived_list:
            yield thread

    def add_post(self, id_, name, owner_id=BOT_ID, locked=False, archived=False, with_message=True):
        post = FakeThread(self.journal, id_, name, self.guild, self, owner_id, locked, archived)
        if with_message:
            post.messages[id_] = FakeMessage(self.journal, id_, post, SimpleNamespace(id=owner_id), None)
        (self.archived_list if archived else self.thread_list).append(post)
        return post

    async def create_thread(self, *, name, content=None, auto_archive_duration=None, slowmode_delay=None, **kw):
        self.journal.add("forum.create_thread", self.label, name, content, auto_archive_duration, slowmode_delay)
        post = FakeThread(self.journal, 8800 + len(self.created), name, self.guild, self)
        post.messages[post.id] = FakeMessage(self.journal, post.id, post, SimpleNamespace(id=BOT_ID), None)
        self.created.append(post)
        self.thread_list.append(post)
        return SimpleNamespace(thread=post, message=post.messages[post.id])


class FakeCategoryChannel(ChannelMixin, disnake.CategoryChannel):
    """Categoria REAL (isinstance CategoryChannel) que cria canais dentro dela."""

    def __init__(self, journal, id_, name, guild, perms_=None):
        self._setup(journal, id_, name, guild, perms_ or perms(send_messages=True))

    async def create_text_channel(self, name, **kwargs):
        return self.guild.make("text", name, self.label, kwargs)

    async def create_voice_channel(self, name, **kwargs):
        return self.guild.make("voice", name, self.label, kwargs)

    async def create_stage_channel(self, name, **kwargs):
        return self.guild.make("stage", name, self.label, kwargs)


class FakeCategory:
    """Categoria do canal onde o comando foi usado: cria canais novos."""

    def __init__(self, journal, guild, perms_=None):
        self.journal, self.guild, self._perms = journal, guild, perms_ or perms(send_messages=True)
        self.label = "categoria"

    def permissions_for(self, member, /):
        return self._perms

    async def create_text_channel(self, name, **kwargs):
        return self.guild.make("text", name, "categoria", kwargs)

    async def create_voice_channel(self, name, **kwargs):
        return self.guild.make("voice", name, "categoria", kwargs)

    async def create_stage_channel(self, name, **kwargs):
        return self.guild.make("stage", name, "categoria", kwargs)


class FakeMember:
    def __init__(self, id_, mention, guild_permissions=None):
        self.id, self.mention = id_, mention
        self.guild_permissions = guild_permissions or perms()
        self.voice = None


class FakeGuild:
    def __init__(self, journal, bot_perms, features=()):
        self.journal, self.id, self.features = journal, GUILD_ID, list(features)
        self.me = FakeMember(BOT_ID, BOT_MENTION, bot_perms)
        self.public_updates_channel = None
        self.rules_channel = None
        self.channels = {}
        self.registry = None  # preenchido pelo bot: novos canais ficam visíveis em bot.get_channel
        self._next = 6000

    def make(self, kind, name, where, kwargs):
        self._next += 1
        cls = {"text": FakeText, "voice": FakeVoice, "stage": FakeStage}[kind]
        channel = cls(self.journal, self._next, name, self)
        self.channels[channel.id] = channel
        if self.registry is not None:
            self.registry[channel.id] = channel
        self.journal.add(f"create_{kind}_channel", where, name, kwargs)
        return channel

    async def create_text_channel(self, name, **kwargs):
        return self.make("text", name, "raiz", kwargs)

    async def create_voice_channel(self, name, **kwargs):
        return self.make("voice", name, "raiz", kwargs)

    async def create_stage_channel(self, name, **kwargs):
        return self.make("stage", name, "raiz", kwargs)


# ============================================================================ banco, player, bot


class FakeDB:
    """Mesma semântica do BaseDB: get devolve o MESMO dict; update registra e guarda uma cópia."""

    def __init__(self, journal, channel=None, message_id=None):
        from empiremuse.utils.db import DBModel, db_models

        doc = copy.deepcopy(db_models[DBModel.guilds])
        doc["player_controller"]["channel"] = channel
        doc["player_controller"]["message_id"] = message_id
        doc["player_controller"]["fav_links"] = {"Fixo": {"url": "https://example.com/x", "description": "d"}}
        doc["djroles"] = ["1"]
        self.doc, self.journal = doc, journal
        self.updates = []

    async def get(self):
        return self.doc

    async def update(self, data):
        self.updates.append(copy.deepcopy(data))
        self.journal.add("db.update", copy.deepcopy(data["player_controller"]))
        self.doc = copy.deepcopy(data)


class FakePlayer:
    def __init__(self, journal, guild, text_channel, static=False, message=None, voice_channel=None):
        self.journal, self.static, self.message, self.text_channel = journal, static, message, text_channel
        self.guild = guild
        guild.me.voice = SimpleNamespace(channel=voice_channel)

    def setup_hints(self):
        self.journal.add("player.setup_hints")

    def setup_features(self):
        self.journal.add("player.setup_features")

    def process_hint(self):
        self.journal.add("player.process_hint")

    async def invoke_np(self, force=False, **kw):
        self.journal.add("player.invoke_np", force)

    async def connect(self, channel_id):
        self.journal.add("player.connect", channel_id)


class FakeRequestStore:
    """O ``pool.servers.jukebox`` do canal de pedidos sobre o ``FakeDB`` (o bot é o único e portanto o bot do painel)."""

    def __init__(self, bot):
        self.bot = bot
        self._lock = asyncio.Lock()

    def operation(self, gid):
        return self._lock

    async def panel_bot(self, gid):
        return self.bot

    async def is_panel_bot(self, gid, bot):
        return True

    async def get(self, gid):
        from empiremuse.utils.server_jukebox import JukeboxConfig

        controller = self.bot.db.doc["player_controller"]
        ident = lambda value: int(value) if value else None
        return JukeboxConfig(ident(controller["channel"]), ident(controller["message_id"]), self.bot.user.id, 1)

    async def set(self, gid, channel_id, message_id, *, expected_rev=None, expected=None):
        doc = copy.deepcopy(self.bot.db.doc)
        doc["player_controller"]["channel"] = str(channel_id) if channel_id else None
        doc["player_controller"]["message_id"] = str(message_id) if channel_id and message_id else None
        await self.bot.db.update(doc)
        return await self.get(gid)

    async def clear(self, gid, *, expected_rev=None, expected=None):
        return await self.set(gid, None, None)


class FakeBot:
    def __init__(self, journal, guild, db):
        self.journal, self.guild, self.db = journal, guild, db
        self.user = SimpleNamespace(id=BOT_ID, name="Bot Um", mention=BOT_MENTION)
        self.appinfo = SimpleNamespace(bot_public=True)
        self.music = SimpleNamespace(players={})
        self.channels = {}
        guild.registry = self.channels
        self.fetchable = {}  # canais que só a API devolve (fora do cache)
        self.script = {}  # evento -> lista de interações entregues por wait_for (exceção = levantar)
        self.waits = []
        self.pool = SimpleNamespace(
            get_guild_bots=lambda gid: [self], servers=SimpleNamespace(jukebox=FakeRequestStore(self))
        )

    @property
    def loop(self):
        return asyncio.get_running_loop()

    def get_guild(self, gid):
        return self.guild if gid == self.guild.id else None

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def fetch_channel(self, cid):
        if cid in self.channels:
            return self.channels[cid]
        if cid in self.fetchable:
            return self.fetchable[cid]
        raise disnake.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Channel")

    def get_color(self, *a, **k):
        return 0

    async def get_data(self, id_, *, db_name):
        return await self.db.get()

    async def update_data(self, id_, data, *, db_name):
        await self.db.update(data)

    async def wait_for(self, event, *, check=None, timeout=None):
        self.waits.append((event, timeout, check))
        queue = self.script.get(event)
        if queue:
            item = queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            if check is not None:
                assert check(item), f"o check de {event} recusou a interação do teste"
            return item
        await asyncio.Future()  # nunca chega: a espera expira


# ============================================================================ interações


class FakeResponse:
    def __init__(self, journal, who):
        self.journal, self.who, self.done = journal, who, False

    def is_done(self):
        return self.done

    async def defer(self, *, ephemeral=False):
        self.done = True
        self.journal.add("response.defer", self.who, ephemeral)

    async def send_modal(self, **kwargs):
        self.done = True
        self.journal.add("response.send_modal", self.who, kwargs)

    async def edit_message(self, **kwargs):
        self.done = True
        self.journal.add("response.edit_message", self.who, kwargs)


class FakeAuthor:
    id = ACTOR_ID
    mention = ACTOR_MENTION

    def __str__(self):
        return "Autor#0001"


class ClientProxy:
    """`inter.bot`: o cliente do disnake visto pela interação (pool, wait_for, loop)."""

    def __init__(self, bot):
        self.pool, self.wait_for = bot.pool, bot.wait_for

    @property
    def loop(self):
        return asyncio.get_running_loop()


class BaseInter:
    def __init__(self, journal, bot, guild, who, id_, channel=None):
        self.journal, self.who = journal, who
        self.id, self.token = id_, f"token-{id_}"
        self.guild_id = guild.id
        self.channel = channel
        self.author = FakeAuthor()
        self.response = FakeResponse(journal, who)
        self.bot = ClientProxy(bot)

    async def edit_original_message(self, *args, **kwargs):
        self.journal.add("inter.edit_original_message", self.who, args, kwargs)
        return SimpleNamespace(id=4242, edit=self._edit_select, delete=self._delete_select)

    async def _edit_select(self, **kwargs):
        self.journal.add("select_message.edit", kwargs)

    async def _delete_select(self):
        self.journal.add("select_message.delete")

    async def send(self, *args, **kwargs):
        self.journal.add("inter.send", self.who, args, kwargs)


class CommandInter(BaseInter):
    """O slash: tem `application_command.reset_cooldown`."""

    def __init__(self, journal, bot, guild, channel):
        super().__init__(journal, bot, guild, "slash", 777, channel)
        self.application_command = SimpleNamespace(reset_cooldown=lambda inter: journal.add("cooldown.reset"))
