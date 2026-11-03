"""Os cliques novos do painel ao vivo (R6): reconectar, estação aleatória e os dois selects (estação do filtro e troca de filtro).

Cada função recebe o cog (para ``hub``, o intervalo entre trocas e o ``Actor`` do painel), a rádio JÁ conferida por ``_panel_radio`` (o clique é de quem
está na call) e devolve o texto da resposta efêmera. O valor de um select NUNCA é confiado: a estação tem de estar na lista da rádio, o filtro tem de
existir na árvore (ou ser as favoritas).
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any

from empiremuse.rules.radio import LIST_MAX
from empiremuse.utils.music.errors import GenericError
from empiremuse.rules.text import external_text
from empiremuse.utils.radio import actions
from empiremuse.utils.radio.picker import FAVORITES_ID
from empiremuse.utils.radio.premium import RadioUnavailable

if TYPE_CHECKING:
    from empiremuse.cogs.radio import Radio as RadioCog

NAME_LIMIT = 80


async def reconnect(cog: RadioCog, tr: Any, runner: Any) -> str:
    """Fora do ar e esperando o recuo: tenta já. Tocando, o botão não faz nada além de dizer isso."""
    return tr("radio.panel.reconnecting" if runner.reconnect_now() else "radio.panel.reconnect_not_needed")


async def _change(cog: RadioCog, interaction: Any, tr: Any, radio: Any, uuid: str) -> str:
    if cog._too_soon(radio.id, "station", cog_gap()):
        return tr("radio.panel.wait")
    station = await cog.hub.directory.by_uuid(uuid)
    if station is None:
        return tr("radio.live.not_found")
    await actions.change_station(cog.hub, radio_id=radio.id, station=station, actor=cog._authorized(interaction))
    return tr("radio.local.changed", station=external_text(station.name, NAME_LIMIT))


def cog_gap() -> float:
    from empiremuse.cogs.radio import STATION_GAP_S  # tarde: o cog importa este módulo

    return STATION_GAP_S


async def random_station(cog: RadioCog, interaction: Any, tr: Any, radio: Any) -> str:
    """Uma estação aleatória da lista do filtro, diferente da atual e que a nossa saúde não marcou indisponível."""
    current = radio.source.get("station")
    options = [u for u in radio.source.get("list") or () if u != current and not cog.hub.health.unavailable(u)]
    if not options:
        return tr("radio.panel.no_other_station")
    return await _change(cog, interaction, tr, radio, random.choice(options))


async def pick_station(cog: RadioCog, interaction: Any, tr: Any, radio: Any, uuid: object) -> str:
    """O select de estações: só vale uma que está na lista da rádio (o valor veio do cliente)."""
    listed = radio.source.get("list") or ()
    if not isinstance(uuid, str) or uuid not in listed:
        raise GenericError(tr("radio.panel.not_here"))
    return await _change(cog, interaction, tr, radio, uuid)


async def pick_filter(cog: RadioCog, interaction: Any, tr: Any, radio: Any, node_id: object) -> str:
    """O select de filtro: grava o filtro e a lista de estações dele (ou as favoritas) SEM trocar a estação no ar. Lembrado por rádio."""
    hub = cog.hub
    if not isinstance(node_id, str):
        raise GenericError(tr("radio.panel.not_here"))
    if node_id == FAVORITES_ID:
        uuids = [f.uuid for f in await hub.favorites.list(radio.guild_id)]
    else:
        tree = hub.locations
        node = tree.node(node_id) if tree is not None else None
        if node is None:
            raise GenericError(tr("radio.local.expired"))
        try:
            result = await hub.directory.region(node, key=radio.guild_id)
        except RadioUnavailable:
            raise GenericError(tr("radio.owner.unavailable")) from None
        ranked = sorted(result.hits, key=lambda hit: hit.clickcount, reverse=True)
        uuids = [hit.station.uuid for hit in ranked if not hub.health.unavailable(hit.station.uuid)]
    if not uuids:
        return tr("radio.local.no_results")
    fresh = await hub.store.get(radio.id)
    if fresh is None:
        raise GenericError(tr("radio.panel.gone"))
    by = str(interaction.author.id)
    if not fresh.source.get(
        "station"
    ):  # recém-configurada (em espera, sem estação): o filtro dá a primeira, a mais ouvida que não está indisponível
        first = next((u for u in uuids if not hub.health.unavailable(u)), None)
        station = await hub.directory.by_uuid(first) if first else None
        if station is None:
            return tr("radio.local.no_results")
        saved = await actions.change_station(
            hub, radio_id=radio.id, station=station, actor=cog._authorized(interaction), station_list=uuids[:LIST_MAX]
        )
    else:
        saved = await hub.store.set_list(radio.id, uuids[:LIST_MAX], expected_rev=fresh.rev, updated_by=by)
    await hub.store.set_filter(radio.id, node_id, expected_rev=saved.rev, updated_by=by)
    sync = getattr(getattr(hub.manager.runner(radio.id), "ports", None), "sync_list", None)
    if sync is not None:
        sync(uuids[:LIST_MAX])  # as portas guardam a lista antiga: o seletor e o clique validam pela nova
    return tr("radio.panel.filter_applied", total=min(len(uuids), LIST_MAX))
