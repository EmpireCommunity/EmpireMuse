"""Mundo das rotas de dados (B3): os dublês do painel genérico (servidores e conta) atrás da API de teste.

Os serviços, as regras e a autorização são os reais; só o banco, o Discord e o diretório de rádio são dublês. O usuário da
sessão é o ``1001`` (``UID``); ``2002`` é outro usuário, que não gerencia nenhum servidor do mundo.
"""

import json
from types import SimpleNamespace

from empiremuse.utils.radio.favorites import AddResult, FavoriteNotFound
from empiremuse.utils.radio.premium import RadioUnavailable
from tests.api.support import ApiTestCase
from tests.panel.helpers_account import FAV_CONFIG, OTHER_UID, RPC_TOKEN_OLD, SESSIONKEY, AccountWorld, UserFakeDB
from tests.panel.helpers_settings import UID, SettingsWorld

USER = str(UID)
OTHER = str(OTHER_UID)
OLD_VERSION = "0" * 16
STATION = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
NODE = "br-sp"
UNKNOWN_STATION = "cccccccc-cccc-cccc-cccc-cccccccccccc"  # o diretório não a conhece


class FakeUserFavorites:
    """As rádios favoritas pessoais (J3) em memória; ``remove_*`` é idempotente, como o armazenamento real."""

    def __init__(self):
        self.station_ids = {UID: [STATION], OTHER_UID: ["bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"]}
        self.filter_ids = {UID: [NODE], OTHER_UID: []}
        self.broken = False

    def _check(self):
        if self.broken:
            raise RadioUnavailable

    async def stations(self, user_id):
        self._check()
        return [SimpleNamespace(uuid=uuid, name=f"Rádio {uuid[:4]}") for uuid in self.station_ids[user_id]]

    async def filters(self, user_id):
        return list(self.filter_ids[user_id])

    async def add_station(self, user_id, uuid):
        self._check()
        if uuid == UNKNOWN_STATION:
            raise FavoriteNotFound
        if len(self.station_ids[user_id]) >= 24:
            return AddResult.FULL
        if uuid in self.station_ids[user_id]:
            return AddResult.ALREADY
        self.station_ids[user_id].append(uuid)
        return AddResult.ADDED

    async def add_filter(self, user_id, node):
        self._check()
        if node == "desconhecido":
            raise FavoriteNotFound
        if node in self.filter_ids[user_id]:
            return AddResult.ALREADY
        self.filter_ids[user_id].append(node)
        return AddResult.ADDED

    async def remove_station(self, user_id, uuid):
        self._check()
        present = uuid in self.station_ids[user_id]
        self.station_ids[user_id] = [item for item in self.station_ids[user_id] if item != uuid]
        return present

    async def remove_filter(self, user_id, node):
        self._check()
        present = node in self.filter_ids[user_id]
        self.filter_ids[user_id] = [item for item in self.filter_ids[user_id] if item != node]
        return present


class DataWorld(SettingsWorld):
    """Servidores do ``SettingsWorld`` (100 Alfa, 800 Ômega, 200 Beta, 600 Rebaixado) e a conta dos dois usuários."""

    def __init__(self):
        super().__init__()
        self.users = UserFakeDB(self.events)
        self.users.seed("global", UID, AccountWorld.user_doc(prefix="!", favs={"Rock": "https://example.com/rock"}))
        self.users.seed(
            "global", OTHER_UID, AccountWorld.user_doc(prefix="?", favs={"Jazz": "https://example.com/jazz"})
        )
        self.config.update(FAV_CONFIG)
        self.favorites = FakeUserFavorites()
        self.pool.radio = SimpleNamespace(user_favorites=self.favorites, locations=None)
        for bot in self.pool.bots:
            self._route_user_documents(bot)

    def _route_user_documents(self, bot):
        guild_docs = (bot.get_global_data, bot.update_global_data)

        async def get_global_data(id_, *, db_name):
            if db_name == "users":
                return await self.users.get("global", id_, db_name)
            return await guild_docs[0](id_, db_name=db_name)

        async def update_global_data(id_, data, *, db_name):
            if db_name == "users":
                return await self.users.update("global", id_, data, db_name)
            return await guild_docs[1](id_, data, db_name=db_name)

        async def is_owner(user):
            return False

        bot.get_global_data, bot.update_global_data, bot.is_owner = get_global_data, update_global_data, is_owner

    def link_lastfm(self, uid=UID):
        doc = self.users.docs[("global", uid)]
        doc["lastfm"].update({"username": "usuario_lfm", "sessionkey": SESSIONKEY, "scrobble": False})
        self.users.baseline[("global", uid)] = json.loads(json.dumps(doc))

    def set_rpc_token(self, uid=UID):
        self.users.docs[("global", uid)]["token"] = RPC_TOKEN_OLD
        self.users.baseline[("global", uid)] = json.loads(json.dumps(self.users.docs[("global", uid)]))

    def user_doc(self, uid=UID):
        return self.users.doc("global", uid)


class DataApiCase(ApiTestCase):
    """A API de teste sobre o ``DataWorld``; ``self.token`` é a sessão do usuário 1001."""

    def make_pool(self):
        self.world = DataWorld()
        return self.world.pool

    def get_app(self):
        app = super().get_app()
        self.oauth.guilds = self.world.raw  # os servidores que o Discord devolve no login
        return app

    def setUp(self):
        super().setUp()
        self.io_loop.run_sync(self.world.premigrate)  # o documento único já migrado: o que se grava daqui é do teste
        self.token = self.new_session(USER)

    def call(self, method, path, body=None, *, session=None, key=None, **extra):
        """Chamada autenticada como o usuário 1001 (ou outro token); ``key`` é o ``Idempotency-Key``."""
        headers = dict(extra.pop("headers", {}))
        if key is not None:
            headers["Idempotency-Key"] = key
        return self.api(method, path, session=session or self.token, body=body, headers=headers, **extra)

    def version_of(self, path, form):
        """A versão atual de um formulário (a leitura que o painel faria antes de escrever)."""
        from tests.api.support import body_of

        data = body_of(self.call("GET", path))
        forms = data["forms"] if "forms" in data else {data["key"]: data}
        return forms[form]["version"]

    @property
    def db_writes(self):
        return list(self.world.db.writes) + list(self.world.users.writes)
