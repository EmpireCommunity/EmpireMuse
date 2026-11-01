"""Página Rádios · filtro de local em cascata (item 8, contrato R4 §5): região → país → estado → cidade → estações.

A árvore é fixa e vem de ``pool.radio.locations`` (Turbine, ``LocationTree``; ``None`` quando o arquivo não carregou: o
filtro some e a busca por nome da R3 continua). Cada nível tem no máximo 24 opções já com rótulo no idioma do painel; o
``node`` que o usuário manda só vale se ``tree.node`` o conhece (id desconhecido, longo ou de outro tipo → ``None``). Só
o nó final (cidade, ou país pequeno) vai ao diretório, por ``directory.region(node, key=guild_id)``, e a página fatia o
resultado em 24 por página. A principal da rádio fica fora da lista, como na busca por nome.
"""

from __future__ import annotations

from typing import Any, NamedTuple, Optional

from empiremuse.rules.i18n import Translator

NODE_FIELD = "node"
PAGE_FIELD = "pagina"
NODE_LIMIT = 100  # ids da árvore têm menos de 100 caracteres (R4 §5); o resto nem chega a ``tree.node``
PAGE_SIZE = 24
MAX_PAGES = 7  # ``region()`` devolve no máximo 150 estações
LEVEL_KEYS = {None: "pick_region", "region": "pick_country", "country": "pick_state", "state": "pick_city"}


class LocalOption(NamedTuple):
    id: str
    label: str
    description: str
    count: int


class LocalLevel(NamedTuple):
    """Um ``<select>`` da cascata: as opções do pai (``parent``, ``None`` no topo) e a escolhida, se houver."""

    depth: int
    key: str  # sufixo de ``panel.radio.local.*`` com o rótulo do nível
    parent: Optional[str]
    options: tuple[LocalOption, ...]
    selected: Optional[str]


class LocalView(NamedTuple):
    node: Optional[str]
    levels: tuple[LocalLevel, ...]
    final: bool


class Paging(NamedTuple):
    node: str
    page: int
    pages: int


def tree_of(ctx: Any) -> Any:
    """A árvore carregada, ou ``None`` (sem arquivo, ou hub sem o atributo: a página fica sem o filtro)."""
    return getattr(ctx.pool.radio, "locations", None)


def guild_locale_of(ctx: Any) -> Optional[str]:
    """O ``preferred_locale`` do servidor no cache do bot, para o atalho ⭐ do país (R4 §1)."""
    locale = getattr(ctx.guild, "preferred_locale", None)
    return str(locale) if locale else None


def parse_page(raw: Any) -> int:
    text = str(raw or "")
    if text.isdigit() and 1 <= int(text) <= MAX_PAGES:
        return int(text)
    return 1


def resolve_node(tree: Any, raw: Any) -> Any:
    """``tree.node`` revalida tudo o que vem do usuário (R-6); texto acima do limite nem é consultado."""
    if not isinstance(raw, str) or not raw or len(raw) > NODE_LIMIT:
        return None
    return tree.node(raw)


class LocalPage:
    @staticmethod
    def levels(tree: Any, node: Any, locale: str, guild_locale: Optional[str]) -> tuple[LocalLevel, ...]:
        """Os selects do topo até o nível abaixo de ``node`` (nenhum abaixo de um nó final)."""
        path = list(tree.path(node.id)) if node is not None else []
        if node is not None and (not path or path[-1].id != node.id):
            path.append(node)
        levels: list[LocalLevel] = []
        parent_id: Optional[str] = None
        parent_kind: Optional[str] = None
        for depth, chosen in enumerate((*path, None)):
            raw = tree.children(parent_id, locale, guild_locale=guild_locale)
            if not raw:
                break
            options = tuple(
                LocalOption(str(o.id), str(o.label), str(getattr(o, "description", "") or ""), int(o.count))
                for o in list(raw)[:PAGE_SIZE]
            )
            key = LEVEL_KEYS.get(parent_kind, "pick_city")
            levels.append(LocalLevel(depth, key, parent_id, options, str(chosen.id) if chosen is not None else None))
            if chosen is None:
                break
            parent_id, parent_kind = str(chosen.id), str(chosen.kind)
        return tuple(levels)

    def view(self, tree: Any, node: Any, locale: str, guild_locale: Optional[str]) -> LocalView:
        return LocalView(
            str(node.id) if node is not None else None,
            self.levels(tree, node, locale, guild_locale),
            bool(getattr(node, "final", False)) if node is not None else False,
        )

    @staticmethod
    async def stations(ctx: Any, radio: Any, node: Any, page: int, tr: Translator, view_of: Any) -> tuple:
        """``(estações da página, dica, Paging)``; a principal fica de fora e ``partial`` vira aviso, não erro."""
        result = await ctx.pool.radio.directory.region(node, key=ctx.guild_id)  # balde por servidor (R4 §4)
        main = radio.source.get("station")
        health = ctx.pool.radio.health
        views = tuple(view_of(h.station, h.station.uuid, health) for h in result.hits if h.station.uuid != main)
        pages = max(1, -(-len(views) // PAGE_SIZE))
        page = min(page, pages)
        chunk = views[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        hint = None
        if not views:
            hint = tr("panel.radio.local.no_results")
        elif getattr(result, "partial", False):
            hint = tr("panel.radio.local.partial")
        return chunk, hint, Paging(str(node.id), page, pages)
