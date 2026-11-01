"""Página Rádios · rádio ao vivo (item 8, contratos R3 §4 e R4 §5): estação principal e reserva, seletor por busca ou por
local, e estado.

Os dados vêm de ``pool.radio`` (Turbine): ``directory`` (``search``, ``by_uuid`` e ``region`` no Radio Browser, com limite
global de 2 req/s e cache), ``health`` (``unavailable``, síncrono), ``store.set_reserve``/``set_station`` (compare-and-set
em ``rev``; recusam rádio que não é ao vivo, uuid inválido e reserva igual à principal) e ``manager.change_station`` (a
rádio ligada troca de estação na hora). Aqui: a busca e a cascata só para quem pode editar a rádio (premium e
``manage_messages`` no canal, R-7), termo por ``check_search_term`` e nó por ``tree.node`` (nada ruim chega ao diretório),
a ``url_resolved`` nunca vai para a página, nomes e países já passaram pela função única da R-11 (só autoescape), ⛔ para
estação indisponível e um cache local de nomes (10 min) para a página não gastar o bucket do diretório a cada carregamento.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, NamedTuple, Optional

from empiremuse.panel.services.base import (
    CONFLICT_TEXT,
    KIND_ERROR,
    KIND_OK,
    PANEL_LOCALE,
    SAVED_TEXT,
    VERSION_TEXT,
    Args,
    FormContext,
    Formulario,
    first,
)
from empiremuse.panel.services.radio_local import (
    NODE_FIELD,
    PAGE_FIELD,
    LocalPage,
    LocalView,
    Paging,
    guild_locale_of,
    parse_page,
    resolve_node,
    tree_of,
)
from empiremuse.panel.services.service import STATUS_CONFLICT, STATUS_INVALID, STATUS_OK
from empiremuse.rules.errors import RuleError
from empiremuse.rules.i18n import Translator
from empiremuse.rules.radio import KIND_LIVE
from empiremuse.rules.radio_stations import check_search_term, valid_uuid
from empiremuse.utils.radio.store import RadioConflict, RadioInvalid, RadioNotFound

logger = logging.getLogger(__name__)

NAME_CACHE_TTL_S = 600
NAME_CACHE_SIZE = 500
RESERVE_FIELD = "reserva"
STATION_FIELD = "estacao"
SEARCH_FIELD = "q"
SEARCH_RAW_LIMIT = 200  # o que se lê da query antes da regra (ela corta em 64)
REV_DIGITS = 12


class StationView(NamedTuple):
    """Uma estação como a página mostra: nunca a URL do fluxo (R-7)."""

    uuid: str
    nome: Optional[str]  # ``None`` = o diretório não respondeu ou não conhece o uuid
    pais: str
    codec: str
    bitrate: int
    indisponivel: bool


class LiveView(NamedTuple):
    """O bloco "Estação ao vivo" de uma rádio: principal, reserva e, para quem pode editar, os seletores."""

    radio_id: str
    slug: str
    principal: Optional[StationView]
    reserva: Optional[StationView]
    form: Optional[Formulario]
    local: Optional[LocalView] = None  # a cascata de local (R4), só com a árvore carregada


class LiveOutcome(NamedTuple):
    """O que um POST decidiu; a página é montada depois, com a lista que o usuário estava vendo."""

    status: int
    message: str
    kind: str
    radio: Any  # a rádio depois da gravação (ou a mesma, quando nada mudou)


class ListState(NamedTuple):
    """De onde veio a lista de estações aberta na página: busca por nome, ou nó e página da cascata."""

    term: str = ""
    node: str = ""
    page: int = 1


class StationNames:
    """``uuid -> Station`` com validade: só a primeira carga de cada estação consulta o diretório."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._items: dict[str, tuple[float, Any]] = {}
        self._clock = clock

    async def get(self, directory: Any, uuid: str) -> Any:
        now = self._clock()
        hit = self._items.get(uuid)
        if hit is not None and now - hit[0] < NAME_CACHE_TTL_S:
            return hit[1]
        station = await directory.by_uuid(uuid)
        if station is not None:  # falha não entra no cache: a próxima carga tenta de novo
            if len(self._items) >= NAME_CACHE_SIZE:
                self._items.pop(next(iter(self._items)))
            self._items[uuid] = (now, station)
        return station


def slug_of(radio: Any) -> str:
    return str(radio.id).replace(":", "-")


def list_state(args: Args) -> ListState:
    """Os campos escondidos que a lista de estações carrega no POST, para a página voltar com a mesma lista."""
    return ListState(
        (first(args, SEARCH_FIELD) or "")[:SEARCH_RAW_LIMIT],
        (first(args, NODE_FIELD) or "").strip(),
        parse_page(first(args, PAGE_FIELD)),
    )


class LivePage:
    def __init__(self) -> None:
        self.names = StationNames()
        self.local = LocalPage()

    # ------------------------------------------------------------------ leitura

    async def station_view(self, ctx: FormContext, uuid: Any) -> Optional[StationView]:
        if not uuid or not valid_uuid(uuid):
            return None
        station = await self.names.get(ctx.pool.radio.directory, uuid)
        return _view(station, uuid, ctx.pool.radio.health)

    async def view(
        self,
        ctx: FormContext,
        radio: Any,
        action: str,
        editable: bool,
        *,
        results: tuple[StationView, ...] = (),
        hint: Optional[str] = None,
        term: str = "",
        message: Optional[str] = None,
        kind: Optional[str] = None,
        local: Optional[LocalView] = None,
        paging: Optional[Paging] = None,
    ) -> LiveView:
        """``action`` é a URL da rádio (``.../radios/<bot:guild>``); os seletores usam ``/reserva``, ``/estacao``,
        ``/estacoes`` (busca) e ``/local`` (cascata)."""
        principal = await self.station_view(ctx, radio.source.get("station"))
        reserva = await self.station_view(ctx, radio.source.get("reserve"))
        form = None
        if editable:
            if local is None:
                local = self.local_view(ctx, None)
            form = Formulario(
                key="radio_reserva",
                form_id=f"form-reserva-{slug_of(radio)}",
                template="form_radio_reserva.html",
                acao=f"{action}/reserva",
                versao=str(radio.rev),
                valores={SEARCH_FIELD: term},
                mensagem=message,
                mensagem_tipo=kind,
                extras={
                    "radio_id": str(radio.id),
                    "slug": slug_of(radio),
                    "acao_busca": f"{action}/estacoes",
                    "acao_local": f"{action}/local",
                    "acao_estacao": f"{action}/estacao",
                    "resultados": tuple(results),
                    "dica": hint,
                    "paginacao": paging,
                },
            )
        return LiveView(str(radio.id), slug_of(radio), principal, reserva, form, local)

    def local_view(self, ctx: FormContext, node: Any) -> Optional[LocalView]:
        """A cascata até ``node`` (``None`` = topo), ou ``None`` sem a árvore carregada."""
        tree = tree_of(ctx)
        if tree is None:
            return None
        return self.local.view(tree, node, PANEL_LOCALE, guild_locale_of(ctx))

    async def reload(
        self, ctx: FormContext, radio: Any, action: str, state: ListState, tr: Translator, outcome: LiveOutcome
    ) -> LiveView:
        """A página depois de um POST: a mesma lista que o usuário estava vendo (busca ou nó da cascata), já sem a
        principal nova; nada do usuário vai ao diretório sem passar pela regra ou pela árvore."""
        results: tuple[StationView, ...] = ()
        hint = None
        local = None
        paging = None
        tree = tree_of(ctx)
        node = resolve_node(tree, state.node) if tree is not None and state.node else None
        if node is not None:
            if getattr(node, "final", False):
                results, hint, paging = await self.stations(ctx, radio, node, state.page, tr)
            local = self.local_view(ctx, node)
        elif state.term:
            results, hint = await self.search(ctx, radio, state.term, tr)
        return await self.view(
            ctx,
            radio,
            action,
            True,
            results=results,
            hint=hint,
            term=state.term,
            message=outcome.message,
            kind=outcome.kind,
            local=local,
            paging=paging,
        )

    async def stations(
        self, ctx: FormContext, radio: Any, node: Any, page: int, tr: Translator
    ) -> tuple[tuple[StationView, ...], Optional[str], Paging]:
        """As estações de um nó final da cascata, 24 por página (R4 §5)."""
        return await self.local.stations(ctx, radio, node, page, tr, _view)

    # ------------------------------------------------------------------ busca (R-7)

    @staticmethod
    async def search(
        ctx: FormContext, radio: Any, raw: str, tr: Translator
    ) -> tuple[tuple[StationView, ...], Optional[str]]:
        """Resultados e a dica a mostrar; termo fora da regra nunca chega ao diretório, e a principal fica de fora."""
        term = check_search_term(raw[:SEARCH_RAW_LIMIT])
        if isinstance(term, RuleError):
            return (), tr("panel.radio.live.search_term")
        stations = await ctx.pool.radio.directory.search(term, key=ctx.guild_id)  # limite por servidor (R-3)
        main = radio.source.get("station")
        health = ctx.pool.radio.health
        views = tuple(_view(s, s.uuid, health) for s in stations if s.uuid != main)
        return views, (None if views else tr("panel.radio.live.search_empty"))

    # ------------------------------------------------------------------ reserva e principal (R-6)

    @staticmethod
    def _precheck(radio: Any, args: Args, tr: Translator) -> Optional[LiveOutcome]:
        sent = first(args, "versao")
        if sent is None or not (sent.isdigit() and len(sent) <= REV_DIGITS):
            return LiveOutcome(STATUS_INVALID, VERSION_TEXT, KIND_ERROR, radio)
        if int(sent) != int(radio.rev):
            return LiveOutcome(STATUS_CONFLICT, CONFLICT_TEXT, KIND_ERROR, radio)
        if radio.kind != KIND_LIVE:
            return LiveOutcome(STATUS_INVALID, tr("panel.radio.live.not_live"), KIND_ERROR, radio)
        return None

    async def set_reserve(self, ctx: FormContext, radio: Any, args: Args, tr: Translator) -> LiveOutcome:
        refused = self._precheck(radio, args, tr)
        if refused is not None:
            return refused
        invalid = LiveOutcome(STATUS_INVALID, tr("panel.radio.live.invalid"), KIND_ERROR, radio)
        wanted = (first(args, RESERVE_FIELD) or "").strip() or None
        if wanted is not None and (
            not valid_uuid(wanted) or wanted == radio.source.get("station") or ctx.pool.radio.health.unavailable(wanted)
        ):
            return invalid
        if wanted == radio.source.get("reserve"):
            return LiveOutcome(STATUS_OK, SAVED_TEXT, KIND_OK, radio)
        try:
            saved = await ctx.pool.radio.store.set_reserve(
                radio.id, wanted, expected_rev=int(radio.rev), updated_by=ctx.user_id
            )
        except RadioConflict, RadioNotFound:  # outro escritor venceu, ou a rádio foi apagada: recarregar
            return LiveOutcome(STATUS_CONFLICT, CONFLICT_TEXT, KIND_ERROR, radio)
        except RadioInvalid:
            return invalid
        _audit(ctx, radio, "source.reserve")
        return LiveOutcome(STATUS_OK, SAVED_TEXT, KIND_OK, saved)

    async def set_station(self, ctx: FormContext, radio: Any, args: Args, tr: Translator) -> LiveOutcome:
        """A principal nova: gravada com CAS e aplicada na hora à rádio ligada (``manager.change_station``)."""
        refused = self._precheck(radio, args, tr)
        if refused is not None:
            return refused
        invalid = LiveOutcome(STATUS_INVALID, tr("panel.radio.live.main_invalid"), KIND_ERROR, radio)
        wanted = (first(args, STATION_FIELD) or "").strip()
        if not wanted or not valid_uuid(wanted) or ctx.pool.radio.health.unavailable(wanted):
            return invalid
        if wanted == radio.source.get("station"):
            return LiveOutcome(STATUS_OK, SAVED_TEXT, KIND_OK, radio)
        try:
            saved = await ctx.pool.radio.store.set_station(
                radio.id, wanted, expected_rev=int(radio.rev), updated_by=ctx.user_id
            )
        except RadioConflict, RadioNotFound:
            return LiveOutcome(STATUS_CONFLICT, CONFLICT_TEXT, KIND_ERROR, radio)
        except RadioInvalid:
            return invalid
        _audit(ctx, radio, "source.station")
        try:  # o dado já está gravado: a rádio ligada troca agora, e uma falha aqui só avisa (como no chat)
            await ctx.pool.radio.manager.change_station(saved)
        except Exception as exc:
            logger.warning("Estação principal gravada, mas a rádio ligada não trocou (%s)", type(exc).__name__)
        return LiveOutcome(STATUS_OK, SAVED_TEXT, KIND_OK, saved)


def _audit(ctx: FormContext, radio: Any, field: str) -> None:
    logger.info(  # só ids e o nome do campo (R-12): o uuid da estação não vai para o log
        "Configuração alterada: usuario=%s servidor=%s escopo=radio:%s categoria=radios campos=%s",
        ctx.user_id,
        ctx.guild_id,
        radio.bot_id,
        field,
    )


def _view(station: Any, uuid: str, health: Any) -> StationView:
    if station is None:
        return StationView(str(uuid), None, "", "", 0, bool(health.unavailable(uuid)))
    return StationView(
        str(station.uuid),
        str(station.name),
        str(station.country),
        str(station.codec),
        int(station.bitrate),
        bool(health.unavailable(station.uuid)),
    )
