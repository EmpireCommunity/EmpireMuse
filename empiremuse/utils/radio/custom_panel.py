"""O painel da rádio PERSONALIZADA no desenho do visor: o retrato (mesmo ``RadioPanelSnapshot`` da ao vivo) e os cliques dos dois selects.

Select 1 = as estações do servidor do filtro aplicado; select 2 = filtro por TIPO da estação (``f:genre``, ``f:artists``, ``f:playlists``). O filtro
fica gravado em ``Radio.filter`` (lembrado por rádio); sem filtro (``f:all`` o esquece) = todas as estações do servidor. "Favoritas" ainda não existe aqui (decisão do dono pendente).
O valor de um select NUNCA é confiado: a estação tem de ser do servidor e estar no filtro; o filtro tem de ser um dos tipos.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timezone
from collections.abc import Collection
from typing import TYPE_CHECKING, Any

from empiremuse.rules.radio import VOLUME_DEFAULT, Radio, station_neighbor
from empiremuse.rules.radio_station import KIND_ARTISTS, KIND_GENRE, KIND_PLAYLISTS, CustomStation
from empiremuse.rules.text import external_text
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.radio import actions
from empiremuse.utils.radio.custom_favorites import MAX_CUSTOM_FAVORITES
from empiremuse.utils.radio.snapshot import (
    LIST_FAVORITES,
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
ALL_FILTER = "f:all"  # 'Todas as estações': esquece o filtro
TYPE_FILTERS = {"f:genre": KIND_GENRE, "f:artists": KIND_ARTISTS, "f:playlists": KIND_PLAYLISTS}
QUALITY_GOOD = "good"  # a estação personalizada não tem taxa nem teste: o ponto é sempre o de "boa"


FAVORITES_VALUE = (
    "f:fav"  # o valor do select 2; gravado em ``Radio.filter`` como ``f:fav:<membro>`` (a lista é a de QUEM escolheu)
)


def favorites_owner(filter_id: str | None) -> int | None:
    """O membro dono da lista de favoritas aplicada (``f:fav:<id>``); ``None`` se o filtro não é o de favoritas."""
    prefix = FAVORITES_VALUE + ":"
    if isinstance(filter_id, str) and filter_id.startswith(prefix) and filter_id[len(prefix) :].isdecimal():
        return int(filter_id[len(prefix) :])
    return None


def filtered(
    stations: list[CustomStation], filter_id: str | None, favorite_ids: Collection[str] = ()
) -> list[CustomStation]:
    """As estações do filtro: o tipo, ou as favoritas do membro (``favorite_ids``, na ordem em que ele favoritou); desconhecido ou ausente = todas."""
    if favorites_owner(filter_id) is not None:
        by_id = {s.id: s for s in stations}
        return [by_id[i] for i in favorite_ids if i in by_id]
    kind = TYPE_FILTERS.get(filter_id or "")
    return [s for s in stations if kind is None or s.kind == kind]


async def listed(hub: Any, radio: Radio) -> list[CustomStation]:
    """A lista de estações do filtro aplicado da rádio (lê as favoritas do membro só quando o filtro é o delas)."""
    owner = favorites_owner(radio.filter)
    ids = await hub.custom_favorites.list(radio.guild_id, owner) if owner is not None else ()
    return filtered(await hub.stations.list_for_guild(radio.guild_id), radio.filter, ids)


async def custom_snapshot(hub: Any, radio: Radio, ports: Any, runner: Any, *, locale: str, tr: Any = None) -> Any:
    """O retrato da rádio PERSONALIZADA ``radio``. ``tr`` (se dado) rotula o tipo do filtro em ``filter_path``."""
    from empiremuse.utils.radio.snapshot import RadioPanelSnapshot

    station = ports.station
    owner = favorites_owner(radio.filter)
    favorite_ids = await hub.custom_favorites.list(radio.guild_id, owner) if owner is not None else []
    listed = filtered(await hub.stations.list_for_guild(radio.guild_id), radio.filter, favorite_ids)
    active = station.id if station is not None else ""
    position, window = _window([s.id for s in listed], active)
    by_id = {s.id: s for s in listed}
    index = {s.id: i + 1 for i, s in enumerate(listed)}
    options = tuple(StationOption(index[i], i, by_id[i].name, 0, "", QUALITY_GOOD) for i in window)
    kind = TYPE_FILTERS.get(radio.filter or "")
    label = ""
    if tr is not None:
        label = tr(f"radio.station.kind.{kind}") if kind else (tr("radio.panel.custom.favorites") if owner else "")
    current = ports.current
    playing = current is not None and not ports.no_tracks
    muted = bool(
        getattr(runner, "admin_paused", False)
    )  # a personalizada não tem ``_view`` nem volume nas portas: o estado vem do runner
    state = STATE_OFFLINE if ports.no_tracks else (STATE_MUTED if muted else STATE_LIVE)
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
        volume=radio.source.get("volume", VOLUME_DEFAULT),
        started_at=datetime.fromtimestamp(since, tz=timezone.utc) if since else None,
        on_air_minutes=max(0, math.floor((ports.wall() - since) / 60)) if since else 0,
        filter_path=(label,) if label else (),
        list_kind=LIST_FAVORITES if owner is not None else LIST_STATIONS,
        position=position,
        total=len(listed),
        stations=options,
        current_index=next((i for i, o in enumerate(options) if o.id == active), None),
        favorite=owner is not None,  # numa lista de favoritas, a estação no ar é favorita dela
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
    return await listed(cog.hub, radio)


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
    if kind is None and value not in (ALL_FILTER, FAVORITES_VALUE):
        raise GenericError(tr("radio.panel.not_here"))
    stations = await cog.hub.stations.list_for_guild(radio.guild_id)
    wanted: str | None = None if value == ALL_FILTER else value
    if (
        value == FAVORITES_VALUE
    ):  # a lista é a de quem escolheu: o painel é um só, então a lista muda para todos na call
        wanted = f"{FAVORITES_VALUE}:{interaction.author.id}"
        mine = set(await cog.hub.custom_favorites.list(radio.guild_id, interaction.author.id))
        total = sum(1 for s in stations if s.id in mine)
        if not total:
            return tr("radio.panel.favorites_empty")
    else:
        total = sum(1 for s in stations if kind is None or s.kind == kind)
    if not total:
        return tr("radio.local.no_results")
    fresh = await cog.hub.store.get(radio.id)
    if fresh is None:
        raise GenericError(tr("radio.panel.gone"))
    await cog.hub.store.set_filter(radio.id, wanted, expected_rev=fresh.rev, updated_by=str(interaction.author.id))
    return tr("radio.panel.filter_applied", total=total)


async def favorite(cog: RadioCog, interaction: Any, tr: Any, radio: Radio) -> str:
    """A estrela: liga/desliga a estação NO AR nas favoritas de quem clicou (por membro, nunca do servidor)."""
    station_id = radio.source.get("station")
    stations = await cog.hub.stations.list_for_guild(radio.guild_id)
    station = next((s for s in stations if s.id == station_id), None)
    if station is None:
        raise GenericError(tr("radio.panel.no_other_station"))
    result = await cog.hub.custom_favorites.toggle(radio.guild_id, interaction.author.id, station.id)
    return tr(
        f"radio.panel.custom.favorite_{result.value}",
        station=external_text(station.name, NAME_LIMIT),
        max=MAX_CUSTOM_FAVORITES,
    )
