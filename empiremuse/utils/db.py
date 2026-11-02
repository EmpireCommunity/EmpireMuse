# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import collections.abc
import json
import logging
import os
import shutil
from collections.abc import Iterable
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional, Union

import disnake
from cachetools import TTLCache
from disnake.ext import commands
from pymongo import AsyncMongoClient
from pymongo.errors import DuplicateKeyError
from tinydb_serialization import Serializer, SerializationMiddleware
from tinymongo import TinyMongoClient
from tinymongo.serializers import DateTimeSerializer

from empiremuse import paths

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore

logger = logging.getLogger(__name__)

# Só estes valores de MONGO_SSL_FIX (sem diferenciar maiúsculas) desligam a validação do certificado TLS (CWE-295).
MONGO_SSL_FIX_TRUE_VALUES = frozenset({"true", "1", "yes", "on"})


def mongo_ssl_fix_enabled() -> bool:
    """Parse booleano estrito: ausente, vazio, ``false``, ``0`` ou qualquer outro valor mantém a validação ligada."""
    return os.environ.get("MONGO_SSL_FIX", "").lower() in MONGO_SSL_FIX_TRUE_VALUES


class DBModel:
    guilds = "guilds"
    users = "users"
    default = "default"
    usage_events = "usage_events"
    banned_guilds = "banned_guilds"
    servers = "servers"


db_models = {
    DBModel.guilds: {
        "ver": 1.11,
        "player_controller": {
            "channel": None,
            "message_id": None,
            "skin": None,
            "static_skin": None,
            "fav_links": {},
        },
        "autoplay": False,
        "check_other_bots_in_vc": False,
        "enable_restrict_mode": False,
        "default_player_volume": 100,
        "djroles": [],
    },
}

scrobble_model = {DBModel.users: {"ver": 1.0, "tracks": []}}

global_db_models = {
    DBModel.users: {
        "ver": 1.6,
        "fav_links": {},
        "integration_links": {},
        "token": "",
        "custom_prefix": "",
        "last_tracks": [],
        "lastfm": {
            "username": "",
            "sessionkey": "",
            "scrobble": False,
        },
    },
    DBModel.guilds: {
        "ver": 1.5,
        "prefix": "",
        "global_skin": False,
        "player_skin": None,
        "player_skin_static": None,
        "voice_channel_status": "",
        "voice_status_permanent": None,  # None = nunca escolhido = ligado (2.1.2); um valor por servidor para todo o pool
        "custom_skins": {},
        "custom_skins_static": {},
        "listen_along_invites": {},
    },
    DBModel.default: {"ver": 1.0, "extra_tokens": {}},
}


async def get_prefix(bot: BotCore, message: disnake.Message):
    if str(message.content).startswith((f"<@!{bot.user.id}> ", f"<@{bot.user.id}> ")):
        return commands.when_mentioned(bot, message)

    try:
        user_prefix = bot.pool.user_prefix_cache[message.author.id]
    except KeyError:
        user_data = await bot.get_global_data(message.author.id, db_name=DBModel.users)
        bot.pool.user_prefix_cache[message.author.id] = user_data["custom_prefix"]
        user_prefix = user_data["custom_prefix"]

    if user_prefix and message.content.startswith(user_prefix):
        return user_prefix

    if not message.guild:
        return commands.when_mentioned_or(bot.default_prefix)

    try:
        guild_prefix = bot.pool.guild_prefix_cache[message.guild.id]
    except KeyError:
        data = await bot.get_global_data(message.guild.id, db_name=DBModel.guilds)
        guild_prefix = data.get("prefix")

    if not guild_prefix:
        guild_prefix = bot.config.get("DEFAULT_PREFIX") or "!!"

    return guild_prefix


class BaseDB:
    def __init__(self, cache_maxsize: int = 1000, cache_ttl=300):
        self.cache = TTLCache(maxsize=cache_maxsize, ttl=cache_ttl)

    def get_default(self, collection: str, db_name: Union[DBModel.guilds, DBModel.users]):
        if collection == "global":
            return deepcopy(global_db_models[db_name])
        return deepcopy(db_models[db_name])


class DatetimeSerializer(Serializer):
    OBJ_CLASS = datetime

    def __init__(self, format="%Y-%m-%dT%H:%M:%S", *args, **kwargs):
        super(DatetimeSerializer, self).__init__(*args, **kwargs)
        self._format = format

    def encode(self, obj):
        return obj.strftime(self._format)

    def decode(self, s):
        return datetime.strptime(s, self._format)


class CustomTinyMongoClient(TinyMongoClient):
    @property
    def _storage(self):
        serialization = SerializationMiddleware()
        serialization.register_serializer(DateTimeSerializer(), "TinyDate")
        return serialization


class LocalDatabase(BaseDB):
    def __init__(self, dir_=None, cache_maxsize=1000, cache_ttl=300):
        super().__init__(cache_maxsize=cache_maxsize, cache_ttl=cache_ttl)

        if dir_ is None or dir_ == "./local_database":
            dir_ = str(paths.RUNTIME_DIR / "local_database")

        self.dir = dir_

        if not os.path.isdir(dir_):
            os.makedirs(dir_)

        self._connect = CustomTinyMongoClient(dir_)

    async def get_data(
        self, id_: int, *, db_name: Union[DBModel.guilds, DBModel.users], collection: str, default_model: dict = None
    ):
        if not default_model:
            default_model = db_models

        id_ = str(id_)

        if (cached_result := self.cache.get(f"{collection}:{db_name}:{id_}")) is not None:
            return cached_result

        data = self._connect[collection][db_name].find_one({"_id": id_})

        if not data:
            data = deepcopy(default_model[db_name])
            data["_id"] = str(id_)
            self._connect[collection][db_name].insert_one(data)

        elif data["ver"] != default_model[db_name]["ver"]:
            data = update_values(deepcopy(default_model[db_name]), data)
            data["ver"] = default_model[db_name]["ver"]

            await self.update_data(id_, data, db_name=db_name, collection=collection)

        return data

    async def update_data(
        self,
        id_,
        data: dict,
        *,
        db_name: Union[DBModel.guilds, DBModel.users],
        collection: str,
        default_model: dict = None,
    ):
        id_ = str(id_)
        data["_id"] = id_

        # upsert como no MongoDatabase: documento novo também vai para o disco. Se a gravação falhar, o erro original
        # sobe e o cache não é atualizado (senão o dado "salvo" some quando o cache expirar).
        if not self._connect[collection][db_name].update_one({"_id": id_}, {"$set": data}, upsert=True).raw_result:
            self._connect[collection][db_name].insert_one(data)

        self.cache[f"{collection}:{db_name}:{id_}"] = data

        return data

    async def query_data(self, db_name: str, collection: str, filter: dict = None, limit: Optional[int] = None):
        """Os documentos que casam com ``filter``; ``limit`` (se informado) é o máximo devolvido. Sem ``limit`` vem tudo."""
        cursor = self._connect[collection][db_name].find(filter or {})
        return cursor if limit is None else cursor.limit(limit)

    async def iter_data(self, db_name: str, collection: str, filter: dict = None):
        """Os documentos, um por vez. O TinyMongo já guarda a coleção na memória; a fotografia da lista evita que quem grava
        enquanto percorre (como o ``fixfavs``) mexa no que está sendo percorrido."""
        for document in list(self._connect[collection][db_name].find(filter or {})):
            yield document

    # Coleção de eventos (registro de uso): sem o cache de documentos, em lote e fora do laço de eventos. O TinyMongo regrava o
    # arquivo inteiro a cada escrita e guarda todas as tabelas de um banco no mesmo arquivo; por isso os eventos ficam em um arquivo
    # só deles (``<banco>_events``), a escrita é em lote e roda em uma thread. Quem chama serializa o acesso.
    def _events(self, db_name: str, collection: str):
        return self._connect[f"{collection}_events"][db_name]

    async def append_events(self, docs: list[dict], *, db_name: str, collection: str, expire_after: timedelta) -> None:
        await asyncio.to_thread(self._events(db_name, collection).insert_many, docs)

    async def patch_event(self, id_: str, fields: dict, *, db_name: str, collection: str) -> None:
        await asyncio.to_thread(self._events(db_name, collection).update_one, {"_id": id_}, {"$set": fields})

    async def find_events(self, filter: dict, *, db_name: str, collection: str, limit: int) -> list[dict]:
        def read() -> list[dict]:
            rows = list(self._events(db_name, collection).find(filter))
            rows.sort(key=lambda doc: doc.get("ts", 0), reverse=True)
            return rows[:limit]

        return await asyncio.to_thread(read)

    async def purge_events(self, older_than_ms: int, *, db_name: str, collection: str) -> None:
        """Poda os eventos mais antigos que o corte (no MongoDB quem poda é o índice TTL)."""
        await asyncio.to_thread(self._events(db_name, collection).delete_many, {"ts": {"$lt": older_than_ms}})

    async def ensure_event_indexes(self, *, db_name: str, collection: str) -> None:
        return None

    async def find_doc(self, id_, *, db_name: str, collection: str) -> Optional[dict]:
        """O documento como está no banco: sem cache e sem criar o padrão (``None`` se não existe)."""
        doc = self._connect[collection][db_name].find_one({"_id": str(id_)})
        return deepcopy(doc) if doc else None

    async def save_doc_cas(self, id_, doc: dict, *, expected_rev: Optional[int], db_name: str, collection: str) -> bool:
        """Grava ``doc`` só se o ``rev`` guardado ainda é ``expected_rev`` (``None`` = o documento ainda não existe)."""
        id_ = str(id_)
        table = self._connect[collection][db_name]
        stored = table.find_one({"_id": id_})
        if expected_rev is None:
            if stored:
                return False
            table.insert_one({**doc, "_id": id_})
            return True
        if not stored or stored.get("rev") != expected_rev:
            return False
        table.update_one({"_id": id_}, {"$set": {k: v for k, v in doc.items() if k != "_id"}})
        return True

    async def unset_fields_cas(
        self, id_, fields: list[str], *, expected_rev: int, db_name: str, collection: str
    ) -> bool:
        """Apaga ``fields`` (caminhos com ponto) só se o ``rev`` guardado ainda é ``expected_rev``; o ``rev`` anda um. Um ``$set`` não apaga campo."""
        id_ = str(id_)
        table = self._connect[collection][db_name]
        stored = table.find_one({"_id": id_})
        if not stored or stored.get("rev") != expected_rev:
            return False
        update: dict = {"$set": {"rev": expected_rev + 1}}
        if fields:  # um ``$unset`` vazio é erro no Mongo
            update["$unset"] = {field: "" for field in fields}
        table.update_one({"_id": id_}, update)
        return True

    async def wipe(self, databases: Iterable[str], keep: frozenset[tuple[str, str]]) -> dict[str, int]:
        """Apaga toda tabela dos bancos ``databases`` (e dos ``<banco>_events`` dos eventos), menos as de ``keep`` = ``(banco, tabela)``.

        Devolve ``{"banco.tabela": documentos apagados}``. Só mexe nos bancos pedidos; o cache de documentos é esvaziado.
        """
        dropped: dict[str, int] = {}
        for name in databases:
            for db_label in (name, f"{name}_events"):
                database = self._connect[db_label]
                for table in list(database.list_collection_names()):
                    if (db_label, table) in keep:
                        continue
                    dropped[f"{db_label}.{table}"] = len(list(database[table].find({})))
                    database.drop_collection(table)
        self.cache.clear()
        return dropped

    async def delete_data(self, id_, db_name: str, collection: str):
        try:
            self._connect[collection][db_name].delete_one({"_id": str(id_)})
        except TypeError:
            return

        try:
            self.cache.pop(f"{collection}:{db_name}:{id_}")
        except KeyError:
            pass


class MongoDatabase(BaseDB):
    def __init__(self, token: str, timeout=30, cache_maxsize=1000, cache_ttl=300):
        super().__init__(cache_maxsize=cache_maxsize, cache_ttl=cache_ttl)

        tls_options = {}

        if mongo_ssl_fix_enabled():
            # Sem a URI na mensagem: ela carrega a senha.
            logger.warning(
                "MONGO_SSL_FIX ativo: a validação do certificado TLS do MongoDB está DESLIGADA (só para depuração)."
            )
            tls_options = {"tls": True, "tlsAllowInvalidCertificates": True}

        self._connect = AsyncMongoClient(token.strip("<>"), connectTimeoutMS=timeout * 1000, **tls_options)

    async def push_data(self, data, *, db_name: Union[DBModel.guilds, DBModel.users], collection: str):
        await self._connect[collection][db_name].insert_one(data)

    async def update_from_json(self):
        dbs_dir = paths.RUNTIME_DIR / "local_dbs"
        backups_dir = dbs_dir / "backups"
        if not backups_dir.is_dir():
            backups_dir.mkdir(parents=True, exist_ok=True)

        if dbs_dir.is_dir():
            for f in os.listdir(dbs_dir):
                if not f.endswith(".json"):
                    continue

                with open(dbs_dir / f) as file:
                    data = json.load(file)

                for db_name, db_data in data.items():
                    if not db_data:
                        continue

                    for id_, d in db_data.items():
                        await self.update_data(id_=id_, data=d, db_name=db_name, collection=f[:-5])

                try:
                    shutil.move(str(dbs_dir / f), str(backups_dir / f))
                except Exception:
                    logger.exception("Falha em update_from_json")

    async def get_data(
        self, id_: int, *, db_name: Union[DBModel.guilds, DBModel.users], collection: str, default_model: dict = None
    ):
        if not default_model:
            default_model = db_models

        id_ = str(id_)

        if (cached_result := self.cache.get(f"{collection}:{db_name}:{id_}")) is not None:
            return cached_result

        data = await self._connect[collection][db_name].find_one({"_id": id_})

        if not data:
            return deepcopy(default_model[db_name])

        elif data["ver"] != default_model[db_name]["ver"]:
            data = update_values(deepcopy(default_model[db_name]), data)
            data["ver"] = default_model[db_name]["ver"]
            await self.update_data(id_, data, db_name=db_name, collection=collection)

        return data

    async def update_data(
        self,
        id_,
        data: dict,
        *,
        db_name: Union[DBModel.guilds, DBModel.users, str],
        collection: str,
        default_model: dict = None,
    ):
        try:
            del data["_id"]
        except KeyError:
            pass

        await self._connect[collection][db_name].update_one({"_id": str(id_)}, {"$set": data}, upsert=True)
        self.cache[f"{collection}:{db_name}:{id_}"] = (
            data  # só depois de gravar: gravação que falha não deixa dado "salvo" no cache
        )
        return data

    async def query_data(self, db_name: str, collection: str, filter: dict = None, limit: Optional[int] = None) -> list:
        """Os documentos que casam com ``filter`` numa lista; ``limit`` (se informado) é o máximo puxado do cursor."""
        cursor = self._connect[collection][db_name].find(filter or {})
        if limit is not None:
            cursor = cursor.limit(limit)
        return [d async for d in cursor]

    async def iter_data(self, db_name: str, collection: str, filter: dict = None):
        """O cursor do PyMongo Async entregue um documento por vez: nada da coleção é guardado na memória."""
        async for document in self._connect[collection][db_name].find(filter or {}):
            yield document

    # Coleção de eventos (registro de uso): sem o cache de documentos. A retenção é do índice TTL em ``expire_at``.
    async def append_events(self, docs: list[dict], *, db_name: str, collection: str, expire_after: timedelta) -> None:
        expire_at = datetime.now(timezone.utc) + expire_after
        await self._connect[collection][db_name].insert_many(
            [{**doc, "expire_at": expire_at} for doc in docs], ordered=False
        )

    async def patch_event(self, id_: str, fields: dict, *, db_name: str, collection: str) -> None:
        await self._connect[collection][db_name].update_one({"_id": id_}, {"$set": fields})

    async def find_events(self, filter: dict, *, db_name: str, collection: str, limit: int) -> list[dict]:
        cursor = self._connect[collection][db_name].find(filter).sort("ts", -1).limit(limit)
        return [d async for d in cursor]

    async def purge_events(self, older_than_ms: int, *, db_name: str, collection: str) -> None:
        return None

    async def ensure_event_indexes(self, *, db_name: str, collection: str) -> None:
        events = self._connect[collection][db_name]
        await events.create_index("expire_at", expireAfterSeconds=0)
        await events.create_index([("guild", 1), ("ts", -1)])

    async def find_doc(self, id_, *, db_name: str, collection: str) -> Optional[dict]:
        """O documento como está no banco: sem cache e sem criar o padrão (``None`` se não existe)."""
        return await self._connect[collection][db_name].find_one({"_id": str(id_)})

    async def save_doc_cas(self, id_, doc: dict, *, expected_rev: Optional[int], db_name: str, collection: str) -> bool:
        """Grava ``doc`` só se o ``rev`` guardado ainda é ``expected_rev`` (``None`` = o documento ainda não existe)."""
        id_ = str(id_)
        table = self._connect[collection][db_name]
        body = {k: v for k, v in doc.items() if k != "_id"}
        if expected_rev is None:
            try:
                await table.insert_one({**body, "_id": id_})
            except DuplicateKeyError:
                return False
            return True
        result = await table.update_one({"_id": id_, "rev": expected_rev}, {"$set": body})
        return result.matched_count == 1

    async def unset_fields_cas(
        self, id_, fields: list[str], *, expected_rev: int, db_name: str, collection: str
    ) -> bool:
        """Apaga ``fields`` (caminhos com ponto) só se o ``rev`` guardado ainda é ``expected_rev``; o ``rev`` anda um. Um ``$set`` não apaga campo."""
        update: dict = {"$set": {"rev": expected_rev + 1}}
        if fields:  # um ``$unset`` vazio é erro no Mongo
            update["$unset"] = {field: "" for field in fields}
        result = await self._connect[collection][db_name].update_one({"_id": str(id_), "rev": expected_rev}, update)
        return result.matched_count == 1

    async def wipe(self, databases: Iterable[str], keep: frozenset[tuple[str, str]]) -> dict[str, int]:
        """Apaga toda coleção dos bancos ``databases``, menos as de ``keep`` = ``(banco, coleção)``; devolve ``{"banco.coleção": documentos}``.

        Só os bancos pedidos (o cluster pode ter outros). O cache de documentos é esvaziado.
        """
        dropped: dict[str, int] = {}
        for name in databases:
            database = self._connect[name]
            for table in await database.list_collection_names():
                if (name, table) in keep:
                    continue
                dropped[f"{name}.{table}"] = await database[table].count_documents({})
                await database.drop_collection(table)
        self.cache.clear()
        return dropped

    async def delete_data(self, id_, db_name: str, collection: str):
        try:
            self.cache.pop(f"{collection}:{db_name}:{id_}")
        except KeyError:
            pass
        return await self._connect[collection][db_name].delete_one({"_id": str(id_)})


def update_values(d: dict, u: collections.abc.Mapping) -> dict:
    """Migra o documento salvo `u` para o modelo `d` (o modelo é alterado e devolvido; passe uma cópia).

    Só acrescenta e atualiza, nunca apaga: o valor salvo vence o padrão em qualquer tipo (inclusive listas), chaves
    novas do modelo ficam com o padrão e chaves removidas do modelo são preservadas. Nada do documento salvo é
    compartilhado com o resultado (cópia profunda).
    """
    for k, v in u.items():
        if isinstance(v, collections.abc.Mapping) and isinstance(d.get(k), collections.abc.Mapping):
            d[k] = update_values(d[k], v)
        else:
            d[k] = deepcopy(v)
    return d
