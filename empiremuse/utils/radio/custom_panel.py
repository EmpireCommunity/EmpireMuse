"""O painel da rádio PERSONALIZADA no desenho do visor: o retrato (mesmo ``RadioPanelSnapshot`` da ao vivo) e os cliques dos dois selects.

Select 1 = as estações do servidor do filtro aplicado; select 2 = filtro por TIPO da estação (``f:genre``, ``f:artists``, ``f:playlists``). O filtro
fica gravado em ``Radio.filter`` (lembrado por rádio); sem filtro = todas as estações do servidor. "Favoritas" ainda não existe aqui (decisão do dono pendente).
O valor de um select NUNCA é confiado: a estação tem de ser do servidor e estar no filtro; o filtro tem de ser um dos tipos.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from empiremuse.rules.radio import Radio, station_neighbor
from empiremuse.rules.radio_station import KIND_ARTISTS, KIND_GENRE, KIND_PLAYLISTS, CustomStation
from empiremuse.rules.text import external_text
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.radio import actions
from empiremuse.utils.radio.snapshot import (
    LIST_STATIONS,
    STATE_LIVE,
    STATE_MUTED,
    STATE_OFFLINE,
    StationOption,
    _window,
)

if TYPE_CHECKING:
    from empiremuse.cogs.radio import Radio as RadioCog

NAME_LIMIT = 80
TYPE_FILTERS = {"f:genre": KIND_GENRE, "f:artists": KIND_ARTISTS, "f:playlists": KIND_PLAYLISTS}
QUALITY_GOOD = "good"  # a estação personalizada não tem taxa nem teste: o ponto é sempre o de "boa"


def filtered(stations: list[CustomStation], filter_id: str | None) -> list[CustomStation]:
    """As estações do filtro (o tipo); filtro desconhecido ou ausente = todas."""
    kind = TYPE_FILTERS.get(filter_id or "")
    return [s for s in stations if kind is None or s.kind == kind]


async def custom_snapshot(hub: Any, radio: Radio, ports: Any, runner: Any, *, locale: str, tr: Any = None) -> Any:
    """O retrato da rádio PERSONALIZADA ``radio``. ``tr`` (se dado) rotula o tipo do filtro em ``filter_path``."""
    from empiremuse.utils.radio.snapshot import RadioPanelSnapshot

    station = ports.station
    listed = filtered(await hub.stations.list_for_guild(radio.guild_id), radio.filter)
    active = station.id if station is not None else ""
    position, window = _window([s.id for s in listed], active)
    by_id = {s.id: s for s in listed}
    index = {s.id: i + 1 for i, s in enumerate(listed)}
    options = tuple(StationOption(index[i], i, by_id[i].name, 0, "", QUALITY_GOOD) for i in window)
    kind = TYPE_FILTERS.get(radio.filter or "")
    label = tr(f"radio.station.kind.{kind}") if kind and tr is not None else ""
    current = ports.current
    playing = current is not None and not ports.no_tracks
    view = ports._view
    state = STATE_OFFLINE if ports.no_tracks else (STATE_MUTED if getattr(view, "muted", False) else STATE_LIVE)
    since = runner.on_air_since if runner is not None else None
    return RadioPanelSnapshot(
        radio_id=radio.id,
        skin=radio.skin,
        station_name=station.name if station else "—",
        country="",
        codec="",
        bitrate_kbps=0,
        logo=None,
        quality=QUALITY_GOOD,
        now_title=current.title if playing and current.title else None,
        now_artist=current.artist if playing and current.artist else None,
        state=state,
        reserve_number=None,
        volume=ports.volume,
        started_at=datetime.fromtimestamp(since, tz=timezone.utc) if since else None,
        on_air_minutes=max(0, math.floor((ports.wall() - since) / 60)) if since else 0,
        filter_path=(label,) if label else (),
        list_kind=LIST_STATIONS,
        position=position,
        total=len(listed),
        stations=options,
        current_index=next((i for i, o in enumerate(options) if o.id == active), None),
        favorite=False,
        favorites_full=False,
        retry_seconds=runner.retry_seconds() if runner is not None else None,
    )


async def _change(cog: RadioCog, interaction: Any, tr: Any, radio: Any, station: CustomStation) -> str:
    if cog._too_soon(radio.id, "station", _gap()):
        return tr("radio.panel.wait")
    await actions.change_custom_station(
        cog.hub, radio_id=radio.id, station_id=station.id, actor=cog._authorized(interaction)
    )
    return tr("radio.local.changed", station=external_text(station.name, NAME_LIMIT))


def _gap() -> float:
    from empiremuse.cogs.radio import STATION_GAP_S  # tarde: o cog importa este módulo

    return STATION_GAP_S


async def _listed(cog: RadioCog, radio: Radio) -> list[CustomStation]:
    return filtered(await cog.hub.stations.list_for_guild(radio.guild_id), radio.filter)


async def walk(cog: RadioCog, interaction: Any, tr: Any, radio: Radio, step: int) -> str:
    """Anterior e próxima: a vizinha na lista do filtro (dá a volta)."""
    listed = await _listed(cog, radio)
    neighbor = station_neighbor([s.id for s in listed], radio.source.get("station"), step)
    if neighbor is None:
        return tr("radio.panel.no_other_station")
    return await _change(cog, interaction, tr, radio, next(s for s in listed if s.id == neighbor))


async def random_station(cog: RadioCog, interaction: Any, tr: Any, radio: Radio) -> str:
    """Uma estação aleatória da lista do filtro, diferente da atual."""
    current = radio.source.get("station")
    options = [s for s in await _listed(cog, radio) if s.id != current]
    if not options:
        return tr("radio.panel.no_other_station")
    return await _change(cog, interaction, tr, radio, random.choice(options))


async def pick_station(cog: RadioCog, interaction: Any, tr: Any, radio: Radio, station_id: object) -> str:
    """Select 1: só vale uma estação que está na lista do filtro (o valor veio do cliente)."""
    chosen = next((s for s in await _listed(cog, radio) if s.id == station_id), None)
    if chosen is None:
        raise GenericError(tr("radio.panel.not_here"))
    return await _change(cog, interaction, tr, radio, chosen)


async def pick_filter(cog: RadioCog, interaction: Any, tr: Any, radio: Radio, value: object) -> str:
    """Select 2: grava o tipo como filtro da rádio SEM trocar a estação no ar. Tipo sem estações no servidor é recusado com o motivo."""
    kind = TYPE_FILTERS.get(value) if isinstance(value, str) else None
    if kind is None:
        raise GenericError(tr("radio.panel.not_here"))
    stations = await cog.hub.stations.list_for_guild(radio.guild_id)
    total = sum(1 for s in stations if s.kind == kind)
    if not total:
        return tr("radio.local.no_results")
    fresh = await cog.hub.store.get(radio.id)
    if fresh is None:
        raise GenericError(tr("radio.panel.gone"))
    await cog.hub.store.set_filter(radio.id, value, expected_rev=fresh.rev, updated_by=str(interaction.author.id))
    return tr("radio.panel.filter_applied", total=total)
