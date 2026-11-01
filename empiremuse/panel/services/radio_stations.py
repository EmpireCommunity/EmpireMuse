"""Página Rádios · estações personalizadas do servidor (item 8, contrato R5 §5 "Web"): listar, criar, editar, apagar e
escolher a estação de uma rádio personalizada.

Os dados vêm de ``pool.radio.stations`` (Turbine): ``list_for_guild``, ``get``, ``create``, ``update``, ``delete``,
``radios_using`` e ``genre_ids``; a validação única (``check_station_doc``: nome de 1 a 32 único no servidor, uma só
fonte, até 10 artistas, até 5 playlists existentes, 10 estações por servidor) roda DENTRO da store, que levanta
``RadioInvalid`` com o ``RuleError`` apontando o campo. Aqui: o formulário vira o documento cru ``{name, kind, source}``
(só strings; nada vai ao log além dos ids), compare-and-set em ``rev``, estação de outro servidor é 404 e apagar uma
estação em uso é recusado com o canal que a usa. Gêneros e artistas vêm do Last.fm ("Powered by Last.fm").
"""

from __future__ import annotations

import logging
import re
from typing import Any, NamedTuple, Optional

from empiremuse.panel.services.base import (
    CONFLICT_TEXT,
    INVALID_TEXT,
    KIND_ERROR,
    KIND_OK,
    SAVED_TEXT,
    VERSION_TEXT,
    Args,
    FormContext,
    Formulario,
    first,
)
from empiremuse.panel.services.radio import RadioPage
from empiremuse.panel.services.service import STATUS_CONFLICT, STATUS_INVALID, STATUS_OK
from empiremuse.rules.i18n import Translator
from empiremuse.utils.radio.store import RadioConflict, RadioInvalid, RadioNotFound

logger = logging.getLogger(__name__)

KIND_GENRE, KIND_ARTISTS, KIND_PLAYLISTS = "genre", "artists", "playlists"
KINDS = (KIND_GENRE, KIND_ARTISTS, KIND_PLAYLISTS)
FIELDS = ("name", "kind", "genre", "artists", "playlists")
LIMIT_FIELD = "limit"
ERROR_KEYS = {"name": "name_rule"}  # chaves ``*.name`` têm o tamanho dos nomes de comando no validador do catálogo
NAME_MAX = 32
ARTISTS_MAX = 10
ARTIST_MAX = 64
PLAYLISTS_MAX = 5
STATIONS_MAX = 10
STATION_ID = re.compile(r"[0-9a-f]{24}", re.ASCII)
REV = re.compile(r"[0-9]{1,12}", re.ASCII)
ACTION_SAVE, ACTION_DELETE = "salvar", "apagar"
RAW_LIMIT = 4096  # o que se lê de cada campo antes da regra (ela corta nos limites reais)
LASTFM_URL = "https://www.last.fm"


class StationRow(NamedTuple):
    """Uma estação como a página mostra (textos já traduzidos ou escapados pelo template)."""

    id: str
    nome: str
    tipo: str
    fonte: tuple[str, ...]  # nome do gênero, artistas ou playlists
    avisos: tuple[str, ...]  # artistas que o Last.fm não achou (⚠️)
    em_uso: tuple[str, ...]  # canais das rádios que usam a estação
    form: Formulario


class StationsView(NamedTuple):
    rows: tuple[StationRow, ...]
    total: int
    maximo: int
    form: Formulario  # criar
    lastfm_url: str = LASTFM_URL


class CustomView(NamedTuple):
    """O seletor de estação de uma rádio personalizada."""

    radio_id: str
    slug: str
    form: Formulario


class StationsOutcome(NamedTuple):
    status: int
    view: StationsView


def station_id_of(raw: str) -> Optional[str]:
    return raw if STATION_ID.fullmatch(raw) else None


def belongs_to(station: Any, guild_id: int) -> bool:
    return int(station.guild_id) == int(guild_id)


def parse(args: Args) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(documento cru, eco)``: só os campos da estação entram (allowlist), e a fonte segue o tipo escolhido."""
    name = (first(args, "name") or "")[:RAW_LIMIT].strip()
    kind = (first(args, "kind") or "")[:RAW_LIMIT].strip()
    genre = (first(args, "genre") or "")[:RAW_LIMIT].strip()
    artists = [a.strip()[:RAW_LIMIT] for a in (first(args, "artists") or "")[:RAW_LIMIT].splitlines() if a.strip()]
    playlists = [p[:RAW_LIMIT] for p in args.get("playlists", []) if p][: PLAYLISTS_MAX + 1]
    source: dict[str, Any] = {}
    if kind == KIND_GENRE:
        source = {KIND_GENRE: genre}
    elif kind == KIND_ARTISTS:
        source = {KIND_ARTISTS: artists}
    elif kind == KIND_PLAYLISTS:
        source = {KIND_PLAYLISTS: playlists}
    raw = {"name": name, "kind": kind, "source": source}
    echo = {"name": name, "kind": kind, "genre": genre, "artists": "\n".join(artists), "playlists": playlists}
    return raw, echo


def field_of(error: Any) -> str:
    field = getattr(error, "field", None) or getattr(error, "key", "") or ""
    return field if field in FIELDS or field == LIMIT_FIELD else "name"


class StationsPage:
    """Leitura e edição das estações, sempre a partir do ``FormContext`` do usuário autorizado (``manage_guild``)."""

    # ------------------------------------------------------------------ leitura

    @staticmethod
    async def playlist_names(ctx: FormContext) -> tuple[str, ...]:
        items, _rev = await ctx.pool.servers.playlists.snapshot(ctx.guild_id)
        return tuple(str(item.name) for item in items)

    @staticmethod
    def genre_ids(ctx: FormContext) -> tuple[str, ...]:
        return tuple(str(g) for g in ctx.pool.radio.stations.genre_ids())

    def form(
        self,
        action: str,
        tr: Translator,
        *,
        genres: tuple[str, ...],
        playlists: tuple[str, ...],
        station: Any = None,
        values: Optional[dict[str, Any]] = None,
        errors: Optional[dict[str, str]] = None,
        message: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> Formulario:
        current = _values_of(station) if station is not None else {"kind": KIND_GENRE, "playlists": []}
        suffix = str(station.id) if station is not None else "nova"
        return Formulario(
            key="radio_station",
            form_id=f"form-estacao-{suffix}",
            template="form_radio_station.html",
            acao=action,
            versao=str(station.rev) if station is not None else "",
            valores={**current, **(values or {})},
            erros=errors or {},
            mensagem=message,
            mensagem_tipo=kind,
            extras={
                "station_id": str(station.id) if station is not None else None,
                "generos": tuple((g, tr(f"panel.radio.genre.{g}")) for g in genres),
                "playlists": playlists,
                "limites": {
                    "name_max": NAME_MAX,
                    "artists_max": ARTISTS_MAX,
                    "artist_max": ARTIST_MAX,
                    "playlists_max": PLAYLISTS_MAX,
                },
            },
        )

    async def view(
        self,
        ctx: FormContext,
        action: str,
        tr: Translator,
        *,
        form: Optional[Formulario] = None,
        edit: Optional[Formulario] = None,
    ) -> StationsView:
        """``action`` é ``.../radios/estacoes``; ``form`` substitui o de criar, ``edit`` o da estação com o mesmo id."""
        stations = await ctx.pool.radio.stations.list_for_guild(ctx.guild_id)
        genres, playlists = self.genre_ids(ctx), await self.playlist_names(ctx)
        rows = []
        for station in sorted(stations, key=lambda s: str(s.name).casefold()):
            if not belongs_to(station, ctx.guild_id):  # defesa em profundidade: a store já filtra pelo servidor
                continue
            item_action = f"{action}/{station.id}"
            row_form = (
                edit
                if edit is not None and edit.station_id == str(station.id)
                else self.form(item_action, tr, genres=genres, playlists=playlists, station=station)
            )
            rows.append(
                StationRow(
                    id=str(station.id),
                    nome=str(station.name),
                    tipo=tr(f"panel.radio.station.kind.{station.kind}") if station.kind in KINDS else str(station.kind),
                    fonte=_source_labels(station, tr),
                    avisos=tuple(str(a) for a in getattr(station, "unknown", ()) or ()),
                    em_uso=await self._in_use(ctx, station),
                    form=row_form,
                )
            )
        create = form if form is not None else self.form(action, tr, genres=genres, playlists=playlists)
        return StationsView(tuple(rows), len(rows), STATIONS_MAX, create)

    @staticmethod
    async def _in_use(ctx: FormContext, station: Any) -> tuple[str, ...]:
        radios = await ctx.pool.radio.stations.radios_using(station.id)
        return tuple(channel_label(ctx, radio) for radio in radios)

    # ------------------------------------------------------------------ escrita (R-6)

    async def create(self, ctx: FormContext, args: Args, action: str, tr: Translator) -> StationsOutcome:
        raw, echo = parse(args)
        genres, playlists = self.genre_ids(ctx), await self.playlist_names(ctx)
        try:
            saved = await ctx.pool.radio.stations.create(ctx.guild_id, raw, updated_by=ctx.user_id)
        except RadioInvalid as exc:
            form = self._refused(action, tr, exc, echo, genres, playlists)
            return StationsOutcome(STATUS_INVALID, await self.view(ctx, action, tr, form=form))
        _audit(ctx, saved, "criada")
        form = self.form(action, tr, genres=genres, playlists=playlists, message=SAVED_TEXT, kind=KIND_OK)
        return StationsOutcome(STATUS_OK, await self.view(ctx, action, tr, form=form))

    async def update(self, ctx: FormContext, station: Any, args: Args, action: str, tr: Translator) -> StationsOutcome:
        item_action = f"{action}/{station.id}"
        genres, playlists = self.genre_ids(ctx), await self.playlist_names(ctx)
        refused = self._version(station, args)
        if refused is not None:
            form = self.form(
                item_action,
                tr,
                genres=genres,
                playlists=playlists,
                station=station,
                message=refused[1],
                kind=KIND_ERROR,
            )
            return StationsOutcome(refused[0], await self.view(ctx, action, tr, edit=form))
        raw, echo = parse(args)
        try:
            saved = await ctx.pool.radio.stations.update(
                station.id, raw, expected_rev=int(station.rev), updated_by=ctx.user_id
            )
        except RadioConflict, RadioNotFound:
            form = self.form(
                item_action,
                tr,
                genres=genres,
                playlists=playlists,
                station=station,
                message=CONFLICT_TEXT,
                kind=KIND_ERROR,
            )
            return StationsOutcome(STATUS_CONFLICT, await self.view(ctx, action, tr, edit=form))
        except RadioInvalid as exc:
            form = self._refused(item_action, tr, exc, echo, genres, playlists, station=station)
            return StationsOutcome(STATUS_INVALID, await self.view(ctx, action, tr, edit=form))
        _audit(ctx, saved, "editada")
        form = self.form(
            item_action, tr, genres=genres, playlists=playlists, station=saved, message=SAVED_TEXT, kind=KIND_OK
        )
        return StationsOutcome(STATUS_OK, await self.view(ctx, action, tr, edit=form))

    async def delete(self, ctx: FormContext, station: Any, args: Args, action: str, tr: Translator) -> StationsOutcome:
        item_action = f"{action}/{station.id}"
        genres, playlists = self.genre_ids(ctx), await self.playlist_names(ctx)

        async def refuse(status: int, message: str) -> StationsOutcome:
            form = self.form(
                item_action, tr, genres=genres, playlists=playlists, station=station, message=message, kind=KIND_ERROR
            )
            return StationsOutcome(status, await self.view(ctx, action, tr, edit=form))

        refused = self._version(station, args)
        if refused is not None:
            return await refuse(*refused)
        try:
            await ctx.pool.radio.stations.delete(station.id, expected_rev=int(station.rev))
        except RadioConflict, RadioNotFound:
            return await refuse(STATUS_CONFLICT, CONFLICT_TEXT)
        except RadioInvalid as exc:
            radios = getattr(exc, "radios", None)
            if radios:  # em uso: a mensagem diz qual call a usa (R5 §2)
                channels = ", ".join(channel_label(ctx, r) for r in radios)
                return await refuse(STATUS_INVALID, tr("panel.radio.station.in_use", channels=channels))
            return await refuse(STATUS_INVALID, INVALID_TEXT)
        _audit(ctx, station, "apagada")
        form = self.form(
            action, tr, genres=genres, playlists=playlists, message=tr("panel.radio.station.deleted"), kind=KIND_OK
        )
        return StationsOutcome(STATUS_OK, await self.view(ctx, action, tr, form=form))

    @staticmethod
    def _version(station: Any, args: Args) -> Optional[tuple[int, str]]:
        sent = first(args, "versao")
        if sent is None or not REV.fullmatch(sent):
            return STATUS_INVALID, VERSION_TEXT
        if int(sent) != int(station.rev):
            return STATUS_CONFLICT, CONFLICT_TEXT
        return None

    def _refused(
        self,
        action: str,
        tr: Translator,
        exc: RadioInvalid,
        echo: dict[str, Any],
        genres: tuple[str, ...],
        playlists: tuple[str, ...],
        *,
        station: Any = None,
    ) -> Formulario:
        error = getattr(exc, "error", None) or (exc.args[0] if exc.args else None)
        field = field_of(error)
        if field == LIMIT_FIELD:
            message = tr("panel.radio.station.limit", max=STATIONS_MAX)
            errors: dict[str, str] = {}
        else:
            message = INVALID_TEXT
            errors = {field: tr(f"panel.radio.station.error.{ERROR_KEYS.get(field, field)}")}
        return self.form(
            action,
            tr,
            genres=genres,
            playlists=playlists,
            station=station,
            values=echo,
            errors=errors,
            message=message,
            kind=KIND_ERROR,
        )

    # ------------------------------------------------------------------ a estação de uma rádio personalizada

    def custom_view(
        self,
        ctx: FormContext,
        radio: Any,
        action: str,
        stations: Any,
        tr: Translator,
        *,
        message: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> CustomView:
        """O seletor da rádio personalizada: as estações do servidor e a atual (``source.station``)."""
        current = str(radio.source.get("station") or "")
        options = tuple((str(s.id), str(s.name)) for s in sorted(stations, key=lambda s: str(s.name).casefold()))
        slug = str(radio.id).replace(":", "-")
        form = Formulario(
            key="radio_estacao",
            form_id=f"form-estacao-radio-{slug}",
            template="form_radio_estacao.html",
            acao=f"{action}/estacao",
            versao=str(radio.rev),
            valores={"estacao": current},
            mensagem=message,
            mensagem_tipo=kind,
            extras={"slug": slug, "opcoes": options, "atual": next((n for i, n in options if i == current), None)},
        )
        return CustomView(str(radio.id), slug, form)

    @staticmethod
    async def set_custom_station(ctx: FormContext, radio: Any, args: Args, tr: Translator) -> tuple[int, str, str, Any]:
        """``(status, mensagem, tipo, rádio depois)``: grava ``source.station`` com CAS em ``rev`` e avisa a rádio ligada."""
        sent = first(args, "versao")
        if sent is None or not REV.fullmatch(sent):
            return STATUS_INVALID, VERSION_TEXT, KIND_ERROR, radio
        if int(sent) != int(radio.rev):
            return STATUS_CONFLICT, CONFLICT_TEXT, KIND_ERROR, radio
        wanted = station_id_of((first(args, "estacao") or "").strip())
        if wanted is None:
            return STATUS_INVALID, tr("panel.radio.station.pick_invalid"), KIND_ERROR, radio
        if wanted == str(radio.source.get("station") or ""):
            return STATUS_OK, SAVED_TEXT, KIND_OK, radio
        try:
            saved = await ctx.pool.radio.store.set_custom_station(
                radio.id, wanted, expected_rev=int(radio.rev), updated_by=ctx.user_id
            )
        except RadioConflict, RadioNotFound:
            return STATUS_CONFLICT, CONFLICT_TEXT, KIND_ERROR, radio
        except RadioInvalid:
            return STATUS_INVALID, tr("panel.radio.station.pick_invalid"), KIND_ERROR, radio
        logger.info(  # só ids e o nome do campo (R-12)
            "Configuração alterada: usuario=%s servidor=%s escopo=radio:%s categoria=radios campos=source.station",
            ctx.user_id,
            ctx.guild_id,
            radio.bot_id,
        )
        try:
            await ctx.pool.radio.manager.change_station(saved)
        except Exception as exc:
            logger.warning("Estação da rádio gravada, mas a rádio ligada não trocou (%s)", type(exc).__name__)
        return STATUS_OK, SAVED_TEXT, KIND_OK, saved


def channel_label(ctx: FormContext, radio: Any) -> str:
    bot = RadioPage.bot_of(ctx, radio)
    channel = bot.get_channel(int(radio.channel_id)) if bot is not None else None
    return str(channel.name) if channel is not None else f"#{radio.channel_id}"


def _values_of(station: Any) -> dict[str, Any]:
    source = dict(getattr(station, "source", {}) or {})
    return {
        "name": str(station.name),
        "kind": str(station.kind),
        "genre": str(source.get(KIND_GENRE) or ""),
        "artists": "\n".join(str(a) for a in source.get(KIND_ARTISTS) or ()),
        "playlists": [str(p) for p in source.get(KIND_PLAYLISTS) or ()],
    }


def _source_labels(station: Any, tr: Translator) -> tuple[str, ...]:
    source = dict(getattr(station, "source", {}) or {})
    if station.kind == KIND_GENRE:
        genre = str(source.get(KIND_GENRE) or "")
        return (tr(f"panel.radio.genre.{genre}"),) if genre else ()
    return tuple(str(x) for x in source.get(str(station.kind)) or ())


def _audit(ctx: FormContext, station: Any, what: str) -> None:
    logger.info(  # só ids (R-12): nome, artistas e playlists nunca vão para o log
        "Configuração alterada: usuario=%s servidor=%s escopo=estacao:%s categoria=radios campos=%s",
        ctx.user_id,
        ctx.guild_id,
        station.id,
        what,
    )
