# -*- coding: utf-8 -*-
"""Playlists do servidor (item 17): a seção ``playlists`` do documento único, a mesma lista para todos os bots do pool.

``pool.servers.playlists`` lê e grava SÓ pelo store (``utils/servers``): trava por servidor, ``rev`` +1 a cada escrita e compare-and-set
no MongoDB. Validação e decisões são de ``rules/playlists``. Toda escrita que muda a lista atualiza o embed ocioso dos bots do servidor
em segundo plano (falha só vai para o log). Quem chama decide a permissão (``may_manage``) antes de gravar.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Optional

import disnake

from empiremuse.rules.errors import RuleError
from empiremuse.rules.jukebox_select import SELECT_CAPACITY
from empiremuse.rules.playlists import (
    ImportReport,
    SCOPE_JUKEBOX,
    SCOPES,
    ParsedFile,
    Playlist,
    add_item,
    check_playlist,
    edit_item,
    find_item,
    find_selected,
    from_doc,
    move_leading_icons,
    plan_import,
    remove_item,
    select_description,
    sorted_by_order,
    to_doc,
    to_export,
)
from empiremuse.rules.servers import SECTION_PLAYLISTS
from empiremuse.utils.db import DBModel

if TYPE_CHECKING:
    from empiremuse.utils.servers import ServerStore

logger = logging.getLogger(__name__)

SECTIONS = (SECTION_PLAYLISTS,)


def _items(doc: dict, scope: str | None = None) -> tuple[Playlist, ...]:
    """As playlists gravadas, relidas com as mesmas regras e por ``order`` (dado gravado ruim é ignorado, nunca levanta).

    ``scope`` filtra por escopo (``None`` = os dois). Cada escopo tem sua própria lista, nome, cota e ordem.
    """
    items = (item for raw in doc.get("playlists", []) if (item := from_doc(raw)) is not None)
    return sorted_by_order(item for item in items if scope is None or item.scope == scope)


def _check_scope(scope: str) -> str:
    if scope not in SCOPES:
        raise ValueError("escopo de playlist desconhecido")
    return scope


def _validated(item: Playlist, scope: str) -> Playlist | RuleError:
    """O item revalidado: nada chega ao banco sem passar pelas regras, venha de onde vier (``icon`` inválido vira ``None``). O escopo é o pedido, nunca o do item."""
    checked = check_playlist(item._asdict(), default_order=item.order)
    return checked if isinstance(checked, RuleError) else checked._replace(order=item.order, scope=scope)


def select_options(bot, playlists: tuple[Playlist, ...], limit: int = SELECT_CAPACITY) -> list[disnake.SelectOption]:
    """As primeiras ``limit`` playlists por ordem como opções de select (o Discord aceita 25): nome, descrição cortada e o emoji (o ícone da playlist; sem ele, o da plataforma).

    O ``value`` é o nome; quem trata a escolha o resolve de novo no store, nunca confia no valor recebido.
    """
    from empiremuse.utils.others import music_source_emoji_url  # tarde: utils.others importa muita coisa

    return [
        disnake.SelectOption(
            label=p.name,
            value=p.name,
            description=select_description(p.description) or None,
            emoji=p.icon or music_source_emoji_url(p.url, bot)[0],
        )
        for p in playlists[: max(0, min(limit, SELECT_CAPACITY))]
    ]


async def safe_list(bot, gid: int, scope: str = SCOPE_JUKEBOX) -> tuple[Playlist, ...]:
    """As playlists do servidor para EXIBIR (embed ocioso, select, menu): store indisponível ou servidor desconhecido = lista vazia."""
    from empiremuse.utils.servers import (  # tarde: servers cria o ServerPlaylists
        ServerConfigConflict,
        ServerConfigUnavailable,
        UnknownGuild,
    )

    try:
        return await bot.pool.servers.playlists.list(gid, scope=scope)
    except ServerConfigUnavailable, ServerConfigConflict, UnknownGuild:
        return ()


class ServerPlaylists:
    def __init__(self, store: ServerStore) -> None:
        self.store = store
        self._refresh_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ leitura

    async def snapshot(self, gid: int, scope: str = SCOPE_JUKEBOX) -> tuple[tuple[Playlist, ...], int]:
        """As playlists do ``scope`` por ``order`` e o ``rev`` do documento (um só para os dois escopos; o painel o leva no formulário e o devolve ao gravar)."""
        doc = await self.store.get_sections(gid, *SECTIONS)
        return _items(doc, _check_scope(scope)), int(doc["rev"])

    async def list(self, gid: int, scope: str = SCOPE_JUKEBOX) -> tuple[Playlist, ...]:
        return (await self.snapshot(gid, scope))[0]

    async def ver(self, gid: int) -> int:
        return (await self.snapshot(gid))[1]

    async def find(self, gid: int, name: str, scope: str = SCOPE_JUKEBOX) -> Playlist | None:
        """A playlist do ``scope`` escolhida pelo nome (``casefold``), ou ``None``; ver ``find_selected`` (valor de antes da migração). Nunca acha a do outro escopo."""
        return find_selected(await self.list(gid, scope), name)

    async def export_bytes(self, gid: int, scope: str | None = None) -> bytes:
        """O formato novo (com ``icon`` e ``scope``): ``ensure_ascii=False``, indentação 2. ``scope=None`` exporta os dois."""
        doc = await self.store.get_sections(gid, *SECTIONS)
        items = _items(doc, None if scope is None else _check_scope(scope))
        return json.dumps(to_export(items), ensure_ascii=False, indent=2).encode("utf-8")

    # ------------------------------------------------------------------ escrita

    async def _write(
        self,
        gid: int,
        scope: str,
        change: Callable[[tuple[Playlist, ...]], tuple[Playlist, ...] | RuleError],
        expected_rev: Optional[int],
    ) -> tuple[Any, tuple[Playlist, ...]]:
        """Roda ``change(lista do escopo) -> nova lista | RuleError`` sob a trava; a lista do outro escopo passa intacta.

        Grava só se a lista do escopo mudou e então atualiza o embed ocioso (só o escopo ``jukebox`` aparece nele).
        """
        _check_scope(scope)
        outcome: dict[str, Any] = {"result": None, "changed": False}

        def apply(doc: dict) -> bool:
            current = _items(doc, scope)
            result = change(current)
            outcome["result"] = result
            if isinstance(result, RuleError) or tuple(result) == current:
                return False
            outcome["changed"] = True
            others = [p for p in _items(doc) if p.scope != scope]
            doc["playlists"] = [to_doc(p) for p in (*others, *(p._replace(scope=scope) for p in result))]
            return True

        written = await self.store.mutate_doc(gid, SECTIONS, apply, expected_rev=expected_rev)
        if outcome["changed"] and scope == SCOPE_JUKEBOX:
            self.refresh_idle(gid)  # só depois de gravar
        result = outcome["result"]
        return result, (() if isinstance(result, RuleError) else _items(written, scope))

    async def add(
        self, gid: int, item: Playlist, expected_rev: Optional[int] = None, scope: str = SCOPE_JUKEBOX
    ) -> Playlist | RuleError:
        checked = _validated(item, _check_scope(scope))
        if isinstance(checked, RuleError):
            return checked
        result, items = await self._write(gid, scope, lambda current: add_item(current, checked), expected_rev)
        return result if isinstance(result, RuleError) else find_item(items, checked.name)  # type: ignore[return-value]

    async def edit(
        self, gid: int, name: str, item: Playlist, expected_rev: Optional[int] = None, scope: str = SCOPE_JUKEBOX
    ) -> Playlist | RuleError:
        checked = _validated(item, _check_scope(scope))
        if isinstance(checked, RuleError):
            return checked
        result, items = await self._write(gid, scope, lambda current: edit_item(current, name, checked), expected_rev)
        return result if isinstance(result, RuleError) else find_item(items, checked.name)  # type: ignore[return-value]

    async def remove(
        self, gid: int, name: str, expected_rev: Optional[int] = None, scope: str = SCOPE_JUKEBOX
    ) -> Playlist | RuleError:
        """Remove a playlist do ``scope`` e devolve a removida."""
        removed: dict[str, Optional[Playlist]] = {}

        def change(current: tuple[Playlist, ...]):
            removed["item"] = find_item(current, name)
            return remove_item(current, name)

        result, _ = await self._write(gid, scope, change, expected_rev)
        return result if isinstance(result, RuleError) else removed["item"]  # type: ignore[return-value]

    async def clear(self, gid: int, expected_rev: Optional[int] = None, scope: str = SCOPE_JUKEBOX) -> int:
        """Apaga todas as do ``scope`` e devolve quantas eram (o chamador exporta antes)."""
        count: dict[str, int] = {}

        def change(current: tuple[Playlist, ...]):
            count["n"] = len(current)
            return ()

        await self._write(gid, scope, change, expected_rev)
        return count["n"]

    async def import_file(
        self, gid: int, parsed: ParsedFile, expected_rev: Optional[int] = None, scope: str = SCOPE_JUKEBOX
    ) -> ImportReport:
        """Só acrescenta ao ``scope`` (nunca altera nem remove; o ``scope`` do arquivo é ignorado). O relatório só existe depois de gravar."""
        report: dict[str, ImportReport] = {}
        incoming = tuple(item._replace(scope=_check_scope(scope)) for item in parsed.items)

        def change(current: tuple[Playlist, ...]):
            planned = plan_import(current, incoming)
            report["value"] = planned
            return (*current, *planned.added)

        await self._write(gid, scope, change, expected_rev)
        planned = report["value"]
        return ImportReport(planned.added, (*parsed.invalid, *planned.skipped))

    # ------------------------------------------------------------------ migração de boot

    async def migrate_icons(self) -> tuple[int, int, tuple[int, ...]]:
        """Uma vez por subida: move o emoji do início do nome para o ``icon`` nas playlists já gravadas (``move_leading_icons``).

        Idempotente (rodar de novo não muda nada). Cada servidor é gravado sob a trava e com ``rev`` +1; um servidor que falha fica
        para a próxima subida. O log só leva contagens, nunca nomes. Devolve ``(servidores, itens, ids dos servidores)`` alterados:
        quem chama atualiza o embed ocioso deles (a gravação direta no store não passa pelo ``refresh_idle``).
        """
        servers = items = failed = 0
        changed: list[int] = []
        for gid in await self.store.stored_ids():
            count = 0

            def apply(doc: dict) -> bool:
                nonlocal count
                moved, count = move_leading_icons(doc.get("playlists") or [])
                if count:
                    doc["playlists"] = moved
                return bool(count)

            try:
                await self.store.mutate_stored(gid, apply)
            except Exception:  # noqa: BLE001 - o servidor fica para a próxima subida; o motivo já foi logado pelo store
                failed += 1
                continue
            if count:
                servers += 1
                items += count
                if gid.isdecimal():  # o id do documento é texto; o painel e os bots usam o inteiro
                    changed.append(int(gid))
        if servers or failed:
            logger.info(
                "Ícones das playlists migrados: %d itens em %d servidores (%d falharam)", items, servers, failed
            )
        return servers, items, tuple(changed)

    # ------------------------------------------------------------------ embed ocioso

    def refresh_idle(self, gid: int) -> None:
        """Atualiza, em segundo plano, o painel da Jukebox (o canal é único: só o bot do painel edita a mensagem)."""
        task = asyncio.get_running_loop().create_task(self._refresh(gid))
        self._refresh_tasks.add(task)
        task.add_done_callback(self._refresh_tasks.discard)

    async def _refresh(self, gid: int) -> None:
        panel = getattr(self.store.pool, "jukebox_panel", None)
        if panel is None:
            return
        try:
            await panel.refresh(gid)
        except Exception as exc:  # noqa: BLE001 - enfeite: a lista já foi gravada
            logger.warning("Painel da Jukebox não atualizado [%s] (%s)", gid, type(exc).__name__)
