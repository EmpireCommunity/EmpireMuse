"""Rótulo das entradas ``custom``/``genre`` das favoritas nos menus do Discord (J2a).

O documento só guarda o id; o nome vem agora (estação do servidor) ou do i18n (gênero). Estação apagada segue listada
com um rótulo neutro e ``missing``, para poder ser removida. Todo nome de estação sai pela função única de texto externo.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from empiremuse.rules.radio_station import GENRE_PREFIX, CustomStation, is_genre
from empiremuse.rules.text import external_text
from empiremuse.utils.radio.stations import StationStore

__all__ = ["GENRE_PREFIX", "LABEL_LIMIT", "RefLabel", "is_genre", "ref_labels", "resolve_refs"]

LABEL_LIMIT = 100


class RefLabel(NamedTuple):
    """Uma entrada do menu: o id guardado, o rótulo sem markdown (pronto para o select) e se a estação já não resolve."""

    ref: str
    label: str
    missing: bool = False


async def resolve_refs(hub: Any, guild_id: int, refs: list[str]) -> list[tuple[str, CustomStation | None]]:
    """Cada id com a estação de agora: as ``custom`` numa leitura só das estações do servidor (nunca uma por favorita); o gênero vem do
    catálogo, sem banco. A de outro servidor não está nessa leitura e sai ``None``, igual a uma apagada."""
    custom: dict[str, CustomStation] = {}
    if any(not is_genre(ref) for ref in refs):
        custom = {str(item.id): item for item in await hub.stations.list_for_guild(guild_id)}
    return [(ref, StationStore.builtin(guild_id, ref) if is_genre(ref) else custom.get(ref)) for ref in refs]


def _label(tr: Any, ref: str, station: CustomStation | None) -> RefLabel:
    """O gênero pelo idioma, a ``custom`` pelo nome atual e a que sumiu (``missing``) por "removida"."""
    if station is None:
        return RefLabel(ref, tr("radio.favorites.ref.missing"), missing=True)
    name = tr(f"radio.genre.{ref[len(GENRE_PREFIX) :]}") if is_genre(ref) else station.name
    return RefLabel(ref, external_text(name, LABEL_LIMIT, markdown=False) or "—")


async def ref_labels(hub: Any, tr: Any, guild_id: int, refs: list[str]) -> list[RefLabel]:
    return [_label(tr, ref, station) for ref, station in await resolve_refs(hub, guild_id, refs)]
