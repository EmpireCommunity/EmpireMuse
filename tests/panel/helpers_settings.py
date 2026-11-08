"""Dublês e utilitários da Etapa 4a (configurações do servidor): DB em memória, cargos, players, formulário.

Aditivo à Etapa 3: reutiliza FakeGuild/FakeBot/FakePool de helpers_servers sem alterar o comportamento deles.
Contrato: BRIEFING-PAINEL-E4.md (E4a). Nada aqui toca rede ou disco.
"""

import asyncio
import copy
import hashlib
import json
import re
from html.parser import HTMLParser
from types import SimpleNamespace
from urllib.parse import urlencode, urljoin, urlsplit

import tornado.web

from empiremuse.rules.voice_status import VoiceStatusConfig
from empiremuse.utils.servers import ServerStore
from tests.panel.helpers import XSRF_META_RE, require
from tests.panel.helpers_login import make_fake_oauth
from tests.panel.helpers_servers import FakeBot, FakeGuild, FakePool, raw_guild, USER_ID
from tests.panel.test_login_flow import LoginBase

UID = int(USER_ID)
NOT_FOUND_TEXT = "Servidor não encontrado ou você não tem permissão para gerenciá-lo."
SAVED_TEXT = "Configurações salvas."
CONFLICT_TEXT = "Estas configurações foram alteradas em outro lugar. Recarregue a página para ver os valores atuais."
CATEGORIES = ("player", "dj", "aparencia", "geral")
HOSTILE_ROLE = "<img src=x onerror=alert(1)>"
HOSTILE_SKIN = "<b>hostil</b>"
ROLE_IDS = [str(5000 + i) for i in range(1, 31)]  # 5001…5030 (5001 tem nome hostil)
GAP_SECONDS = 4  # 20 escritas/min: com 4 s entre POSTs o limite nunca é atingido por acidente


def version_of(fields: dict) -> str:
    """Fórmula do briefing (decisão 4): 16 primeiros hex do SHA-256 do JSON canônico dos campos do formulário."""
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ============================================================================ banco em memória


class FakeDB:
    """Espelha a semântica do BaseDB: get devolve o MESMO dict mutável do cache; só se grava via update.

    Registra violações do contrato do painel (mutar o cache, gravar o próprio dict do cache, id que não é int,
    db_name inesperado) em `violations`, para os testes derrubarem a regressão com mensagem clara.
    """

    def __init__(self, events):
        self.docs = {}
        self.baseline = {}
        self.violations = []
        self.writes = []
        self.reads = []
        self.events = events

    @staticmethod
    def _models():
        db = require("empiremuse.utils.db")
        return db.db_models[db.DBModel.guilds], db.global_db_models[db.DBModel.guilds]

    def seed(self, scope, gid, doc):
        self.docs[(scope, gid)] = doc
        self.baseline[(scope, gid)] = copy.deepcopy(doc)

    def doc(self, scope, gid):
        """Cópia do que está gravado (não o objeto do cache)."""
        return copy.deepcopy(self.docs[(scope, gid)])

    def _check_args(self, gid, db_name):
        if not isinstance(gid, int) or isinstance(gid, bool):
            self.violations.append(f"id do servidor deve ser int, veio {gid!r}")
        if db_name != "guilds":
            self.violations.append(f"db_name inesperado: {db_name!r}")

    async def get(self, scope, gid, db_name):
        self._check_args(gid, db_name)
        await asyncio.sleep(0)  # cede o loop: expõe corrida entre leitura e escrita
        self.reads.append((scope, gid))
        if (scope, gid) not in self.docs:
            bot_model, global_model = self._models()
            self.seed(scope, gid, copy.deepcopy(global_model if scope == "global" else bot_model))
        return self.docs[(scope, gid)]

    async def update(self, scope, gid, data, db_name):
        self._check_args(gid, db_name)
        await asyncio.sleep(0)
        key = (scope, gid)
        if data is self.docs.get(key):
            self.violations.append(f"{key}: gravou o próprio dict do cache (deve gravar uma cópia alterada)")
        if key in self.docs and self.docs[key] != self.baseline[key]:
            self.violations.append(f"{key}: o dict do cache foi mutado antes da gravação")
        self.docs[key] = copy.deepcopy(data)
        self.baseline[key] = copy.deepcopy(data)
        self.writes.append(key)
        self.events.append(("db", "update", scope, gid))


class WorldDatabase:
    """Banco do pool para o ``ServerStore`` real: o documento único em memória (compare-and-set por ``rev``); o documento
    antigo de cada bot (a migração lê o do principal) vem do ``FakeDB``. Gravações do store entram em ``db.writes`` como
    ``("servidor", gid)``."""

    def __init__(self, db):
        self.db = db
        self.servers = {}

    async def find_doc(self, id_, *, db_name, collection):
        await asyncio.sleep(0)
        if db_name == "servers":
            self.db.reads.append(("servidor", int(id_)))
            return copy.deepcopy(self.servers.get(int(id_)))
        return copy.deepcopy(self.db.docs.get((collection, int(id_))))

    async def get_data(self, id_, *, db_name, collection):
        return copy.deepcopy(self.db.docs[(collection, int(id_))])

    async def save_doc_cas(self, id_, doc, *, expected_rev, db_name, collection):
        await asyncio.sleep(0)
        gid, stored = int(id_), self.servers.get(int(id_))
        if (stored is None) != (expected_rev is None) or (stored is not None and stored["rev"] != expected_rev):
            return False
        self.servers[gid] = copy.deepcopy(doc)
        self.db.writes.append(("servidor", gid))
        self.db.events.append(("db", "update", "servidor", gid))
        return True


# ============================================================================ players, bots, pool, servidores


async def assentar() -> None:
    """Cede ao laço até que toda outra tarefa espere um futuro ainda não resolvido (ou tenha terminado): sem relógio, sem adivinhar voltas."""
    import asyncio

    for _ in range(2000):
        outras = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        if all((w := getattr(t, "_fut_waiter", None)) is not None and not w.done() for t in outras):
            return
        await asyncio.sleep(0)
    raise AssertionError("as tarefas de fundo nunca ficaram ociosas")


class FakePlayer:
    def __init__(self, events, label, static=False, skin="default", skin_static="default", voice_channel_id=None):
        self.events, self.label, self.static = events, label, static
        self.listen_along_invite = ""
        self.guild = SimpleNamespace(
            me=SimpleNamespace(voice=SimpleNamespace(channel=SimpleNamespace(id=voice_channel_id)))
        )
        self.skin, self.skin_static = skin, skin_static
        self.calls = []
        self.controller_mode = False
        self.stage_title_event = False
        self.stage_title_template = ""
        self.update = False

    def _note(self, name, **kwargs):
        self.calls.append((name, kwargs))
        self.events.append((self.label, name, kwargs))

    def called(self, name):
        return [kw for n, kw in self.calls if n == name]

    async def process_rpc(self):
        self._note("process_rpc")

    async def process_save_queue(self):
        self._note("process_save_queue")

    async def destroy_message(self):
        self._note("destroy_message")

    async def invoke_np(self, force=False, **kwargs):
        self._note("invoke_np", force=force)

    async def update_stage_topic(self, reconnect=True, clear=False):
        self._note("update_stage_topic", clear=clear)

    portao = None  # um asyncio.Event: com ele, o apply fica preso até o teste soltar

    async def apply_permanent_voice_status(self, cfg: VoiceStatusConfig, *, jitter: bool = False) -> None:
        self._note("apply_permanent_voice_status", cfg=cfg, jitter=jitter)
        if self.portao is not None:
            await self.portao.wait()

    def setup_features(self):
        self._note("setup_features")

    def setup_hints(self):
        self._note("setup_hints")

    def process_hint(self):
        self._note("process_hint")

    def set_command_log(self, **kwargs):
        self._note("set_command_log")


class SettingsGuild(FakeGuild):
    """Servidor do cache do bot, com cargos (`roles`, `get_role`, `default_role`)."""

    def __init__(self, guild_id, name, roles=(), **kwargs):
        super().__init__(guild_id, name, **kwargs)
        self.default_role = SimpleNamespace(id=int(guild_id), name="@everyone")
        self.roles = [self.default_role] + list(roles)
        self.channels = {}

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    def get_role(self, role_id):
        return next((r for r in self.roles if r.id == role_id), None)


class SettingsBot(FakeBot):
    def __init__(self, bot_id, name, world, guilds=()):
        super().__init__(bot_id, name, guilds=guilds)
        self.world = world
        self.config = world.config
        self.pool = None
        self.music = SimpleNamespace(players={})
        self.player_skins = {
            n: SimpleNamespace(preview=f"https://example.invalid/{i}.png")
            for i, n in enumerate(("default", "neon", "dark", HOSTILE_SKIN))
        }
        self.player_static_skins = {
            n: SimpleNamespace(preview=f"https://example.invalid/s{i}.png")
            for i, n in enumerate(("default", "green", "blue"))
        }
        self.default_skin = self.default_static_skin = "default"
        self.identifier = "TOKEN" if int(bot_id) == 1 else f"TOKEN_RESERVE_{bot_id}"  # o bot 1 é o principal

    def get_channel(self, channel_id):
        self.calls["get_channel"] += 1
        return self.world.channels.get(channel_id)

    async def get_data(self, id_, *, db_name):
        return await self.world.db.get(str(self.user.id), id_, db_name)

    async def update_data(self, id_, data, *, db_name):
        return await self.world.db.update(str(self.user.id), id_, data, db_name)

    async def get_global_data(self, id_, *, db_name):
        return await self.world.db.get("global", id_, db_name)

    async def update_global_data(self, id_, data, *, db_name):
        return await self.world.db.update("global", id_, data, db_name)


class SettingsPool(FakePool):
    def __init__(self, bots=()):
        super().__init__(bots)
        self.guild_prefix_cache = {}
        self.user_prefix_cache = {}
        self.default_skin = self.default_static_skin = "default"

    def get_all_bots(self):
        return list(self.bots)


class SettingsWorld:
    """Dois bots (Um=1, Dois=2). Servidores (usuário 1001):
    100 Alfa (só bot 1, admin) · 800 Ômega (bots 1 e 2, admin) · 200 Beta (só bot 2, MANAGE_GUILD)
    600 Rebaixado (bot 1, membro comum: OAuth diz admin) · 300 Gama (sem bots) · 500 Fora (bot 1, fora da sessão).
    """

    def __init__(self):
        self.events = []
        self.db = FakeDB(self.events)
        self.config = {
            "INVITE_PERMISSIONS": 332892794064,
            "IGNORE_SKINS": "dark",
            "IGNORE_STATIC_SKINS": "blue",
            "USER_FAV_MAX_NAME_LENGTH": 35,
            "USER_FAV_MAX_URL_LENGTH": 90,
            "MAX_USER_FAVS": 10,
        }
        self.channels = {
            7777: SimpleNamespace(id=7777, name="pedidos"),
            123: SimpleNamespace(id=123, name="Sala <i>DJ</i>"),
            456: SimpleNamespace(id=456, name="Sala 2"),
        }
        for channel in self.channels.values():
            channel.guild_id = None
        roles = [
            SimpleNamespace(id=int(rid), name=HOSTILE_ROLE if i == 0 else f"Cargo {i + 1}")
            for i, rid in enumerate(ROLE_IDS)
        ]
        self.g100 = SettingsGuild(100, "Alfa", roles, owner_id=999)
        self.g800a = SettingsGuild(800, "Ômega", roles, owner_id=999)
        self.g800b = SettingsGuild(800, "Ômega", roles, owner_id=999)
        self.g200 = SettingsGuild(200, "Beta", roles, owner_id=999)
        self.g600 = SettingsGuild(600, "Rebaixado", roles, owner_id=999)
        self.g500 = SettingsGuild(500, "Fora", roles, owner_id=999)
        for guild in (self.g100, self.g800a, self.g800b):
            guild.add_member(UID, administrator=True)
        self.g200.add_member(UID, manage_guild=True)
        self.g600.add_member(UID)
        self.g500.add_member(UID, administrator=True)
        for guild in (self.g100, self.g800a, self.g800b):
            guild.members[UID].guild_permissions.manage_channels = True  # administrador implica o poder
        for guild in (self.g200, self.g600, self.g500):
            guild.members[UID].guild_permissions.manage_channels = guild is self.g500
        for guild in (self.g100, self.g800a, self.g800b, self.g200, self.g600, self.g500):
            guild.channels = self.channels
        self.bot1 = SettingsBot(1, "Bot Um", self, [self.g100, self.g800a, self.g600, self.g500])
        self.bot2 = SettingsBot(2, "Bot Dois", self, [self.g800b, self.g200])
        self.pool = SettingsPool([self.bot1, self.bot2])
        self.pool.config = self.config  # no bot real, bot.config é pool.config
        for bot in self.pool.bots:
            bot.pool = self.pool
        self.database = self.pool.database = WorldDatabase(self.db)
        self.pool.servers = ServerStore(self.pool)  # o store real; só o banco é de mentira
        self.p100 = FakePlayer(self.events, "p100/b1", voice_channel_id=123)
        self.p800a = FakePlayer(self.events, "p800/b1", voice_channel_id=456)
        self.p800b = FakePlayer(self.events, "p800/b2", voice_channel_id=123)
        self.bot1.music.players.update({100: self.p100, 800: self.p800a})
        self.bot2.music.players.update({800: self.p800b})
        self.raw = [
            raw_guild(100, "Alfa", permissions="8"),
            raw_guild(800, "Ômega", permissions="8"),
            raw_guild(200, "Beta", permissions="32"),
            raw_guild(600, "Rebaixado", permissions="8"),
            raw_guild(300, "Gama", permissions="8"),
            raw_guild(500, "Fora", owner=False, permissions="1024"),
        ]
        self._seed()
        self.db.docs[("global", 800)]["listen_along_invites"] = {
            "123": "https://discord.gg/aaa",
            "456": "https://discord.gg/bbb",
        }
        self.db.docs[("global", 200)]["listen_along_invites"] = {"123": "https://discord.gg/ccc"}
        for key in (("global", 800), ("global", 200)):
            self.db.baseline[key] = copy.deepcopy(self.db.docs[key])

    async def premigrate(self):
        """Estado de partida de um servidor já em uso: ``settings`` migrado do principal e contadores zerados."""
        for gid in (100, 800, 200, 600):
            await self.pool.servers.get(gid)
            await self.pool.servers.playlists.list(gid)  # migra também as playlists (do principal)
        self.db.reads.clear()
        self.db.writes.clear()
        self.events.clear()

    def server_playlists(self, gid):
        """Nomes das playlists gravadas no documento único, por ``order``."""
        doc = self.database.servers[gid]
        return [p["name"] for p in sorted(doc["playlists"], key=lambda p: p["order"])]

    def set_playlists(self, gid, docs):
        """Outro escritor troca a lista (documentos crus) sem passar pelo painel."""
        doc = self.database.servers[gid]
        doc["playlists"] = copy.deepcopy(docs)
        doc["rev"] += 1
        self.pool.servers._docs.clear()

    def server_settings(self, gid):
        """Cópia do ``settings`` gravado no documento único."""
        return copy.deepcopy(self.database.servers[gid]["settings"])

    def external_server_change(self, gid, **fields):
        """Outro escritor (comando no Discord) muda o documento único sem passar pelo painel."""
        doc = self.database.servers[gid]
        doc["settings"].update(fields)
        doc["rev"] += 1
        self.pool.servers._docs.clear()

    def _seed(self):
        self.db.seed("1", 100, self.bot_doc(volume=100, djroles=["5002"]))
        self.db.seed("1", 800, self.bot_doc(volume=60, autoplay=True, djroles=["5003"]))
        self.db.seed("2", 800, self.bot_doc(volume=80, djroles=["5004", "5005"]))
        self.db.seed("2", 200, self.bot_doc())
        self.db.docs[("1", 800)]["player_controller"]["fav_links"] = {
            "Um": {"url": "https://example.com/um", "description": "do bot 1"}
        }
        self.db.docs[("2", 800)]["player_controller"]["fav_links"] = {
            "Dois": {"url": "https://example.com/dois", "description": "do bot 2"}
        }
        for key in (("1", 800), ("2", 800)):
            self.db.baseline[key] = copy.deepcopy(self.db.docs[key])
        self.db.seed("1", 600, self.bot_doc())
        for gid in (100, 800, 200, 600):
            self.db.seed("global", gid, self.global_doc())

    @staticmethod
    def bot_doc(volume=100, autoplay=False, djroles=()):
        doc = copy.deepcopy(FakeDB._models()[0])
        doc.update(
            {"autoplay": autoplay, "default_player_volume": volume, "djroles": list(djroles), "extra_flag": "intacto"}
        )
        doc["player_controller"].update(
            {
                "channel": "7777",
                "message_id": "8888",
                "skin": "default",
                "static_skin": "default",
                "fav_links": {"Fixo": {"url": "https://example.com/x", "description": "descrição fixa"}},
            }
        )
        return doc

    @staticmethod
    def global_doc():
        doc = copy.deepcopy(FakeDB._models()[1])
        doc.update(
            {
                "prefix": "!",
                "custom_skins": {"meu": "DADOS-LEGADOS"},
                "custom_skins_static": {},
                "listen_along_invites": {"123": "https://discord.gg/abc"},
                "extra_flag": "intacto",
            }
        )
        return doc


# ============================================================================ formulário (um "navegador" mínimo)


class Form:
    def __init__(self, action, method, controls):
        self.action, self.method, self.controls = action, method, controls

    def names(self):
        return {c["name"] for c in self.controls if c.get("name")}

    def has(self, name):
        return name in self.names()

    def control(self, name):
        return next((c for c in self.controls if c.get("name") == name), None)

    def pairs(self, **overrides):
        """Campos como o navegador enviaria; `overrides` troca TODOS os valores de um nome (lista ou valor único;
        None remove o campo, como um checkbox desmarcado)."""
        result = []
        for control in self.controls:
            name = control.get("name")
            if not name or control.get("disabled") or name in overrides:
                continue
            result.extend((name, value) for value in control["values"])
        for name, value in overrides.items():
            if value is None:
                continue
            for item in value if isinstance(value, list) else [value]:
                result.append((name, item))
        return result

    def value(self, name):
        control = self.control(name)
        return None if control is None or not control["values"] else control["values"][0]

    def values(self, name):
        """Valores enviados para `name` (agrega vários controles de mesmo nome, como checkboxes)."""
        return [v for c in self.controls if c.get("name") == name and not c.get("disabled") for v in c["values"]]

    def choices(self, name):
        """Valores oferecidos para `name`: opções de <select> ou valor de cada checkbox/radio."""
        offered = []
        for c in self.controls:
            if c.get("name") != name:
                continue
            if c["tag"] == "select":
                offered.extend(o["value"] for o in c["options"])
            elif c["type"] in ("checkbox", "radio"):
                offered.append(c["attrs"].get("value", "on"))
        return offered

    def options(self, name):
        control = self.control(name)
        return [] if control is None else [o["value"] for o in control.get("options", [])]


class _FormParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms, self._form, self._select, self._textarea, self._option = [], None, None, None, None

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        if tag == "form":
            self._form = Form(a.get("action"), a.get("method", "get").lower(), [])
            self.forms.append(self._form)
        elif self._form is None:
            return
        elif tag == "input":
            kind = a.get("type", "text").lower()
            if kind in ("submit", "button", "image", "reset", "file"):
                return
            if kind in ("checkbox", "radio"):
                values = [a.get("value", "on")] if "checked" in a else []
            else:
                values = [a.get("value", "")]
            self._form.controls.append(
                {
                    "tag": "input",
                    "type": kind,
                    "name": a.get("name"),
                    "values": values,
                    "attrs": a,
                    "disabled": "disabled" in a,
                    "options": [],
                }
            )
        elif tag == "select":
            self._select = {
                "tag": "select",
                "type": "select",
                "name": a.get("name"),
                "values": [],
                "attrs": a,
                "multiple": "multiple" in a,
                "disabled": "disabled" in a,
                "options": [],
            }
            self._form.controls.append(self._select)
        elif tag == "option" and self._select is not None:
            self._option = {"value": a.get("value"), "selected": "selected" in a, "attrs": a, "label": ""}
            self._select["options"].append(self._option)
        elif tag == "textarea":
            self._textarea = {
                "tag": "textarea",
                "type": "textarea",
                "name": a.get("name"),
                "values": [""],
                "attrs": a,
                "disabled": "disabled" in a,
                "options": [],
            }
            self._form.controls.append(self._textarea)

    def handle_data(self, data):
        if self._textarea is not None:
            self._textarea["values"][0] += data
        elif self._option is not None:
            self._option["label"] += data

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        elif tag == "option":
            self._option = None
        elif tag == "textarea" and self._textarea is not None:
            text = self._textarea["values"][0]
            self._textarea["values"][0] = text[1:] if text.startswith("\n") else text
            self._textarea = None
        elif tag == "select" and self._select is not None:
            select = self._select
            for option in select["options"]:
                if option["value"] is None:
                    option["value"] = option["label"].strip()
            chosen = [o["value"] for o in select["options"] if o["selected"]]
            if not chosen and select["options"] and not select["multiple"]:
                chosen = [select["options"][0]["value"]]
            select["values"] = chosen
            self._select = None


def parse_forms(body: str):
    parser = _FormParser()
    parser.feed(body)
    return parser.forms


def element_text(body: str, element_id: str) -> str:
    """Texto do elemento com o id dado, ou '' se não existir."""
    match = re.search(r'id="%s"[^>]*>(.*?)</' % re.escape(element_id), body, re.S)
    return re.sub(r"<[^>]+>", "", match.group(1)).strip() if match else ""


def invites(body: str):
    """Convites (href para discord.com/oauth2/authorize) da página: lista de {'base','params'}."""
    from html import unescape
    from urllib.parse import parse_qs

    found = []
    for href in re.findall(r'href="(https://discord\.com/oauth2/authorize[^"]*)"', body):
        parts = urlsplit(unescape(href))
        found.append(
            {
                "base": f"{parts.scheme}://{parts.netloc}{parts.path}",
                "params": {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()},
            }
        )
    return found


# ============================================================================ caso de teste base


class SettingsBase(LoginBase):
    """Painel montado com o SettingsWorld; `logged_in()` entra como o usuário 1001."""

    raw_override = None

    def get_app(self):
        panel = require("empiremuse.panel")
        settings = require("empiremuse.panel.settings").load_panel_settings(self.config_factory())
        self.settings, self.world = settings, SettingsWorld()
        self.pool, self.db, self.events = self.world.pool, self.world.db, self.world.events
        raw = self.world.raw if self.raw_override is None else self.raw_override
        self.fake = make_fake_oauth(settings, guilds=raw)
        self.io_loop.run_sync(self.world.premigrate)
        app = panel.build_panel_app(self.pool, settings, oauth_client=self.fake, clock=self.clock)
        return panel.make_router(tornado.web.Application([]), app)

    def tearDown(self):
        violations = list(self.db.violations)
        super().tearDown()
        assert not violations, "violações do contrato de dados:\n  " + "\n  ".join(violations)

    # ---- navegação
    def logged_in(self):
        browser = self.browser()
        assert self.login(browser).code == 302
        return browser

    @staticmethod
    def url(gid, category, bot=None):
        return f"/painel/servidores/{gid}/{category}" + (f"?bot={bot}" if bot is not None else "")

    def open_form(self, browser, gid, category, field, bot=None):
        """GET da categoria e o formulário que contém `field`. Devolve (url, form, response)."""
        url = self.url(gid, category, bot)
        response = browser.get(url)
        assert response.code == 200, f"GET {url} deveria ser 200, veio {response.code}"
        body = response.body.decode()
        form = next((f for f in parse_forms(body) if f.has(field)), None)
        assert form is not None, f"nenhum formulário com o campo {field!r} em {url}"
        return url, form, response

    def xsrf_header(self, browser):
        token = XSRF_META_RE.search(browser.get("/painel/").body.decode()).group(1)
        return {"X-XSRFToken": token}

    def submit(self, browser, page_url, form, *, htmx=False, gap=GAP_SECONDS, headers=None, pairs=None, **overrides):
        """POST como o navegador: ação do formulário (relativa a `page_url`), campos atuais + `overrides`
        (qualquer nome de campo, inclusive `url`)."""
        if gap:
            self.clock.advance(gap)
        target = urljoin("http://x" + page_url, form.action or "")
        parts = urlsplit(target)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        body = urlencode(form.pairs(**overrides) if pairs is None else pairs)
        merged = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
        if htmx:
            merged["HX-Request"] = "true"
        return browser.post(path, body=body, headers=merged)

    def save(self, browser, gid, category, field, *, bot=None, htmx=False, **overrides):
        url, form, _ = self.open_form(browser, gid, category, field, bot)
        return self.submit(browser, url, form, htmx=htmx, **overrides)


# ============================================================================ regras dos comandos (contrato)
#
# São o contrato que o painel espelha. Quem prova que os COMANDOS cumprem é o teste de comportamento de
# tests/panel/test_settings_rules.py (TestCommandRules): ele chama os callbacks reais com dublês e compara com estas
# constantes. Nenhum teste lê o texto do código-fonte, então a refatoração das regras (F3-5b) não os quebra.

COMMAND_VOLUMES = frozenset(range(5, 101, 5)) | frozenset(range(110, 151, 10))
COMMAND_VOICE_PLACEHOLDERS = (
    "{track.title}",
    "{track.timestamp}",
    "{track.emoji}",
    "{track.author}",
    "{track.duration}",
    "{track.source}",
    "{track.playlist}",
    "{requester.name}",
    "{requester.id}",
)
COMMAND_VOICE_MAX_LENGTH = 496
COMMAND_PREFIX_MAX_LENGTH = 5


def command_voice_placeholders():
    return COMMAND_VOICE_PLACEHOLDERS


def command_voice_max_length():
    return {COMMAND_VOICE_MAX_LENGTH}


def command_prefix_max_length():
    return COMMAND_PREFIX_MAX_LENGTH


# ============================================================================ E4b: formulários identificados por `acao`


def acao_form(body: str, acao: str, nome=None):
    """Formulário cujo campo oculto/lido `acao` vale `acao` (e `nome` igual, quando dado)."""
    for form in parse_forms(body):
        if form.value("acao") == acao and (nome is None or nome in (form.value("nome"), form.value("alvo"))):
            return form
    return None


def acao_forms(body: str, acao: str):
    return [f for f in parse_forms(body) if f.value("acao") == acao]
