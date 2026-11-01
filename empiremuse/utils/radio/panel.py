"""A mensagem de controle de uma rádio no chat da própria call (item 8, R1, R-6): o texto e os três botões.

Os ``custom_id`` são fixos (``rules.radio.PANEL_IDS``) e NÃO levam rádio, bot nem servidor: quem trata o clique acha a rádio por
``interaction.guild`` mais o bot que recebeu a interação. O nome da playlist passa pela função única de texto externo (R-11).
"""

from __future__ import annotations

from typing import Any

import disnake

from empiremuse.rules.i18n import fit
from empiremuse.rules.radio import PANEL_NEXT_ID, PANEL_PAUSE_ID, PANEL_STOP_ID
from empiremuse.rules.text import external_text

NAME_LIMIT = 80


def panel_payload(tr: Any, *, playlist_name: object, color: int = 0) -> dict[str, Any]:
    """``{"embeds", "components"}`` do painel; quem envia põe ``AllowedMentions.none()``."""
    embed = disnake.Embed(
        title=tr("radio.panel.title"),
        description=tr("radio.panel.description", playlist=external_text(playlist_name, NAME_LIMIT)),
        color=color,
    )
    buttons = [
        disnake.ui.Button(label=fit(tr("radio.panel.pause"), 80), emoji="⏯️", custom_id=PANEL_PAUSE_ID),
        disnake.ui.Button(label=fit(tr("radio.panel.next"), 80), emoji="⏭️", custom_id=PANEL_NEXT_ID),
        disnake.ui.Button(
            label=fit(tr("radio.panel.stop"), 80),
            emoji="⏹️",
            style=disnake.ButtonStyle.danger,
            custom_id=PANEL_STOP_ID,
        ),
    ]
    return {"embeds": [embed], "components": [disnake.ui.ActionRow(*buttons)]}
