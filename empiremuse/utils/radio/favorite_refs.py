"""Rótulo das entradas ``custom``/``genre`` das favoritas nos menus do Discord (J2a).

O documento só guarda o id; o nome vem agora (estação do servidor) ou do i18n (gênero). Estação apagada segue listada
com um rótulo neutro, para poder ser removida. Todo nome de estação sai pela função única de texto externo.
"""

from __future__ import annotations

from typing import Any

from empiremuse.rules.text import external_text

LABEL_LIMIT = 100
GENRE_PREFIX = "g:"


def is_genre(ref: str) -> bool:
    return ref.startswith(GENRE_PREFIX)


async def ref_label(hub: Any, tr: Any, guild_id: int, ref: str) -> str:
    """O rótulo de uma entrada: o gênero pelo idioma, a ``custom`` pelo nome atual e a que sumiu por "removida"."""
    if is_genre(ref):
        return external_text(tr(f"radio.genre.{ref[len(GENRE_PREFIX) :]}"), LABEL_LIMIT, markdown=False) or "—"
    station = await hub.stations.get(guild_id, ref)
    if station is None:
        return tr("radio.favorites.ref.missing")
    return external_text(station.name, LABEL_LIMIT, markdown=False) or "—"


async def ref_labels(hub: Any, tr: Any, guild_id: int, refs: list[str]) -> list[tuple[str, str]]:
    return [(ref, await ref_label(hub, tr, guild_id, ref)) for ref in refs]
