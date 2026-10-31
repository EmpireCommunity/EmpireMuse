# -*- coding: utf-8 -*-
"""Validação estrita de UM emoji Unicode (o ícone de uma playlist). Funções puras, sem I/O.

Vale um único grafema de emoji, em uma destas formas (nada além disso, nem espaço, nem texto, nem emoji personalizado ``<:x:id>``):

* emoji com apresentação de emoji (``🎧``), ou de texto com VS16 (``U+FE0F``): ``⛏️``, ``🌧️``; tom de pele opcional: ``👍🏽``;
* sequência ZWJ (``U+200D``) só entre as bases que aparecem em sequências reconhecidas: ``👮‍♂️``, ``❤️‍🔥``, ``🏳️‍🌈``;
* bandeira de um país ou região que existe, e as três de subdivisão (Inglaterra, Escócia e País de Gales);
* keycap (``0-9``, ``#`` ou ``*`` + ``U+FE0F`` + ``U+20E3``).

As tabelas (Unicode 16.0) estão em ``emoji_data``; este módulo não depende da versão do ``unicodedata`` do Python.
"""

from __future__ import annotations

import re

from empiremuse.rules.emoji_data import (
    FLAGS,
    PRESENTATION,
    TAG_FLAGS,
    TEXT_DEFAULT,
    ZWJ_LEAD,
    ZWJ_TAIL,
)
from empiremuse.rules.limits import PLAYLIST_ICON_MAX


def _expand(table: str) -> frozenset[int]:
    points: set[int] = set()
    for part in table.split():
        low, _, high = part.partition("-")
        points.update(range(int(low, 16), int(high or low, 16) + 1))
    return frozenset(points)


def _char_class(points: frozenset[int]) -> str:
    """A classe de caracteres de ``re`` com as faixas contíguas de ``points``."""
    ranges: list[list[int]] = []
    for point in sorted(points):
        if ranges and point == ranges[-1][1] + 1:
            ranges[-1][1] = point
        else:
            ranges.append([point, point])
    return "[" + "".join(f"\\U{low:08X}-\\U{high:08X}" for low, high in ranges) + "]"


_PRESENTATION = _expand(PRESENTATION)
_TEXT_DEFAULT = _expand(TEXT_DEFAULT)
_LEAD = _expand(ZWJ_LEAD)
_TAIL = _expand(ZWJ_TAIL)

_VS16 = "️"
_ZWJ = "‍"
_SKIN = "[\U0001f3fb-\U0001f3ff]"
_REGIONAL = "[\U0001f1e6-\U0001f1ff]"


def _element(allowed: frozenset[int] | None = None) -> str:
    """Um emoji simples: de apresentação de emoji (VS16 ou tom de pele opcional) ou de texto (VS16 ou tom de pele obrigatório)."""
    presentation = _PRESENTATION if allowed is None else _PRESENTATION & allowed
    text = _TEXT_DEFAULT if allowed is None else _TEXT_DEFAULT & allowed
    return f"(?:{_char_class(presentation)}(?:{_VS16}|{_SKIN})?|{_char_class(text)}(?:{_VS16}|{_SKIN}))"


_KEYCAP = re.compile(f"[0-9#*]{_VS16}⃣")
_FLAG = re.compile(f"{_REGIONAL}{{2}}")
_EMOJI = re.compile(f"{_element()}|{_element(_LEAD)}(?:{_ZWJ}{_element(_TAIL)})+")


def _is_flag(value: str) -> bool:
    return bool(_FLAG.fullmatch(value)) and "".join(chr(ord(c) - 0x1F1E6 + 65) for c in value) in FLAGS


def check_emoji(raw: object) -> str | None:
    """O emoji (aparado), ou ``None`` se ``raw`` não for exatamente um emoji Unicode (texto, vários, personalizado, longo demais)."""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value or len(value) > PLAYLIST_ICON_MAX:
        return None
    if value in TAG_FLAGS or _is_flag(value) or _KEYCAP.fullmatch(value) or _EMOJI.fullmatch(value):
        return value
    return None


def split_leading_icon(text: str) -> tuple[str | None, str]:
    """``(emoji, resto)`` se ``text`` começa com UM emoji seguido de espaço (ou nada); senão ``(None, text aparado)``."""
    text = text.strip()
    for size in range(min(len(text), PLAYLIST_ICON_MAX), 0, -1):
        rest = text[size:]
        if (rest == "" or rest[0].isspace()) and (icon := check_emoji(text[:size])):
            return icon, rest.strip()
    return None, text
