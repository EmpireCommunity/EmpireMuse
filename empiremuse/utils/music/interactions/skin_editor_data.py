# -*- coding: utf-8 -*-
"""Chaves de catálogo das recusas do editor de skins e a skin base no idioma do servidor.

Os textos da skin base são o conteúdo inicial que o admin edita; eles ficam no catálogo (``skin_editor.base.*``) com
placeholders simples (``{duration}``...), porque o catálogo não aceita os placeholders de skin com ponto (``{track.title}``)
nem links em markdown. ``base_skin_for(tr)`` resolve o texto e troca cada placeholder simples pelo placeholder de skin
correspondente (``BASE_SKIN_PLACEHOLDERS``); ``base_skin`` é o esqueleto, com as CHAVES no lugar dos textos.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable

SKIN_NAME_REFUSED = "skin_editor.refused.skin_name"
SKIN_REFUSED = "skin_editor.refused.content"
SKIN_LIMIT_REFUSED = "skin_editor.refused.limit"

# Placeholder simples do catálogo -> placeholder de skin (ou trecho com link) que o ``skin_converter`` entende.
BASE_SKIN_PLACEHOLDERS: dict[str, str] = {
    "track_link": "[{track.title}]({track.url})",
    "duration": "{track.duration}",
    "requester": "{requester.mention}",
    "author": "{track.author}",
    "playlist_link": "[`{playlist.name}`]({playlist.url})",
    "log_emoji": "{player.log.emoji}",
    "log_text": "{player.log.text}",
    "queue_size": "{player.queue.size}",
}

base_skin = {
    "queue_max_entries": 7,
    "queue_format": "`{track.number}) [{track.duration}]` [`{track.title_42}`]({track.url})",
    "embeds": [
        {"title": "skin_editor.base.queue_title", "description": "{queue_format}", "color": "{guild.color}"},
        {
            "description": "skin_editor.base.now_playing",
            "image": {"url": "{track.thumb}"},
            "color": "{guild.color}",
            "footer": {"text": "skin_editor.base.footer"},
        },
    ],
}


def base_skin_for(tr: Callable[..., str]) -> dict[str, Any]:
    """A skin base com os textos no idioma de ``tr`` e os placeholders de skin no lugar (cópia nova a cada chamada)."""
    skin = deepcopy(base_skin)
    queue, now_playing = skin["embeds"]
    queue["title"] = tr("skin_editor.base.queue_title")
    now_playing["description"] = tr("skin_editor.base.now_playing", **BASE_SKIN_PLACEHOLDERS)
    now_playing["footer"]["text"] = tr("skin_editor.base.footer", **BASE_SKIN_PLACEHOLDERS)
    return skin
