"""Página Rádios · rádio ao vivo (item 8, contrato R3 §4): estação principal e reserva, seletor por busca e estado.

Os dados vêm de ``pool.radio`` (Turbine): ``directory`` (``search`` e ``by_uuid`` no Radio Browser, com limite global de
2 req/s e cache), ``health`` (``unavailable``, síncrono) e ``store.set_reserve`` (compare-and-set em ``rev``; recusa rádio
que não é ao vivo, uuid inválido e reserva igual à principal). Aqui: a busca só para quem pode editar a rádio (premium e
``manage_messages`` no canal, R-7), termo por ``check_search_term`` (termo ruim nunca chega ao diretório), a
``url_resolved`` nunca vai para a página, nomes e países já passaram pela função única da R-11 (só autoescape), ⛔ para
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
    SAVED_TEXT,
    VERSION_TEXT,
    Args,
    FormContext,
    Formulario,
    first,
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
    """O bloco "Estação ao vivo" de uma rádio: principal, reserva e, para quem pode editar, o seletor."""

    radio_id: str
    slug: str
    principal: Optional[StationView]
    reserva: Optional[StationView]
    form: Optional[Formulario]


class LiveOutcome(NamedTuple):
    status: int
    view: LiveView


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


class LivePage:
    def __init__(self) -> None:
        self.names = StationNames()

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
    ) -> LiveView:
        """``action`` é a URL da rádio (``.../radios/<bot:guild>``); o seletor envia para ``/reserva`` e busca em ``/estacoes``."""
        principal = await self.station_view(ctx, radio.source.get("station"))
        reserva = await self.station_view(ctx, radio.source.get("reserve"))
        form = None
        if editable:
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
                    "resultados": tuple(results),
                    "dica": hint,
                },
            )
        return LiveView(str(radio.id), slug_of(radio), principal, reserva, form)

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

    # ------------------------------------------------------------------ reserva

    async def set_reserve(self, ctx: FormContext, radio: Any, args: Args, action: str, tr: Translator) -> LiveOutcome:
        async def outcome(status: int, message: str, kind: str, saved: Any = radio) -> LiveOutcome:
            return LiveOutcome(status, await self.view(ctx, saved, action, True, message=message, kind=kind))

        sent = first(args, "versao")
        if sent is None or not (sent.isdigit() and len(sent) <= REV_DIGITS):
            return await outcome(STATUS_INVALID, VERSION_TEXT, KIND_ERROR)
        if int(sent) != int(radio.rev):
            return await outcome(STATUS_CONFLICT, CONFLICT_TEXT, KIND_ERROR)
        if radio.kind != KIND_LIVE:
            return await outcome(STATUS_INVALID, tr("panel.radio.live.not_live"), KIND_ERROR)
        wanted = (first(args, RESERVE_FIELD) or "").strip() or None
        if wanted is not None and (
            not valid_uuid(wanted) or wanted == radio.source.get("station") or ctx.pool.radio.health.unavailable(wanted)
        ):
            return await outcome(STATUS_INVALID, tr("panel.radio.live.invalid"), KIND_ERROR)
        if wanted == radio.source.get("reserve"):
            return await outcome(STATUS_OK, SAVED_TEXT, KIND_OK)
        try:
            saved = await ctx.pool.radio.store.set_reserve(
                radio.id, wanted, expected_rev=int(radio.rev), updated_by=ctx.user_id
            )
        except RadioConflict, RadioNotFound:  # outro escritor venceu, ou a rádio foi apagada: recarregar
            return await outcome(STATUS_CONFLICT, CONFLICT_TEXT, KIND_ERROR)
        except RadioInvalid:
            return await outcome(STATUS_INVALID, tr("panel.radio.live.invalid"), KIND_ERROR)
        logger.info(  # só ids e o nome do campo (R-12): o uuid da estação não vai para o log
            "Configuração alterada: usuario=%s servidor=%s escopo=radio:%s categoria=radios campos=source.reserve",
            ctx.user_id,
            ctx.guild_id,
            radio.bot_id,
        )
        return await outcome(STATUS_OK, SAVED_TEXT, KIND_OK, saved)


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
