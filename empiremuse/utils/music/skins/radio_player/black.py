"""Skin PRETA do visor da rádio: tokens medidos na referência escura do dono (cartão ``#1E1E1E``, visor verde ``#67B095``)."""

from __future__ import annotations

from empiremuse.utils.music.skins.radio_player import RGB, RadioSkin

TOKENS: dict[str, RGB] = {
    "card": (30, 30, 30),
    "card_border": (21, 21, 21),
    "lcd": (103, 176, 149),
    "lcd_text": (14, 24, 20),
    "lcd_dim": (30, 70, 55),
    "chip_dark": (17, 17, 17),
    "chip_dark_text": (240, 240, 240),
    "chip_light": (240, 240, 240),
    "chip_light_text": (17, 17, 17),
    "np": (17, 17, 17),
    "np_text": (250, 250, 250),
    "np_dim": (150, 150, 150),
    "np_chip": (40, 40, 40),
    "scale_line": (90, 90, 90),
    "scale_tick": (110, 110, 110),
    "scale_text": (190, 190, 190),
    "pointer": (250, 250, 250),
    "pill": (17, 17, 17),
    "pill_text": (240, 240, 240),
    "round_btn": (17, 17, 17),
    "round_icon": (133, 133, 133),
    "divider": (52, 52, 52),
    "box": (30, 30, 30),
    "box_border": (83, 83, 83),
    "box_label": (150, 150, 150),
    "box_text": (250, 250, 250),
    "side_btn": (17, 17, 17),
    "side_btn_text": (200, 200, 200),
    "heading": (250, 250, 250),
    "heading_dim": (140, 140, 140),
    "st_pill": (51, 51, 51),
    "st_pill_text": (250, 250, 250),
    "st_pill_dim": (170, 170, 170),
    "slider": (16, 16, 16),
    "slider_fill": (106, 176, 150),
    "slider_knob": (250, 250, 250),
    "slider_knob_border": (106, 176, 150),
    "transport": (250, 250, 250),
    "transport_dim": (110, 110, 110),
    "knob": (250, 250, 250),
    "knob_ring": (60, 60, 60),
    "knob_icon": (0, 0, 0),
    "knob_pm": (200, 200, 200),
    "live": (225, 29, 72),
}


def load() -> RadioSkin:
    return RadioSkin("black", "skin_radio_black_preview.png", TOKENS)
