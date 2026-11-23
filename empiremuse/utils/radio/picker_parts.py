"""Peças do seletor de estação (``picker.py``) da J2a: o modal de busca por nome (A3) e as listas pessoais do autor (B1).

Nada aqui decide quem pode o quê: o modal só entrega o termo ao seletor, que confere o autor e o balde do servidor; as listas pessoais vêm
SEMPRE do ``author_id`` do seletor (a interação que o abriu), nunca de ``custom_id`` nem de valor de select [m7].
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Optional

import disnake

from empiremuse.rules.i18n import Translator, fit
from empiremuse.utils.radio.premium import RadioUnavailable

if TYPE_CHECKING:
    from empiremuse.utils.radio import RadioHub
    from empiremuse.utils.radio.favorites import FavoriteStation
    from empiremuse.utils.radio.locations import LocationTree

logger = logging.getLogger(__name__)

SEARCH_MIN = 2
SEARCH_MAX = 64  # a regra de ``directory.search`` (``check_search_term``)
SEARCH_FIELD = "term"

OnSearch = Callable[[disnake.ModalInteraction, str], Awaitable[None]]


class SearchModal(disnake.ui.Modal):
    """ "🔎 Buscar por nome": um campo, 2 a 64 caracteres. O ``custom_id`` é aleatório por abertura (nada do estado vai nele)."""

    def __init__(self, tr: Translator, on_search: OnSearch) -> None:
        self._on_search = on_search
        super().__init__(
            title=fit(tr("jukebox.radio.search.title"), 45),
            custom_id=f"radio_picker_search:{secrets.token_hex(4)}",
            components=[
                disnake.ui.TextInput(
                    label=fit(tr("jukebox.radio.search.field"), 45),
                    custom_id=SEARCH_FIELD,
                    min_length=SEARCH_MIN,
                    max_length=SEARCH_MAX,
                    required=True,
                )
            ],
        )

    async def callback(self, interaction: disnake.ModalInteraction, /) -> None:
        values = getattr(interaction, "text_values", None) or {}
        await self._on_search(interaction, str(values.get(SEARCH_FIELD, "")).strip())


async def load_mine(hub: RadioHub, author_id: int) -> list[FavoriteStation]:
    """As MINHAS favoritas ao vivo; banco fora (ou qualquer falha) = lista vazia, e a entrada some do topo."""
    store = getattr(hub, "user_favorites", None)
    if store is None:
        return []
    try:
        return list(await store.stations(author_id))
    except RadioUnavailable:
        return []
    except Exception as error:  # noqa: BLE001 - a tela segue sem a lista; só o tipo vai ao log (m7)
        logger.warning("Favoritas pessoais não lidas no seletor (%s)", type(error).__name__)
        return []


async def load_filters(hub: RadioHub, tree: Optional[LocationTree], author_id: int) -> list[str]:
    """Os MEUS filtros (ids de nó); nó que a árvore não conhece sai na leitura. Sem árvore, não há filtros."""
    store = getattr(hub, "user_favorites", None)
    if store is None or tree is None:
        return []
    try:
        nodes = await store.filters(author_id)
    except RadioUnavailable:
        return []
    except Exception as error:  # noqa: BLE001 - idem
        logger.warning("Filtros pessoais não lidos no seletor (%s)", type(error).__name__)
        return []
    return [node for node in nodes if tree.node(node) is not None]


def filter_label(tree: LocationTree, node_id: str, locale: str) -> str:
    """O caminho do nó no idioma do seletor ("Brasil › Goiás › Goiânia"); o documento só guarda o id (Q8)."""
    return " › ".join(tree.label(node, locale) for node in tree.path(node_id)) or node_id


def manages(interaction: Any) -> bool:
    """O autor tem Gerenciar Servidor (ou administrador) AGORA? Só decide se a estrela do servidor aparece: quem grava confere de novo."""
    perms = getattr(getattr(interaction, "author", None), "guild_permissions", None)
    return bool(perms is not None and (perms.manage_guild or perms.administrator))
