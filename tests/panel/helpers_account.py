"""Dublês da Etapa 5 (Conta do usuário): bot que guarda dados GLOBAIS de usuários (DBModel.users) em memória.

Contrato: BRIEFING-PAINEL-E5.md. O FakeDB devolve o MESMO dict do cache (como o BaseDB) e registra violações
(mutar o cache, gravar o próprio dict, id que não é int, db_name diferente de "users").
"""

import copy
from types import SimpleNamespace

import tornado.web

from tests.panel.helpers import require
from tests.panel.helpers_login import make_fake_oauth
from tests.panel.helpers_servers import raw_guild
from tests.panel.helpers_settings import FakeDB, SettingsBase, UID

OTHER_UID = 2002
SESSIONKEY = "SK-SEGREDO-0123456789abcdef"
RPC_TOKEN_OLD = "T" * 50
FAV_CONFIG = {
    "MAX_USER_FAVS": 10,
    "USER_FAV_MAX_NAME_LENGTH": 35,
    "USER_FAV_MAX_URL_LENGTH": 90,
    "MAX_USER_INTEGRATIONS": 10,
    "ENABLE_RPC_COMMAND": True,
    "ENABLE_RPC_AUTH": True,
    "RPC_PUBLIC_URL": "wss://rpc.example.com/ws",
    "RPC_SERVER": "",
    "INVITE_PERMISSIONS": 332892794064,
}


class UserFakeDB(FakeDB):
    """FakeDB do escopo `global` com documentos de USUÁRIOS (modelo `global_db_models[users]`)."""

    def _check_args(self, gid, db_name):
        if not isinstance(gid, int) or isinstance(gid, bool):
            self.violations.append(f"id do usuário deve ser int, veio {gid!r}")
        if db_name != "users":
            self.violations.append(f"db_name inesperado: {db_name!r} (esperado 'users')")

    async def get(self, scope, gid, db_name):
        if (scope, gid) not in self.docs:
            from empiremuse.utils.db import DBModel, global_db_models

            self.seed(scope, gid, copy.deepcopy(global_db_models[DBModel.users]))
        return await super().get(scope, gid, db_name)


class FakeRpcPlayer:
    def __init__(self, journal, label, user_ids):
        self.journal, self.label = journal, label
        self.guild = SimpleNamespace(
            me=SimpleNamespace(
                voice=SimpleNamespace(channel=SimpleNamespace(voice_states={uid: object() for uid in user_ids}))
            )
        )

    async def _send_rpc_data(self, user_ids, stats):
        self.journal.append(("rpc", self.label, list(user_ids), dict(stats)))


class ExternalGuard:
    """Qualquer uso é uma requisição externa proibida (Spotify/Deezer/YouTube) — fica registrada."""

    def __init__(self, sink, label):
        self._sink, self._label = sink, label

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self._sink.append((self._label, name))
            raise AssertionError(f"requisição externa proibida: {self._label}.{name}")

        return call


class AccountBot:
    def __init__(self, world, bot_id=7000, name="Bot Um"):
        self.world, self.ready = world, True
        self.application_id = bot_id
        self.user = SimpleNamespace(
            id=bot_id,
            name=name,
            mention=f"<@{bot_id}>",
            display_avatar=SimpleNamespace(
                url=f"https://cdn.discordapp.com/avatars/{bot_id}/x.png",
                replace=lambda **kw: SimpleNamespace(url=f"https://cdn.discordapp.com/avatars/{bot_id}/x.png"),
            ),
        )
        self.config = world.config
        self.music = SimpleNamespace(players={})
        self.pool = None
        self.spotify = ExternalGuard(world.external_calls, "spotify")
        self.deezer = ExternalGuard(world.external_calls, "deezer")

    def is_ready(self):
        return self.ready

    def get_guild(self, guild_id):
        return None

    async def is_owner(self, user):
        return user.id in self.world.owner_ids

    async def get_global_data(self, id_, *, db_name):
        return await self.world.db.get("global", id_, db_name)

    async def update_global_data(self, id_, data, *, db_name):
        await self.world.db.update("global", id_, data, db_name)

    async def get_data(self, id_, *, db_name):
        raise AssertionError("a conta do usuário só usa dados globais de usuários")


class AccountPool:
    def __init__(self, bots, config):
        self.bots, self.config, self.database = list(bots), config, None
        self.user_prefix_cache, self.guild_prefix_cache = {}, {}
        self.default_skin = self.default_static_skin = "default"

    def get_all_bots(self):
        return list(self.bots)

    def api_calls(self):
        return 0


class AccountWorld:
    """Dois usuários com dados distintos (1001 = sessão dos testes; 2002 = outro) e um bot pronto."""

    def __init__(self):
        self.journal = []
        self.db = UserFakeDB(self.journal)
        self.config = dict(FAV_CONFIG)
        self.owner_ids = set()
        self.external_calls = []
        self.bot = AccountBot(self)
        self.pool = AccountPool([self.bot], self.config)
        self.bot.pool = self.pool
        self.raw = [raw_guild(100, "Alfa", permissions="8")]
        self.db.seed("global", UID, self.user_doc(prefix="!", favs={"Rock": "https://example.com/rock"}))
        self.db.seed("global", OTHER_UID, self.user_doc(prefix="?", favs={"Jazz": "https://example.com/jazz"}))

    @staticmethod
    def user_doc(prefix="", favs=None, integrations=None, token="", lastfm=None):
        from empiremuse.utils.db import DBModel, global_db_models

        doc = copy.deepcopy(global_db_models[DBModel.users])
        doc.update(
            {
                "custom_prefix": prefix,
                "fav_links": dict(favs or {}),
                "token": token,
                "integration_links": dict(
                    integrations
                    if integrations is not None
                    else {"Meu canal": {"url": "https://example.com/canal", "avatar": "https://example.com/a.png"}}
                ),
                "last_tracks": [{"title": "faixa antiga"}],
                "extra_flag": "intacto",
            }
        )
        doc["lastfm"].update(lastfm or {"username": "", "sessionkey": "", "scrobble": False})
        return doc

    def doc(self, uid=UID):
        return self.db.doc("global", uid)

    def set_doc(self, uid=UID, **fields):
        """Outro escritor (comando no Discord): altera o documento gravado sem passar pelo painel."""
        doc = self.db.docs[("global", uid)]
        doc.update(fields)
        self.db.baseline[("global", uid)] = copy.deepcopy(doc)

    def link_lastfm(self, uid=UID, username="usuario_lfm", scrobble=True):
        doc = self.db.docs[("global", uid)]
        doc["lastfm"].update({"username": username, "sessionkey": SESSIONKEY, "scrobble": scrobble})
        self.db.baseline[("global", uid)] = copy.deepcopy(doc)


class AccountBase(SettingsBase):
    """Painel montado com o AccountWorld; `logged_in()` entra como o usuário 1001."""

    def get_app(self):
        panel = require("empiremuse.panel")
        settings = require("empiremuse.panel.settings").load_panel_settings(self.config_factory())
        self.settings, self.world = settings, AccountWorld()
        self.pool, self.db = self.world.pool, self.world.db
        self.events = self.world.journal
        self.fake = make_fake_oauth(settings, guilds=self.world.raw)
        app = panel.build_panel_app(self.pool, settings, oauth_client=self.fake, clock=self.clock)
        self.panel_app = app
        return panel.make_router(tornado.web.Application([]), app)
