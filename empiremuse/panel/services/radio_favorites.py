"""Página Rádios · "Favoritas do servidor" (J3, contrato Jukebox §4.2 e §6): a lista do servidor, Remover com confirmação
e Adicionar pelo seletor de local.

Quem vê e quem age: só quem gerencia o servidor. A página já exige o ``GuildAccess`` do painel; aqui o bloco some para
quem não passa em ``can_manage_guild`` (defesa em profundidade) e o store (``RadioFavorites.add/remove``) reconfere o
ator em toda escrita. A lista de estações por local lê o diretório e exige premium (contrato §6, como a busca): sem
premium nada vai ao diretório, e a lista salva e o Remover seguem valendo. O ator é o usuário da SESSÃO (``ctx.user_id``): nenhum campo do corpo escolhe ator nem servidor (o
servidor é o da rota, já autorizado). A estação a adicionar é relida pelo diretório (``by_uuid``) e o nome nunca vem do
formulário. ``FavoriteForbidden`` → 403, estação não encontrada → 404 (refazer), banco ou diretório fora → 503 (tentar de
novo). Nomes externos só passam como texto (autoescape) depois da função única de texto externo. Nenhum log leva uuid ou
nome de estação.
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple, Optional

from empiremuse.panel.services.base import (
    KIND_ERROR,
    KIND_OK,
    PANEL_LOCALE,
    Args,
    FormContext,
    first,
)
from empiremuse.panel.services.radio_local import (
    PAGE_SIZE,
    LocalPage,
    LocalView,
    Paging,
    guild_locale_of,
    parse_page,
    resolve_node,
    tree_of,
)
from empiremuse.panel.services.service import STATUS_INVALID, STATUS_OK
from empiremuse.rules.i18n import Translator
from empiremuse.rules.radio_stations import valid_uuid
from empiremuse.rules.text import external_text
from empiremuse.utils.radio.directory import DirectoryError
from empiremuse.utils.radio.favorites import MAX_FAVORITES, AddResult, FavoriteForbidden
from empiremuse.utils.radio.perms import can_manage_guild
from empiremuse.utils.radio.premium import RadioUnavailable

logger = logging.getLogger(__name__)

FAVORITES_PATH = "favoritas"
ACTION_ADD = "adicionar"
ACTION_REMOVE = "remover"
STATION_FIELD = "estacao"
NAME_LIMIT = 100
STATUS_FORBIDDEN = 403
STATUS_NOT_FOUND = 404
STATUS_FULL = 409
STATUS_UNAVAILABLE = 503


class FavoriteRow(NamedTuple):
    uuid: str
    nome: str


class ResultRow(NamedTuple):
    uuid: str
    nome: str
    pais: str
    codec: str
    bitrate: int
    ja_favorita: bool
    indisponivel: bool


class FavoritesView(NamedTuple):
    """O bloco "Favoritas do servidor". ``slug`` e ``acao_local`` dão a ``_radio_local.html`` o que ele pede de ``vivo``/``form``."""

    acao: str
    acao_local: str
    itens: tuple[FavoriteRow, ...]
    total: int
    maximo: int
    cheia: bool
    mensagem: Optional[str] = None
    mensagem_tipo: Optional[str] = None
    local: Optional[LocalView] = None
    resultados: tuple[ResultRow, ...] = ()
    dica: Optional[str] = None
    paginacao: Optional[Paging] = None
    slug: str = FAVORITES_PATH


class FavoritesOutcome(NamedTuple):
    status: int
    message: str
    kind: str


def actor_of(ctx: FormContext) -> Optional[int]:
    """O id numérico do usuário da sessão, ou ``None`` (a sessão é a única fonte do ator)."""
    return int(ctx.user_id) if ctx.user_id.isdigit() else None


async def can_manage(ctx: FormContext) -> bool:
    actor = actor_of(ctx)
    return actor is not None and await can_manage_guild(ctx.pool, ctx.guild_id, actor)


class ServerFavoritesPage:
    def __init__(self) -> None:
        self.local = LocalPage()

    # ------------------------------------------------------------------ leitura

    async def view(
        self,
        ctx: FormContext,
        action: str,
        *,
        message: Optional[str] = None,
        kind: Optional[str] = None,
        node: Any = None,
        page: int = 1,
        tr: Optional[Translator] = None,
    ) -> Optional[FavoritesView]:
        """O bloco, ou ``None`` para quem não gerencia o servidor (a tela esconde as ações administrativas)."""
        store = getattr(ctx.pool.radio, "favorites", None)
        if store is None or not await can_manage(ctx):
            return None
        favorites = await store.list(ctx.guild_id)
        items = tuple(FavoriteRow(f.uuid, external_text(f.name, NAME_LIMIT, markdown=False)) for f in favorites)
        results: tuple[ResultRow, ...] = ()
        hint: Optional[str] = None
        paging: Optional[Paging] = None
        tree = tree_of(ctx)
        local = self.local.view(tree, node, PANEL_LOCALE, guild_locale_of(ctx)) if tree is not None else None
        if tree is not None and node is not None and getattr(node, "final", False) and tr is not None:
            if (
                await ctx.pool.radio.premium.get(ctx.guild_id) is None
            ):  # sem premium: nada vai ao diretório (balde global)
                hint = tr("panel.radio.premium.none")
            else:
                results, hint, paging = await self._results(ctx, node, page, {f.uuid for f in favorites}, tr)
        return FavoritesView(
            acao=f"{action}/{FAVORITES_PATH}",
            acao_local=f"{action}/{FAVORITES_PATH}/local",
            itens=items,
            total=len(items),
            maximo=MAX_FAVORITES,
            cheia=len(items) >= MAX_FAVORITES,
            mensagem=message,
            mensagem_tipo=kind,
            local=local,
            resultados=results,
            dica=hint,
            paginacao=paging,
        )

    @staticmethod
    async def _results(
        ctx: FormContext, node: Any, page: int, saved: set[str], tr: Translator
    ) -> tuple[tuple[ResultRow, ...], Optional[str], Paging]:
        """As estações do nó final, 24 por página, pelo balde do servidor no diretório (R4 §4)."""
        found = await ctx.pool.radio.directory.region(node, key=ctx.guild_id)
        health = ctx.pool.radio.health
        rows = tuple(
            ResultRow(
                str(hit.station.uuid),
                external_text(hit.station.name, NAME_LIMIT, markdown=False),
                external_text(hit.station.country, NAME_LIMIT, markdown=False),
                external_text(hit.station.codec, 20, markdown=False),
                int(hit.station.bitrate),
                hit.station.uuid in saved,
                bool(health.unavailable(hit.station.uuid)),
            )
            for hit in found.hits
        )
        pages = max(1, -(-len(rows) // PAGE_SIZE))
        page = min(page, pages)
        hint = None
        if not rows:
            hint = tr("panel.radio.local.no_results")
        elif getattr(found, "partial", False):
            hint = tr("panel.radio.local.partial")
        return rows[(page - 1) * PAGE_SIZE : page * PAGE_SIZE], hint, Paging(str(node.id), page, pages)

    # ------------------------------------------------------------------ escrita

    async def add(self, ctx: FormContext, args: Args, tr: Translator) -> FavoritesOutcome:
        refused = await self._precheck(ctx, args, tr)
        if isinstance(refused, FavoritesOutcome):
            return refused
        radio, actor, uuid = refused
        try:
            station = await radio.directory.by_uuid_strict(
                uuid
            )  # relido aqui (nome do formulário não vale); falha = DirectoryError
        except DirectoryError:
            return FavoritesOutcome(STATUS_UNAVAILABLE, tr("panel.radio.favorites.unavailable"), KIND_ERROR)
        if station is None:
            return FavoritesOutcome(STATUS_NOT_FOUND, tr("panel.radio.favorites.not_found"), KIND_ERROR)
        try:
            result = await radio.favorites.add(ctx.guild_id, actor, station)
        except FavoriteForbidden:
            return FavoritesOutcome(STATUS_FORBIDDEN, tr("panel.radio.favorites.forbidden"), KIND_ERROR)
        except RadioUnavailable:
            return FavoritesOutcome(STATUS_UNAVAILABLE, tr("panel.radio.favorites.unavailable"), KIND_ERROR)
        if result is AddResult.FULL:
            return FavoritesOutcome(STATUS_FULL, tr("panel.radio.favorites.full", max=MAX_FAVORITES), KIND_ERROR)
        if result is AddResult.ADDED:
            _audit(ctx, ACTION_ADD)
        name = external_text(station.name, NAME_LIMIT, markdown=False)
        key = "added" if result is AddResult.ADDED else "already"
        return FavoritesOutcome(STATUS_OK, tr(f"panel.radio.favorites.{key}", station=name), KIND_OK)

    async def remove(self, ctx: FormContext, args: Args, tr: Translator) -> FavoritesOutcome:
        refused = await self._precheck(ctx, args, tr)
        if isinstance(refused, FavoritesOutcome):
            return refused
        radio, actor, uuid = refused
        try:
            changed = await radio.favorites.remove(ctx.guild_id, actor, uuid)
        except FavoriteForbidden:
            return FavoritesOutcome(STATUS_FORBIDDEN, tr("panel.radio.favorites.forbidden"), KIND_ERROR)
        except RadioUnavailable:
            return FavoritesOutcome(STATUS_UNAVAILABLE, tr("panel.radio.favorites.unavailable"), KIND_ERROR)
        if changed:
            _audit(ctx, ACTION_REMOVE)
        key = "removed" if changed else "already_removed"
        return FavoritesOutcome(STATUS_OK, tr(f"panel.radio.favorites.{key}"), KIND_OK)

    @staticmethod
    async def _precheck(ctx: FormContext, args: Args, tr: Translator) -> Any:
        """``(radio, ator, uuid)`` ou a recusa: sem ator na sessão, sem gate ou com uuid fora do formato."""
        actor = actor_of(ctx)
        if actor is None or not await can_manage_guild(ctx.pool, ctx.guild_id, actor):
            return FavoritesOutcome(STATUS_FORBIDDEN, tr("panel.radio.favorites.forbidden"), KIND_ERROR)
        uuid = (first(args, STATION_FIELD) or "").strip()
        if not valid_uuid(uuid):
            return FavoritesOutcome(STATUS_INVALID, tr("panel.radio.favorites.invalid"), KIND_ERROR)
        return ctx.pool.radio, actor, uuid


def node_of(ctx: FormContext, raw: Any) -> Any:
    """O nó da cascata que o usuário mandou, revalidado pela árvore (id forjado ou árvore desligada = ``None``)."""
    tree = tree_of(ctx)
    return resolve_node(tree, raw) if tree is not None and raw else None


def page_of(raw: Any) -> int:
    return parse_page(raw)


def _audit(ctx: FormContext, field: str) -> None:
    logger.info(  # só ids e o tipo da ação (R-12): uuid e nome da estação nunca vão para o log
        "Configuração alterada: usuario=%s servidor=%s categoria=radios campos=favoritas.%s",
        ctx.user_id,
        ctx.guild_id,
        field,
    )
