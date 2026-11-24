"""O adaptador do Components V2 do player (T-C-BUILD, Alicerce §2): "V2 fica V2".

A flag ``IS_COMPONENTS_V2`` só se liga, nunca se desliga: uma mensagem V2 recusa ``content`` e ``embeds`` (400). Todo lugar que
edita a mensagem do player, e que ainda monta um payload de embed (ocioso, painel em lista, encerramento, arquivada), passa por
aqui: ``adapt_for(message, kwargs)`` devolve os mesmos ``kwargs`` numa mensagem V1 e a conversão (``legacy_to_v2``) numa V2.

Sem I/O. Texto de fora nunca é reescrito aqui: o embed já traz o texto escapado pelo montador dele.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

import disnake

from empiremuse.rules.usage import escape_markdown

logger = logging.getLogger(__name__)

MAX_COMPONENTS = 40
MAX_TEXT = 4000  # a soma dos TextDisplay de uma mensagem V2
ROW_BUTTONS = 5
TEXT_BLOCKS = 10  # blocos de texto por embed convertido, até o teto de componentes pedir menos
LEGACY_KEYS = ("content", "embed", "embeds", "view")
_ELLIPSIS = "…"


def is_v2(message: Any) -> bool:
    """A mensagem tem a flag V2? Uma ``PartialMessage`` não tem ``flags``: conta como V1 (``False``)."""
    return bool(getattr(getattr(message, "flags", None), "is_components_v2", False))


def has_legacy_fields(kwargs: Mapping[str, Any]) -> bool:
    """O payload leva algo que uma mensagem V2 recusa: texto, embed (de verdade, não vazio) ou ``view``."""
    if kwargs.get("content") or kwargs.get("embed") or kwargs.get("embeds"):
        return True
    return kwargs.get("view") is not None


def _clip(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + _ELLIPSIS


def _embed_texts(embed: disnake.Embed) -> list[str]:
    """Os blocos de texto do embed na ordem em que ele os mostra (autor, título, descrição, campos, rodapé)."""
    texts: list[str] = []
    if embed.author and embed.author.name:
        texts.append(
            f"-# {escape_markdown(embed.author.name)}"
        )  # no embed o autor é texto puro: no ``-#`` não vira markdown
    if embed.title:
        texts.append(f"## [{embed.title}]({embed.url})" if embed.url else f"## {embed.title}")
    if embed.description:
        texts.append(str(embed.description))
    texts.extend(f"**{field.name}**\n{field.value}" for field in embed.fields)
    if embed.footer and embed.footer.text:
        texts.append(f"-# {escape_markdown(embed.footer.text)}")
    return texts


def _embed_children(embed: disnake.Embed, budget: int, blocks: int = TEXT_BLOCKS) -> tuple[list[Any], int]:
    """Os filhos do ``Container`` de um embed e o que sobra do teto de texto. A miniatura vira o acessório da 1ª seção."""
    used = 0
    kept: list[str] = []
    for text in _embed_texts(embed):
        text = _clip(text, budget - used)
        if text:
            kept.append(text)
            used += len(text)
    if len(kept) > blocks:  # os blocos que passam do limite viram um só (o Discord aceita 40 componentes na mensagem)
        merged = _clip("\n\n".join(kept[blocks - 1 :]), budget - sum(len(t) for t in kept[: blocks - 1]))
        used -= sum(len(t) for t in kept[blocks - 1 :]) - len(merged)
        kept = [*kept[: blocks - 1], merged]
    children: list[Any] = []
    thumb = embed.thumbnail.url if embed.thumbnail else None
    if kept and thumb:
        children.append(disnake.ui.Section(kept[0], accessory=disnake.ui.Thumbnail(media=thumb)))
        children.extend(disnake.ui.TextDisplay(text) for text in kept[1:])
    else:
        children.extend(disnake.ui.TextDisplay(text) for text in kept)
    if embed.image and embed.image.url:
        children.append(disnake.ui.MediaGallery(disnake.MediaGalleryItem(media=embed.image.url)))
    return children, budget - used


def embed_children(
    embed: disnake.Embed, budget: int = MAX_TEXT, blocks: int = TEXT_BLOCKS
) -> tuple[list[Any], disnake.Colour | None, int]:
    """Os filhos do ``Container`` de um embed, a cor dele e o que sobrou do teto de texto."""
    children, left = _embed_children(embed, budget, blocks)
    colour = embed.colour if isinstance(embed.colour, disnake.Colour) else None
    return children, colour, left


def count_components(item: Any) -> int:
    """Quantos componentes o Discord conta nesta árvore (ela mesma, os filhos e o acessório)."""
    children = list(getattr(item, "children", None) or ())
    accessory = getattr(item, "accessory", None)
    return 1 + sum(count_components(child) for child in children) + (count_components(accessory) if accessory else 0)


def disable_all(item: Any) -> None:
    """Desativa botões e selects da árvore (o ``Container`` V2 não propaga ``disabled`` aos filhos). Nó sem o campo é pulado."""
    for child in list(getattr(item, "children", None) or ()):
        disable_all(child)
    if (accessory := getattr(item, "accessory", None)) is not None:
        disable_all(accessory)
    try:
        item.disabled = True
    except AttributeError:
        pass


def _is_layout(item: Any) -> bool:
    return isinstance(item, disnake.ui.ActionRow) or not isinstance(item, disnake.ui.WrappedComponent)


def components_to_rows(items: Sequence[Any] | None) -> list[Any]:
    """Botões e menus soltos (a lista de componentes de uma mensagem V1) em ``ActionRow``: 5 botões por linha, um menu por linha.
    O que já é layout (``ActionRow``, ``Container``...) passa como está."""
    rows: list[Any] = []
    buttons: list[Any] = []

    def flush() -> None:
        if buttons:
            rows.append(disnake.ui.ActionRow(*buttons))
            buttons.clear()

    for item in items or ():
        if _is_layout(item):
            flush()
            rows.append(item)
        elif isinstance(item, disnake.ui.Button):
            buttons.append(item)
            if len(buttons) == ROW_BUTTONS:
                flush()
        else:
            flush()
            rows.append(disnake.ui.ActionRow(item))
    flush()
    return rows


def legacy_to_v2(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """O mesmo pedido de edição/envio em V2: ``content`` e embeds viram texto e ``Container``, a ``view`` e os componentes
    soltos viram ``ActionRow``. Sempre ``content=None`` e ``embeds=[]`` (zerar os dois é o que converte uma V1 por edição)."""
    out = {name: value for name, value in kwargs.items() if name not in (*LEGACY_KEYS, "components")}
    layout: list[Any] = []
    budget = MAX_TEXT
    if content := kwargs.get("content"):
        text = _clip(str(content), budget)
        layout.append(disnake.ui.TextDisplay(text))
        budget -= len(text)
    embeds = list(kwargs.get("embeds") or [])
    if kwargs.get("embed") is not None:
        embeds.insert(0, kwargs["embed"])
    controls = components_to_rows(kwargs.get("components"))
    view = kwargs.get("view")
    if view is not None and getattr(view, "children", None):
        controls.extend(components_to_rows(list(view.children)))
    head = list(layout)
    for blocks in (TEXT_BLOCKS, 4, 1):  # menos blocos por embed até caber nos 40 componentes
        layout = list(head)
        left = budget
        parts: list[tuple[list[Any], disnake.Colour | None]] = []
        for embed in embeds:
            children, colour, left = embed_children(embed, left, blocks)
            parts.append((children, colour))
        if not parts:
            layout.extend(controls)
        else:
            for index, (children, colour) in enumerate(parts):
                last = index == len(parts) - 1
                layout.append(disnake.ui.Container(*children, *(controls if last else ()), accent_colour=colour))
        if sum(count_components(item) for item in layout) <= MAX_COMPONENTS:
            break
    out.update(content=None, embeds=[], components=layout)
    return out


def adapt_for(message: Any, kwargs: Mapping[str, Any], *, where: str = "", expected: bool = False) -> dict[str, Any]:
    """Os ``kwargs`` certos para editar ``message``: iguais numa V1; convertidos numa V2 que recebeu um payload de embed.
    ``expected`` = o caminho foi feito para converter (painel em lista, ocioso, encerramento): converte sem aviso. Sem ele, o
    aviso é a rede de segurança de um caminho que esqueceu o payload V2."""
    if is_v2(message) and has_legacy_fields(kwargs):
        if not expected:
            logger.warning("Mensagem V2 recebeu um payload de embed (%s): convertido", where or "?")
        return legacy_to_v2(kwargs)
    return dict(kwargs)


async def edit_message(message: Any, *, where: str = "", expected: bool = False, **kwargs: Any) -> Any:
    """``message.edit`` que nunca manda embed numa mensagem V2."""
    return await message.edit(**adapt_for(message, kwargs, where=where, expected=expected))


async def edit_from_interaction(interaction: Any, *, where: str = "", expected: bool = False, **kwargs: Any) -> Any:
    """``interaction.response.edit_message`` (ou ``message.edit`` se a interação já foi respondida), com a mesma regra."""
    message = getattr(interaction, "message", None)
    data = adapt_for(message, kwargs, where=where, expected=expected)
    if interaction.response.is_done():
        return await message.edit(**data)
    return await interaction.response.edit_message(**data)
