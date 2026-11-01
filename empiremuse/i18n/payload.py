# -*- coding: utf-8 -*-
"""Rede de segurança de uma mensagem do player (item 21, I-6e): corta, DEPOIS da expansão, o que passa dos limites do Discord.

Uma tradução cabe no teto do validador (que depende do texto da referência), mas a mensagem final junta muitos textos: o embed soma
título, descrição, campos, rodapé e autor contra os 6000 e cada componente tem o seu limite. ``fit_payload`` corta no MEIO (``fit``),
sem partir menção ou emoji, e nunca levanta: o que não for reconhecido fica como está.
"""

from __future__ import annotations

from typing import Any

from empiremuse.rules.i18n import fit, utf16_length

EMBED_TOTAL = 6000
EMBED_TITLE = 256
EMBED_DESCRIPTION = 4096
EMBED_FIELD_NAME = 256
EMBED_FIELD_VALUE = 1024
EMBED_FOOTER = 2048
EMBED_AUTHOR = 256
CONTENT = 2000
BUTTON_LABEL = 80
SELECT_PLACEHOLDER = 150
OPTION_TEXT = 100


def _embed_total(embed: Any) -> int:
    total = utf16_length(embed.title or "") + utf16_length(embed.description or "")
    total += utf16_length(getattr(embed.footer, "text", "") or "") + utf16_length(
        getattr(embed.author, "name", "") or ""
    )
    return total + sum(utf16_length(f.name or "") + utf16_length(f.value or "") for f in embed.fields)


def _fit_embed(embed: Any) -> None:
    if embed.title:
        embed.title = fit(embed.title, EMBED_TITLE)
    if embed.description:
        embed.description = fit(embed.description, EMBED_DESCRIPTION)
    footer = embed.footer
    if getattr(footer, "text", None):
        embed.set_footer(text=fit(footer.text, EMBED_FOOTER), icon_url=getattr(footer, "icon_url", None))
    author = embed.author
    if getattr(author, "name", None):
        embed.set_author(
            name=fit(author.name, EMBED_AUTHOR),
            url=getattr(author, "url", None),
            icon_url=getattr(author, "icon_url", None),
        )
    for index, field in enumerate(list(embed.fields)):
        embed.set_field_at(
            index,
            name=fit(field.name or "", EMBED_FIELD_NAME),
            value=fit(field.value or "", EMBED_FIELD_VALUE),
            inline=field.inline,
        )


def _fit_component(component: Any) -> None:
    for child in getattr(component, "children", None) or ():
        _fit_component(child)
    label = getattr(component, "label", None)
    if isinstance(label, str) and hasattr(component, "style"):
        component.label = fit(label, BUTTON_LABEL)
    placeholder = getattr(component, "placeholder", None)
    if isinstance(placeholder, str):
        component.placeholder = fit(placeholder, SELECT_PLACEHOLDER)
    for option in getattr(component, "options", None) or ():
        if isinstance(getattr(option, "label", None), str):
            option.label = fit(option.label, OPTION_TEXT)
        if isinstance(getattr(option, "description", None), str):
            option.description = fit(option.description, OPTION_TEXT)


def fit_payload(data: Any) -> Any:
    """Aplica os limites do Discord a ``data`` (o dicionário de uma skin) e o devolve. Nunca levanta."""
    try:
        embeds = list(data.get("embeds") or ([data["embed"]] if data.get("embed") else []))
        for embed in embeds:
            _fit_embed(embed)
        over = sum(_embed_total(embed) for embed in embeds) - EMBED_TOTAL
        if over > 0 and embeds:
            biggest = max(embeds, key=lambda embed: utf16_length(embed.description or ""))
            current = utf16_length(biggest.description or "")
            biggest.description = fit(biggest.description or "", max(1, current - over))
        if isinstance(data.get("content"), str):
            data["content"] = fit(data["content"], CONTENT)
        for component in data.get("components") or ():
            _fit_component(component)
    except Exception:  # noqa: BLE001 - a rede de segurança nunca derruba o redesenho do player
        return data
    return data
