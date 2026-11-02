"""Skin BRANCA do visor da rádio: tokens medidos na referência clara do dono (cartão ``#E7E7E7``, visor azul ``#A5C4DE``)."""

from __future__ import annotations

from empiremuse.utils.music.skins.radio_player import RGB, RadioSkin

TOKENS: dict[str, RGB] = {
    "card": (231, 231, 231),
    "card_border": (210, 210, 210),
    "lcd": (165, 196, 222),
    "lcd_text": (20, 20, 22),
    "lcd_dim": (70, 86, 102),
    "chip_dark": (25, 25, 25),
    "chip_dark_text": (255, 255, 255),
    "chip_light": (255, 255, 255),
    "chip_light_text": (25, 25, 25),
    "np": (25, 25, 25),
    "np_text": (255, 255, 255),
    "np_dim": (170, 170, 170),
    "np_chip": (50, 50, 50),
    "scale_line": (120, 120, 120),
    "scale_tick": (140, 140, 140),
    "scale_text": (60, 60, 60),
    "pointer": (25, 25, 25),
    "pill": (211, 211, 211),
    "pill_text": (25, 25, 25),
    "round_btn": (211, 211, 211),
    "round_icon": (31, 31, 31),
    "divider": (254, 254, 254),
    "box": (242, 242, 242),
    "box_border": (205, 205, 205),
    "box_label": (90, 90, 90),
    "box_text": (25, 25, 25),
    "side_btn": (211, 211, 211),
    "side_btn_text": (25, 25, 25),
    "heading": (25, 25, 25),
    "heading_dim": (110, 110, 110),
    "st_pill": (255, 255, 255),
    "st_pill_text": (25, 25, 25),
    "st_pill_dim": (120, 120, 120),
    "slider": (207, 207, 207),
    "slider_fill": (113, 175, 226),
    "slider_knob": (255, 255, 255),
    "slider_knob_border": (113, 175, 226),
    "transport": (25, 25, 25),
    "transport_dim": (150, 150, 150),
    "knob": (31, 31, 31),
    "knob_ring": (90, 90, 90),
    "knob_icon": (255, 255, 255),
    "knob_pm": (60, 60, 60),
    "live": (225, 29, 72),
}


def load() -> RadioSkin:
    return RadioSkin("white", "skin_radio_white_preview.png", TOKENS)
