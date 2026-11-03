"""O retrato de uma rádio ao vivo para o painel (R6): tudo que a tela precisa, imutável, montado de uma vez para a renderização (``visor`` e botões).

O painel NUNCA lê o runner, as portas nem o banco: recebe este retrato. A montagem é aqui (``live_snapshot``) e só olha; nada é gravado.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, NamedTuple, Optional

from empiremuse.rules.radio import Radio
from empiremuse.rules.radio_quality import station_quality
from empiremuse.utils.radio.favorites import MAX_FAVORITES
from empiremuse.utils.radio.panel import split_stream_title
from empiremuse.utils.radio.picker import FAVORITES_ID

WINDOW = 25  # estações do filtro mostradas no select (o limite do Discord)
STATE_LIVE, STATE_MUTED, STATE_OFFLINE = "live", "muted", "offline"
LIST_STATIONS, LIST_FAVORITES = "stations", "favorites"


class StationOption(NamedTuple):
    position: int  # 1-based na lista inteira do filtro
    id: str
    name: str
    bitrate_kbps: int
    codec: str
    quality: str


class RadioPanelSnapshot(NamedTuple):
    radio_id: str
    skin: str
    station_name: str
    country: str
    codec: str
    bitrate_kbps: int
    logo: Optional[bytes]
    quality: str
    now_title: Optional[str]
    now_artist: Optional[str]
    state: str  # live | muted | offline
    reserve_number: Optional[int]  # 1 quando toca a estação reserva
    volume: int  # 10..150 (100 = padrão); o painel escala
    started_at: Optional[datetime]  # desde quando toca (UTC)
    on_air_minutes: int
    filter_path: tuple[
        str, ...
    ]  # nomes, do mais amplo ao mais fino (região › país › estado › cidade); vazio = sem filtro
    list_kind: str  # stations | favorites
    position: int  # da estação no ar na lista inteira (1-based); 0 = fora da lista
    total: int
    stations: tuple[StationOption, ...]  # a janela de até ``WINDOW`` estações em volta da atual
    current_index: Optional[int]  # índice da estação no ar dentro de ``stations``
    favorite: bool  # a estação no ar está nas favoritas do servidor
    favorites_full: bool
    retry_seconds: Optional[int]  # fora do ar: segundos para a próxima tentativa


def _window(uuids: list[str], active: str) -> tuple[int, list[str]]:
    """``(posição 1-based da ativa na lista inteira, a janela de até WINDOW uuids que a contém)``."""
    if active not in uuids:
        return 0, uuids[:WINDOW]
    index = uuids.index(active)
    start = max(0, min(index - WINDOW // 2, len(uuids) - WINDOW))
    return index + 1, uuids[start : start + WINDOW]


def _state(view: Any) -> str:
    if getattr(view, "offline", False):
        return STATE_OFFLINE
    return STATE_MUTED if getattr(view, "muted", False) else STATE_LIVE


async def live_snapshot(
    hub: Any, radio: Radio, ports: Any, runner: Any, *, locale: str, logo: Optional[bytes] = None
) -> RadioPanelSnapshot:
    """Monta o retrato da rádio AO VIVO ``radio`` a partir das portas e do runner dela. Falha do diretório não derruba: o painel mostra o que tem."""
    active = ports.active
    ids = list(ports.station_list)
    position, window_ids = _window(ids, active)
    wanted = list(dict.fromkeys([active, *window_ids])) if active else window_ids
    try:
        known = await hub.directory.by_uuids(wanted)
    except Exception:  # noqa: BLE001 - sem diretório o painel mostra o que sabe (o nome em cache das portas)
        known = {}
    station = known.get(active)
    view = ports._view
    title, artist = split_stream_title(getattr(view, "title", None))
    unavailable = hub.health.unavailable
    quality = station_quality(getattr(station, "bitrate", 0), check_ok=None, unavailable=unavailable(active))
    options = tuple(
        StationOption(
            (ids.index(u) + 1) if u in ids else 0,
            u,
            known[u].name if u in known else "—",
            known[u].bitrate if u in known else 0,
            known[u].codec if u in known else "",
            station_quality(known[u].bitrate if u in known else 0, check_ok=None, unavailable=unavailable(u)),
        )
        for u in window_ids
    )
    favorites = [f.uuid for f in await hub.favorites.list(radio.guild_id)]
    tree = hub.locations
    path: tuple[str, ...] = ()
    if radio.filter and radio.filter != FAVORITES_ID and tree is not None:
        path = tuple(tree.label(node, locale) for node in tree.path(radio.filter))
    since = runner.on_air_since if runner is not None else None
    started = datetime.fromtimestamp(since, tz=timezone.utc) if since else None
    wall = ports.wall()
    return RadioPanelSnapshot(
        radio_id=radio.id,
        skin=radio.skin,
        station_name=station.name if station else ports._names.get(active, "—"),
        country=station.country if station else "",
        codec=station.codec if station else "",
        bitrate_kbps=station.bitrate if station else 0,
        logo=logo,
        quality=quality,
        now_title=title or None,
        now_artist=artist,
        state=_state(view),
        reserve_number=1 if ports.on_reserve() else None,
        volume=ports.volume,
        started_at=started,
        on_air_minutes=max(0, math.floor((wall - since) / 60)) if since else 0,
        filter_path=path,
        list_kind=LIST_FAVORITES if radio.filter == FAVORITES_ID else LIST_STATIONS,
        position=position,
        total=len(ids),
        stations=options,
        current_index=next((i for i, o in enumerate(options) if o.id == active), None),
        favorite=active in favorites,
        favorites_full=len(favorites) >= MAX_FAVORITES,
        retry_seconds=runner.retry_seconds() if runner is not None else None,
    )
