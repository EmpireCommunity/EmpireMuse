# -*- coding: utf-8 -*-
"""Configuração única por servidor (item 11, P1): o documento ``servers`` do banco global, a trava, a migração e a fachada antiga.

``pool.servers`` guarda UM documento por servidor para o pool inteiro (``rules/servers.py`` tem o modelo e as decisões puras).
Cada operação roda sob a trava do servidor: lê, planeja e grava o documento inteiro; só depois da gravação a cópia em memória é
trocada. No MongoDB a gravação é compare-and-set pelo ``rev`` (conflito: relê e tenta mais uma vez). A leitura no formato antigo
(``BotCore.get_data``) é montada deste documento; a escrita no formato antigo só aceita os campos por bot (canal, mensagem e
favoritos) e levanta se o escritor mexeu em ``settings`` sem passar por aqui.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import TYPE_CHECKING, Optional

from cachetools import TTLCache

from empiremuse.rules.limits import MAX_DJ_ROLES
from empiremuse.rules.playlists import move_leading_icons, plan_migration, to_doc
from empiremuse.rules.servers import (
    SECTION_PLAYLISTS,
    SECTION_REQUEST,
    LEGACY_LANGUAGE,
    SECTION_SETTINGS,
    Allowed,
    SettingError,
    changed_request,
    changed_settings,
    MAIN_IDENTIFIERS,
    is_main_identifier,
    migrate_request,
    migrate_settings,
    request_from_legacy,
    new_doc,
    normalize_role_ids,
    settings_from_legacy,
    to_legacy,
    validate_changes,
)
from empiremuse.utils.db import DBModel
from empiremuse.utils.music.errors import GenericError

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore, BotPool

logger = logging.getLogger(__name__)

SERVERS_DB = "global"
_ABSENT = object()  # campo que o documento ainda não tem (ex.: ``language`` antes da migração)
CACHE_SIZE = 2000
CACHE_TTL = 300
CONTROLLER_KEYS = (
    "channel",
    "message_id",
)  # os únicos campos que o formato antigo ainda grava, por bot (os links são playlists do servidor)

Plan = Callable[[Optional[dict]], Awaitable[Optional[dict]]]


class ServerConfigUnavailable(GenericError):
    def __init__(self) -> None:
        super().__init__("A configuração do servidor está indisponível no momento. Tente novamente em instantes.")


class ServerConfigConflict(GenericError):
    def __init__(self) -> None:
        super().__init__("A configuração do servidor mudou ao mesmo tempo. Tente novamente.")


class UnknownGuild(GenericError):
    def __init__(self) -> None:
        super().__init__("Servidor não encontrado.")


class StaleSettingsWriter(RuntimeError):
    """``update_data`` recebeu ``settings`` alterado: o escritor deve usar ``pool.servers`` (erro de programação)."""


class GuildDoc(dict):
    """O documento no formato antigo devolvido por ``get_data``: lembra o ``settings`` que leu para saber se o escritor mexeu nele."""

    settings_seen: dict
    request_seen: dict


def as_guild_id(value: object) -> Optional[int]:
    """O id de servidor como inteiro positivo, ou ``None`` (DM, texto, bool)."""
    if isinstance(value, bool):
        return None
    try:
        gid = int(value)  # type: ignore[call-overload]
    except TypeError, ValueError:
        return None
    return gid if gid > 0 else None


class ServerStore:
    def __init__(self, pool: BotPool) -> None:
        self.pool = pool
        self._docs: TTLCache = TTLCache(maxsize=CACHE_SIZE, ttl=CACHE_TTL)
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        from empiremuse.utils.server_playlists import ServerPlaylists  # aqui para não criar ciclo de importação

        self.playlists = ServerPlaylists(self)
        from empiremuse.utils.server_request import ServerRequest

        self.request = ServerRequest(self)

    # ------------------------------------------------------------------ o pool

    def main_bot(self) -> Optional[BotCore]:
        """O principal: o bot do token principal (nunca o primeiro por ordem); ``None`` se não logou."""
        candidates = [
            bot
            for bot in self.pool.get_all_bots()
            if bot.user is not None
            and is_main_identifier(bot.identifier)
            and getattr(bot, "exclusive_guild_id", None) is None
        ]
        # a ordem de get_all_bots() vem de um conjunto: com TOKEN e TOKEN_1 juntos, vale sempre o TOKEN
        return min(candidates, key=lambda bot: MAIN_IDENTIFIERS.index(bot.identifier), default=None)

    def knows(self, gid: int) -> bool:
        return any(bot.get_guild(gid) is not None for bot in self.pool.get_all_bots())

    def _loaded_guild(self, gid: int):
        """O servidor como algum bot do pool o vê COM os dados carregados (``None`` se nenhum o vê ou se está só o esboço do READY)."""
        return next(
            (
                g
                for b in self.pool.get_all_bots()
                if (g := b.get_guild(gid)) is not None and not getattr(g, "unavailable", False)
            ),
            None,
        )

    def _languages(self) -> frozenset[str]:
        """Os idiomas ligados no pool (``pool.i18n.enabled``); o ``I18n`` único do processo quando o pool não o traz."""
        from empiremuse.i18n import get_i18n

        i18n = getattr(self.pool, "i18n", None) or get_i18n()
        return i18n.enabled

    def _allowed(self, gid: int, settings: Optional[dict] = None) -> Allowed:
        """Skins carregadas e cargos do servidor; a skin já gravada continua valendo (como no painel)."""
        bots = self.pool.get_all_bots()
        bot = self.main_bot() or next(iter(bots), None)
        saved = settings or {}
        skins = frozenset(getattr(bot, "player_skins", ()) or ()) | {saved.get("skin")} - {None}
        static = frozenset(getattr(bot, "player_static_skins", ()) or ()) | {saved.get("static_skin")} - {None}
        guild = self._loaded_guild(gid)
        roles = frozenset(str(role.id) for role in guild.roles) if guild is not None else frozenset()
        return Allowed(skins, static, roles, self._languages())

    # ------------------------------------------------------------------ banco

    @property
    def _database(self):
        return self.pool.database

    def _lock(self, gid: str) -> asyncio.Lock:
        lock = self._locks.get(gid)
        if lock is None:
            lock = self._locks[gid] = asyncio.Lock()
        return lock

    async def _load(self, gid: str, *, fresh: bool = False) -> Optional[dict]:
        if not fresh and (cached := self._docs.get(gid)) is not None:
            return cached
        try:
            doc = await self._database.find_doc(gid, db_name=DBModel.servers, collection=SERVERS_DB)
        except Exception as exc:  # noqa: BLE001 - sem o documento nada se decide: erro genérico, nunca o padrão
            logger.warning("Documento do servidor [%s] não lido (%s)", gid, type(exc).__name__)
            raise ServerConfigUnavailable from exc
        if doc is not None:
            self._docs[gid] = doc
        return doc

    async def _transact(self, gid: str, plan: Plan) -> dict:
        """Lê, planeja e grava sob a trava do servidor; a cópia em memória só é trocada depois da gravação."""
        async with self._lock(gid):
            for attempt in (0, 1):
                doc = await self._load(gid, fresh=attempt > 0)
                new = await plan(doc)
                if new is None:
                    assert doc is not None
                    return doc
                new["rev"] = (doc["rev"] if doc is not None else 0) + 1
                try:
                    saved = await self._database.save_doc_cas(
                        gid,
                        new,
                        expected_rev=doc["rev"] if doc is not None else None,
                        db_name=DBModel.servers,
                        collection=SERVERS_DB,
                    )
                except Exception as exc:  # noqa: BLE001 - gravação que falha: cópia e migração ficam como estavam
                    # o servidor pode ter aplicado antes do erro: a próxima leitura vai ao banco
                    self._docs.pop(gid, None)
                    logger.warning("Documento do servidor [%s] não gravado (%s)", gid, type(exc).__name__)
                    raise ServerConfigUnavailable from exc
                if saved:
                    self._docs[gid] = new
                    return new
                self._docs.pop(gid, None)
            raise ServerConfigConflict

    # ------------------------------------------------------------------ migração

    async def _prepare(
        self, gid: str, doc: Optional[dict], sections: tuple[str, ...] = (SECTION_SETTINGS,)
    ) -> tuple[dict, bool]:
        """Cópia do documento com as ``sections`` pedidas migradas do principal (as que ainda não foram) e se mudou.

        Principal fora do ar ou erro de leitura: levanta e nada é gravado (a seção fica pendente; nunca o padrão do modelo).
        As seções migradas juntas saem na MESMA gravação.
        """
        new = deepcopy(doc) if doc is not None else new_doc()
        pending = [section for section in sections if not new["migrated"].get(section)]
        if not pending:
            return new, False
        main = self.main_bot()
        if main is None:
            raise ServerConfigUnavailable
        if SECTION_SETTINGS in pending and self._loaded_guild(int(gid)) is None:
            # esboço do READY (sem cargos): migrar agora descartaria os DJs de cargos "que não existem" e marcaria como migrado (V-156-1)
            raise ServerConfigUnavailable
        try:
            legacy = await self._database.find_doc(gid, db_name=DBModel.guilds, collection=str(main.user.id))
        except Exception as exc:  # noqa: BLE001 - erro de leitura: nada é gravado, a seção fica pendente
            logger.warning("Documento antigo do principal no servidor [%s] não lido (%s)", gid, type(exc).__name__)
            raise ServerConfigUnavailable from exc
        if SECTION_SETTINGS in pending:
            if (
                self._loaded_guild(int(gid)) is None
            ):  # o servidor pode ter voltado a ficar indisponível durante a leitura
                raise ServerConfigUnavailable
            new["settings"] = migrate_settings(legacy, self._allowed(int(gid)))
            new["migrated"][SECTION_SETTINGS] = True
            logger.info(
                "Servidor [%s] migrado: configuração antiga do principal %s", gid, "copiada" if legacy else "ausente"
            )
        if SECTION_REQUEST in pending:
            new["request"] = migrate_request(legacy)
            new["migrated"][SECTION_REQUEST] = True
            logger.info(
                "Servidor [%s] migrado: canal de pedidos do principal %s",
                gid,
                "copiado" if new["request"]["channel"] else "ausente",
            )
        if SECTION_PLAYLISTS in pending:
            plan = plan_migration(((legacy or {}).get("player_controller") or {}).get("fav_links"))
            new["playlists"], _ = move_leading_icons([to_doc(p) for p in plan.items])  # o emoji do início já vira ícone
            new["migrated"][SECTION_PLAYLISTS] = True
            logger.info(
                "Servidor [%s] migrado: %d playlists copiadas, %d repetidas e %d inválidas puladas",
                gid,
                len(plan.items),
                plan.skipped,
                plan.invalid,
            )
        return new, True

    # ------------------------------------------------------------------ API do store

    async def _ensure(self, gid: int, sections: tuple[str, ...]) -> dict:
        """Cópia do documento único com as ``sections`` migradas (na primeira vez de cada uma, sob a trava)."""
        key = str(as_guild_id(gid) or "")
        if not key:
            raise UnknownGuild
        cached = self._docs.get(key)
        if cached is not None and all(cached["migrated"].get(section) for section in sections):
            return deepcopy(cached)
        if not self.knows(int(key)):  # S-V11: não cria documento de servidor fora do pool; um que já existe só se lê
            doc = await self._load(key)
            if doc is None:
                raise UnknownGuild
            return deepcopy(doc)

        async def plan(doc: Optional[dict]) -> Optional[dict]:
            new, changed = await self._prepare(key, doc, sections)
            return new if changed else None

        return deepcopy(await self._transact(key, plan))

    async def get(self, gid: int) -> dict:
        """Cópia do documento único do servidor (migra ``settings`` na primeira vez)."""
        return await self._ensure(gid, (SECTION_SETTINGS,))

    async def get_sections(self, gid: int, *sections: str) -> dict:
        """Cópia do documento único com as seções pedidas já migradas (ex.: ``playlists``)."""
        return await self._ensure(gid, sections)

    async def mutate_doc(
        self, gid: int, sections: tuple[str, ...], change: Callable[[dict], bool], expected_rev: Optional[int] = None
    ) -> dict:
        """Roda ``change`` (que altera a cópia do documento e diz se mudou) sob a trava do servidor e grava só se mudou.

        ``expected_rev``: se o ``rev`` do documento já não é esse, ``ServerConfigConflict`` (conferido dentro da trava).
        """
        key = str(as_guild_id(gid) or "")
        if not key or not self.knows(int(key)):
            raise UnknownGuild

        async def plan(doc: Optional[dict]) -> Optional[dict]:
            if expected_rev is not None and (doc["rev"] if doc is not None else 0) != expected_rev:
                raise ServerConfigConflict
            new, changed = await self._prepare(key, doc, sections)
            if change(new):
                changed = True
            return new if changed else None

        return deepcopy(await self._transact(key, plan))

    async def stored_ids(self) -> list[str]:
        """Os ids dos documentos únicos que existem no banco (para as migrações de boot)."""
        documents = self._database.iter_data(db_name=DBModel.servers, collection=SERVERS_DB)
        return [str(doc["_id"]) async for doc in documents if doc.get("_id") is not None]

    async def mutate_stored(self, gid: str, change: Callable[[dict], bool]) -> None:
        """Como ``mutate_doc``, para um documento que já está no banco: sem exigir o servidor carregado nem migrar seções.

        Mesma trava por servidor, ``rev`` +1 e compare-and-set; grava só se ``change`` (que altera a cópia) disser que mudou.
        """

        async def plan(doc: Optional[dict]) -> Optional[dict]:
            if doc is None:
                return None
            new = deepcopy(doc)
            return new if change(new) else None

        await self._transact(gid, plan)

    async def migrate_language(self) -> tuple[int, int]:
        """Uma vez por subida, DEPOIS de ``I18n.load`` e ANTES dos bots: servidor que já existia e ainda não tem ``settings.language`` ganha ``pt-BR``.

        Mexe só nesse campo, sob a trava e com ``rev`` +1 (``mutate_stored``). Documento com a chave (inclusive ``None``) fica intacto, então
        rodar de novo não muda nada; um servidor que falha fica para a próxima subida. O log leva só contagens. Devolve ``(alterados, falhas)``.
        """
        changed = failed = 0
        for gid in await self.stored_ids():
            wrote = False

            def apply(doc: dict) -> bool:
                nonlocal wrote
                wrote = False
                settings = doc.get(SECTION_SETTINGS)
                if not isinstance(settings, dict) or "language" in settings:
                    return False
                settings["language"] = LEGACY_LANGUAGE
                wrote = True
                return True

            try:
                await self.mutate_stored(gid, apply)
            except Exception:  # noqa: BLE001 - o servidor fica para a próxima subida; o motivo já foi logado pelo store
                failed += 1
                continue
            changed += wrote
        if changed or failed:
            logger.info("Idioma dos servidores existentes: %d migrados para pt-BR (%d falharam)", changed, failed)
        return changed, failed

    async def settings(self, gid: int) -> dict:
        return (await self.get(gid))[SECTION_SETTINGS]

    async def patch(self, gid: int, section: str, changes: dict) -> dict:
        """Grava só os campos de ``changes`` (validados) sob a trava; devolve a seção nova."""
        if section != SECTION_SETTINGS:
            raise ValueError(f"seção desconhecida: {section}")
        return (await self._mutate_settings(gid, lambda settings, allowed: validate_changes(changes, allowed)))[section]

    async def add_djrole(self, gid: int, role_id: int | str) -> list[str]:
        """Põe o cargo na lista de DJ dentro da trava (já na lista = nada muda; cargo inexistente ou lista cheia levanta)."""
        (role,) = normalize_role_ids([role_id])

        def change(settings: dict, allowed: Allowed) -> dict:
            if role not in allowed.role_ids:
                raise SettingError("cargo inexistente neste servidor")
            if role in settings["djroles"]:
                return {}
            if len(settings["djroles"]) >= MAX_DJ_ROLES:
                raise SettingError(f"djroles passa de {MAX_DJ_ROLES} cargos")
            return {"djroles": [*settings["djroles"], role]}

        return (await self._mutate_settings(gid, change))[SECTION_SETTINGS]["djroles"]

    async def remove_djrole(self, gid: int, role_id: int | str) -> list[str]:
        """Tira o cargo da lista de DJ dentro da trava (sem ele na lista, nada muda)."""
        (role,) = normalize_role_ids([role_id])

        def change(settings: dict, allowed: Allowed) -> dict:
            return {"djroles": [r for r in settings["djroles"] if r != role]} if role in settings["djroles"] else {}

        return (await self._mutate_settings(gid, change))[SECTION_SETTINGS]["djroles"]

    async def _mutate_settings(self, gid: int, change: Callable[[dict, Allowed], dict]) -> dict:
        key = str(as_guild_id(gid) or "")
        if not key or not self.knows(int(key)):
            raise UnknownGuild

        async def plan(doc: Optional[dict]) -> Optional[dict]:
            new, changed = await self._prepare(key, doc)
            fields = change(new[SECTION_SETTINGS], self._allowed(int(key), new[SECTION_SETTINGS]))
            if any(new[SECTION_SETTINGS].get(name, _ABSENT) != value for name, value in fields.items()):
                new[SECTION_SETTINGS].update(fields)
                changed = True
            return new if changed else None

        return deepcopy(await self._transact(key, plan))

    # ------------------------------------------------------------------ fachada no formato antigo

    async def legacy_doc(self, bot: BotCore, gid: object) -> GuildDoc:
        """O documento do servidor no formato antigo (cópia profunda): campos por bot do próprio bot, ``settings`` do documento único."""
        base = await self._database.get_data(gid, db_name=DBModel.guilds, collection=str(bot.user.id))
        gid_int = as_guild_id(gid)
        try:
            if gid_int is None:
                raise UnknownGuild
            unique = await self._ensure(gid_int, (SECTION_SETTINGS, SECTION_REQUEST))
            settings = unique[SECTION_SETTINGS]
            request = unique.get(SECTION_REQUEST) or request_from_legacy(
                base
            )  # documento antigo sem a seção: vale o do bot
        except UnknownGuild:  # DM ou servidor fora do pool: vale o que o próprio bot tinha, sem criar documento único
            settings, request = settings_from_legacy(base), request_from_legacy(base)
        doc = GuildDoc(to_legacy(base, settings, request))
        doc.settings_seen = deepcopy(settings)
        doc.request_seen = {"channel": request.get("channel"), "message_id": request.get("message_id")}
        return doc

    async def save_legacy(self, bot: BotCore, gid: object, data: dict) -> dict:
        """Grava o canal e a mensagem do canal de pedidos NO DOCUMENTO ÚNICO, só o que o escritor mudou em relação ao que leu.

        Levanta se ``settings`` foi alterado (use ``pool.servers``). Servidor fora do pool (ou DM) grava no documento do bot, como antes.
        """
        reference = None
        seen = getattr(data, "settings_seen", None)
        seen_request = getattr(data, "request_seen", None)
        if seen is None or seen_request is None:  # documento montado à mão: compara com o que o servidor tem agora
            reference = await self.legacy_doc(bot, gid)
            seen, seen_request = reference.settings_seen, reference.request_seen
        if changed := changed_settings(data, seen):
            raise StaleSettingsWriter(
                f"update_data com settings alterado ({', '.join(sorted(changed))}): use pool.servers"
            )
        changes = changed_request(data, seen_request)
        gid_int = as_guild_id(gid)
        if changes:
            if gid_int is not None and self.knows(gid_int):

                def apply(doc: dict) -> bool:
                    before = dict(doc["request"])
                    doc["request"].update(changes)
                    if changes.get("channel") is None and "channel" in changes:
                        doc["request"]["message_id"] = None  # sem canal não há mensagem
                    return doc["request"] != before

                await self.mutate_doc(gid_int, (SECTION_REQUEST,), apply)
            else:
                await self._save_bot_document(bot, gid, changes)
        return data

    async def _save_bot_document(self, bot: BotCore, gid: object, changes: dict) -> None:
        """Servidor fora do pool: canal e mensagem no documento do próprio bot (o comportamento antigo, sem o documento único)."""
        collection = str(bot.user.id)
        async with self._lock(f"{collection}:{gid}"):
            stored = deepcopy(await self._database.get_data(gid, db_name=DBModel.guilds, collection=collection))
            stored.setdefault("player_controller", {}).update(changes)
            await self._database.update_data(gid, stored, db_name=DBModel.guilds, collection=collection)
