"""Skins do painel do player de rádio: o visor (imagem gerada) tem duas skins, ``black`` e ``white``, idênticas às referências do dono.

Cada skin é um módulo desta pasta com ``load()`` devolvendo ``RadioSkin``: o nome, a prévia (asset) e a tabela de tokens que o
renderizador (``empiremuse/utils/radio/visor.py``) usa. O carregador do cliente (``utils/client.py``) só varre ``normal_player`` e
``static_player``; as skins da rádio são registradas aqui, de propósito, porque o visor não é um player de música. Desenho e tokens
documentados em ``docs/design/radio-player.md``.
"""

from __future__ import annotations

from importlib import import_module
from typing import Mapping, NamedTuple

RGB = tuple[int, int, int]

# Todo token que o renderizador lê; as duas skins precisam definir todos (um teste confere).
TOKEN_NAMES: tuple[str, ...] = (
    "card",
    "card_border",
    "lcd",
    "lcd_text",
    "lcd_dim",
    "chip_dark",
    "chip_dark_text",
    "chip_light",
    "chip_light_text",
    "np",
    "np_text",
    "np_dim",
    "np_chip",
    "scale_line",
    "scale_tick",
    "scale_text",
    "pointer",
    "pill",
    "pill_text",
    "round_btn",
    "round_icon",
    "divider",
    "box",
    "box_border",
    "box_label",
    "box_text",
    "side_btn",
    "side_btn_text",
    "heading",
    "heading_dim",
    "st_pill",
    "st_pill_text",
    "st_pill_dim",
    "slider",
    "slider_fill",
    "slider_knob",
    "slider_knob_border",
    "transport",
    "transport_dim",
    "knob",
    "knob_ring",
    "knob_icon",
    "knob_pm",
    "live",
)

SKIN_NAMES: tuple[str, ...] = ("black", "white")
DEFAULT_RADIO_SKIN = "black"


class RadioSkin(NamedTuple):
    name: str
    preview: str  # nome do asset da prévia (``assets/skins/skin_radio_<nome>_preview.png``)
    tokens: Mapping[str, RGB]


def load_radio_skin(name: str) -> RadioSkin:
    """A skin pelo nome; nome desconhecido cai na padrão (nunca levanta por causa de um valor vindo do documento)."""
    chosen = name if name in SKIN_NAMES else DEFAULT_RADIO_SKIN
    module = import_module(f"{__name__}.{chosen}")
    return module.load()


def load_radio_skins() -> dict[str, RadioSkin]:
    return {name: load_radio_skin(name) for name in SKIN_NAMES}
