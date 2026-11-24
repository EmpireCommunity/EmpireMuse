"""O Retrato, v2 (skins 2.3, spec §6): o visor aprovado na vertical, 480×840 lógicos desenhados em ``SCALE``x, usado pela
rádio 24 h (``portrait_black`` e ``portrait_white``): ``PortraitCard`` montado do snapshot da rádio
(``from_radio_snapshot``) e ``render_portrait`` devolve o PNG. "A seguir" mostra ``NEXT_MAX`` estações.

Puro: nenhum I/O além das fontes e nenhum estado. Tokens, estados (``live``, ``muted``, ``offline``), ícones e rótulos
(chaves ``visor.*``) são os do visor (``utils/radio/visor.py``); só a geometria muda.

- **Texto de fora:** o pintor só passa o texto por ``clean_text`` (como o visor). O escape sai no adaptador, onde a origem
  guarda escapado: na rádio ao vivo (``from_radio_snapshot``), o nome da estação e das próximas (diretório) e o título e o
  artista ICY (``clean_title``) perdem um escape por ``stored_text(markdown=False)``. A personalizada chega crua, e a barra
  dela é do próprio texto (``AC\\DC``).
- **Fontes:** só Inter (família Rádio, D7), como o visor: o ``clean_text`` tira o que ela não desenha (emoji, CJK, escrita da
  direita para a esquerda), e o desenho tira de novo qualquer caractere fora dela: nunca sai caixa vazia.
- **Call privada:** a rádio 24 h não esconde nada: o painel dela sai só no chat da própria call, e quem o lê já vê o canal.
  ``seal`` (com ``PortraitCard.hidden``) fica pronto para o dia em que o painel sair fora desse chat; hoje nada o liga.
- **Layout:** toda medida vive em ``LAYOUT`` (coordenadas lógicas, 1×).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import cache
from types import MappingProxyType
from typing import Any, NamedTuple

from PIL import Image, ImageDraw, ImageFont

from empiremuse.rules.text import stored_text
from empiremuse.utils.music.skins.radio_player import RGB, load_radio_skin
from empiremuse.utils.radio.visor import (
    MAX_SCALE_POSITIONS,
    QUALITY_COLORS,
    VisorData,
    clean_text,
    from_snapshot,
    inter_points,
    volume_level,
)
from empiremuse.utils.render.fonts import FONTS_DIR
from empiremuse.utils.render.gif import encode_png
from empiremuse.utils.render.primitives import circle_logo, fit, mono_icon

Translator = Callable[..., str]

RADIO_SKINS = MappingProxyType(
    {"portrait_black": "black", "portrait_white": "white"}
)  # rádio 24 h: skin → tokens do visor
DEFAULT_SKIN = "portrait_black"
SCALE = 2  # desenha em 960×1680 para o Discord reduzir com nitidez
WIDTH, HEIGHT = 480, 840
NEXT_MAX = 4
NAME_SIZES = (24, 21, 18)  # o nome da estação encolhe antes das reticências
TITLE_MAX = 120
NAME_MAX = 80
PART_MAX = 40

# Toda a geometria da rodada 2 (spec §6.1), em coordenadas lógicas: um lugar só para o ajuste que o dono pedir.
LAYOUT: Mapping[str, Any] = MappingProxyType(
    {
        "margin": 16,
        "radius": 18,
        "lcd": (16, 16, 464, 140),
        "logo": 36,
        "now": (16, 152, 464, 252),
        "scale_y": 290,  # linha da escala; as marcas sobem até ``scale_tick_top``
        "scale_tick_top": 276,
        "scale_numbers_y": 304,
        "scale_x": (52, 428),
        "arrow_r": 10,
        "bottom_y": 360,
        "round_r": 20,
        "divider_y": 396,
        "filter": (16, 412, 332, 452),
        "reserve": (344, 412, 464, 452),
        "next_head_y": 486,
        "next_top": 504,
        "next_h": 40,
        "next_gap": 8,
        "volume_y": 718,
        "transport_y": 772,
        "knob": (428, 784),
        "knob_r": 28,
        "knob_ring": 32,
    }
)


class PortraitCard(NamedTuple):
    """O que o retrato desenha: os fatos do visor (montados do snapshot da rádio), a skin e a call privada."""

    data: VisorData
    skin: str = DEFAULT_SKIN
    hidden: bool = False


# --------------------------------------------------------------------------------------------------------------- fatos


def seal(card: PortraitCard) -> PortraitCard:
    """Call privada: nada do que toca nem de onde (estação, logo, título, artista, filtro, nomes das próximas); ficam só as
    contagens, o estado e os controles. Como o painel de texto e o ``rules.card.seal`` da Jukebox."""
    if not card.hidden:
        return card
    data = card.data._replace(
        station_name="",
        logo=None,
        now_title="",
        now_artist="",
        filter_path=(),
        next_stations=tuple(s._replace(name="") for s in card.data.next_stations),
    )
    return card._replace(data=data)


def from_radio_snapshot(snapshot: Any, *, custom: bool = False) -> VisorData:
    """Os fatos do visor para o retrato: o mesmo ``from_snapshot``, com até ``NEXT_MAX`` estações em "A seguir". Na rádio ao
    vivo, o texto guardado escapado (nomes do diretório, título e artista ICY) perde esse escape aqui; o da personalizada
    chega cru e fica como está."""
    data = from_snapshot(snapshot, custom=custom, next_max=NEXT_MAX)
    return data if custom else _unescaped(data)


def _unescaped(data: VisorData) -> VisorData:
    """A rádio ao vivo guarda escapado (``external_text`` no diretório, ``clean_title`` no ICY): um escape a menos, nenhuma
    barra visível na imagem."""

    def text(value: str, limit: int) -> str:
        return stored_text(value, limit, markdown=False)

    return data._replace(
        station_name=text(data.station_name, NAME_MAX),
        now_title=text(data.now_title, TITLE_MAX),
        now_artist=text(data.now_artist, TITLE_MAX),
        next_stations=tuple(s._replace(name=text(s.name, NAME_MAX)) for s in data.next_stations),
    )


# -------------------------------------------------------------------------------------------------------------- fontes


@cache
def _inter(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS_DIR / f"Inter-{weight}.ttf"), size)


def _runs(text: str, weight: str, size_px: int) -> tuple[tuple[str, ImageFont.FreeTypeFont], ...]:
    """O texto em Inter, sem o caractere que ela não desenha (o de fora já chega limpo pelo ``clean_text``)."""
    points = inter_points()
    kept = "".join(char for char in text if ord(char) in points or char.isspace())
    return ((kept, _inter(weight, size_px)),) if kept.strip() else ()


# ------------------------------------------------------------------------------------------------------------- desenho


def _px(value: float) -> int:
    return int(round(value * SCALE))


class _Canvas:
    """Primitivas em coordenadas lógicas (1×) sobre a imagem em ``SCALE``x."""

    def __init__(self) -> None:
        self.image = Image.new("RGBA", (_px(WIDTH), _px(HEIGHT)), (0, 0, 0, 0))
        self.draw = ImageDraw.Draw(self.image)

    def rrect(self, box, radius: float, fill=None, outline=None, width: float = 1) -> None:
        x0, y0, x1, y1 = (_px(v) for v in box)
        self.draw.rounded_rectangle((x0, y0, x1, y1), radius=_px(radius), fill=fill, outline=outline, width=_px(width))

    def line(self, points, fill, width: float = 1) -> None:
        self.draw.line([(_px(x), _px(y)) for x, y in points], fill=fill, width=_px(width))

    def dot(self, cx: float, cy: float, r: float, fill) -> None:
        self.draw.ellipse((_px(cx - r), _px(cy - r), _px(cx + r), _px(cy + r)), fill=fill)

    def width(self, text: str, weight: str, size: float) -> float:
        return sum(font.getlength(piece) for piece, font in _runs(text, weight, _px(size))) / SCALE

    def fit(self, text: str, weight: str, size: float, max_width: float) -> str:
        return fit(text, lambda value: self.width(value, weight, size), max_width)

    def text(self, xy, text: str, weight: str, size: float, fill, anchor: str = "lm", tracking: float = 0) -> float:
        """Desenha na linha do meio (``l``/``m``/``r`` na horizontal); devolve a largura lógica."""
        runs = _runs(text, weight, _px(size))
        gap = _px(tracking)
        total = sum(font.getlength(piece) for piece, font in runs) + gap * max(0, len(text) - 1)
        x = _px(xy[0]) - {"l": 0, "m": total / 2, "r": total}[anchor[0]]
        y = _px(xy[1])
        for piece, font in runs:
            for chunk in piece if gap else (piece,):
                self.draw.text((x, y), chunk, font=font, fill=fill, anchor="lm")
                x += font.getlength(chunk) + gap
        return total / SCALE

    def icon(self, kind: str, size: float, color: RGB, xy) -> None:
        self.image.alpha_composite(mono_icon(kind, _px(size), color), (_px(xy[0]), _px(xy[1])))

    def paste(self, image: Image.Image, xy) -> None:
        self.image.alpha_composite(image, (_px(xy[0]), _px(xy[1])))


def _state_label(tr: Translator, state: str) -> str:
    return tr({"live": "visor.live", "muted": "visor.muted", "offline": "visor.offline"}.get(state, "visor.live"))


def _draw_lcd(c: _Canvas, t: Mapping[str, RGB], tr: Translator, data: VisorData) -> None:
    x0, y0, x1, y1 = LAYOUT["lcd"]
    lcd = t["lcd"] if data.state != "offline" else tuple(int(v * 0.75) for v in t["lcd"])
    c.rrect((x0, y0, x1, y1), 10, fill=lcd)
    top = y0 + ((y1 - y0) - 78) / 2  # conteúdo centrado: chips 16 · 10 · nome 28 · 8 · banda 16
    chips_y = top + 8
    label = _state_label(tr, data.state)
    label_w = c.width(label, "SemiBold", 10) + 18
    c.rrect((x0 + 14, top, x0 + 14 + label_w, top + 16), 3, fill=t["chip_dark"])
    if data.state == "live":
        c.dot(x0 + 22, chips_y, 2.8, t["live"])
        c.text((x0 + 28, chips_y), label, "SemiBold", 10, t["chip_dark_text"])
    else:
        c.text((x0 + 14 + label_w / 2, chips_y), label, "SemiBold", 10, t["chip_dark_text"], anchor="mm")
    position = str(data.position) if data.position >= 1 else "—"
    pos_x = x0 + 18 + label_w
    pos_w = max(20, c.width(position, "SemiBold", 10) + 12)
    c.rrect((pos_x, top, pos_x + pos_w, top + 16), 3, fill=t["chip_light"], outline=t["chip_dark"], width=1)
    c.text((pos_x + pos_w / 2, chips_y), position, "SemiBold", 10, t["chip_light_text"], anchor="mm")

    name_color = t["lcd_text"] if data.state != "muted" else t["lcd_dim"]
    logo = LAYOUT["logo"]
    name_y = top + 16 + 10 + 14
    name_width = (x1 - 14 - logo - 10) - (x0 + 14)
    raw_name = clean_text(data.station_name, NAME_MAX) or "—"
    size = next((s for s in NAME_SIZES if c.width(raw_name, "Bold", s) <= name_width), NAME_SIZES[-1])
    c.text((x0 + 14, name_y), c.fit(raw_name, "Bold", size, name_width), "Bold", size, name_color)
    c.paste(circle_logo(data.logo, _px(logo), t["lcd_dim"]), (x1 - 14 - logo, name_y - logo / 2))
    band_y = name_y + 14 + 8 + 8
    badge_w = c.text((x0 + 14, band_y), "WEB" if data.custom else "FM", "Bold", 13, name_color)
    rate = f"{int(data.bitrate_kbps)} kbps" if data.bitrate_kbps else ""
    detail = " · ".join(part for part in (rate, clean_text(data.codec, 8).upper()) if part)
    if data.custom and not detail:
        detail = tr("visor.custom.detail")
    dx = x0 + 14 + badge_w + 10
    c.text((dx, band_y), c.fit(detail, "Medium", 11, x1 - 14 - dx), "Medium", 11, t["lcd_dim"])


def _draw_now(c: _Canvas, t: Mapping[str, RGB], tr: Translator, data: VisorData) -> None:
    x0, y0, x1, y1 = LAYOUT["now"]
    c.rrect((x0, y0, x1, y1), 10, fill=t["np"])
    x, width = x0 + 14, (x1 - 14) - (x0 + 14)
    if data.state == "offline":
        retry = tr("visor.retry", seconds=int(data.retry_seconds))
        head, title, sub = tr("visor.no_signal"), tr("visor.reconnecting"), retry
    elif data.state == "muted":
        head, title, sub = tr("visor.silenced"), tr("visor.silenced_title"), tr("visor.silenced_hint")
    else:
        head = tr("visor.now_playing")
        title = clean_text(data.now_title, TITLE_MAX) or clean_text(data.station_name, NAME_MAX) or "—"
        sub = clean_text(data.now_artist, TITLE_MAX)
    c.text((x, y0 + 18), head, "SemiBold", 9, t["np_dim"], tracking=1.2)
    c.text((x, y0 + 44), c.fit(title, "SemiBold", 17, width), "SemiBold", 17, t["np_text"])
    if sub:
        c.text((x, y0 + 66), c.fit(sub, "Regular", 12, width - 110), "Regular", 12, t["np_dim"])
    minutes = tr("visor.on_air", minutes=int(max(0, data.on_air_minutes)))
    chip_w = c.width(minutes, "Medium", 11) + 34
    cx = x1 - 14 - chip_w
    c.rrect((cx, y1 - 30, x1 - 14, y1 - 12), 9, fill=t["np_chip"])
    c.icon("clock", 12, t["np_text"], (cx + 8, y1 - 27))
    c.text((cx + 24, y1 - 21), minutes, "Medium", 11, t["np_text"])


def _draw_scale(c: _Canvas, t: Mapping[str, RGB], data: VisorData) -> None:
    base, top, numbers_y = LAYOUT["scale_y"], LAYOUT["scale_tick_top"], LAYOUT["scale_numbers_y"]
    left, right = LAYOUT["scale_x"]
    r = LAYOUT["arrow_r"]
    for cx, kind in ((LAYOUT["margin"] + r + 4, "chevron_left"), (WIDTH - LAYOUT["margin"] - r - 4, "chevron_right")):
        c.dot(cx, base - 4, r, t["round_btn"])
        c.icon(kind, 12, t["round_icon"], (cx - 6, base - 10))
    total = max(1, data.total)
    current = min(max(1, data.position), total)
    c.line(((left, base), (right, base)), t["scale_line"], 1)
    if total <= MAX_SCALE_POSITIONS:
        for i in range(1, total + 1):
            xi = left + (right - left) * (i - 0.5) / total
            c.line(((xi, top), (xi, base)), t["scale_tick"], 1.5)
            if total <= 14 or i == current or i in (1, total):
                c.text((xi, numbers_y), str(i), "Medium", 10, t["scale_text"], anchor="mm")
            for k in (0.25, 0.5, 0.75):
                xs = left + (right - left) * (i - 1 + k) / total
                c.line(((xs, base - 4), (xs, base)), t["scale_tick"], 1)
    if data.position >= 1:
        xc = left + (right - left) * (current - 0.5) / total
        c.line(((xc, top - 6), (xc, base + 2)), t["pointer"], 3)


def _draw_bottom_row(c: _Canvas, t: Mapping[str, RGB], tr: Translator, data: VisorData) -> None:
    y, r, m = LAYOUT["bottom_y"], LAYOUT["round_r"], LAYOUT["margin"]
    c.dot(m + r, y, r, t["round_btn"])
    c.icon("star", 20, t["slider_fill"] if data.favorite else t["round_icon"], (m + r - 10, y - 10))
    pill = (m + 2 * r + 12, y - r, WIDTH - m - 2 * r - 12, y + r)
    c.rrect(pill, r, fill=t["pill"])
    key = "visor.favorites" if data.list_kind == "favorites" else "visor.stations"
    label = tr(key, total=max(1, data.total))
    c.text(((pill[0] + pill[2]) / 2, y), label, "SemiBold", 13, t["pill_text"], anchor="mm", tracking=0.6)
    c.dot(WIDTH - m - r, y, r, t["round_btn"])
    c.icon("signal", 20, QUALITY_COLORS.get(data.quality, QUALITY_COLORS["good"]), (WIDTH - m - r - 10, y - 10))
    c.line(((m, LAYOUT["divider_y"]), (WIDTH - m, LAYOUT["divider_y"])), t["divider"], 1)


def _draw_filter_reserve(c: _Canvas, t: Mapping[str, RGB], tr: Translator, data: VisorData) -> None:
    fx0, fy0, fx1, fy1 = LAYOUT["filter"]
    cy = (fy0 + fy1) / 2
    c.rrect((fx0, fy0, fx1, fy1), 8, fill=t["box"], outline=t["box_border"])
    label_w = c.text((fx0 + 12, cy), tr("visor.filter"), "Medium", 10.5, t["box_label"], tracking=0.8)
    if data.list_kind == "favorites":
        path = tr("visor.favorites_path")
    else:
        parts = (clean_text(part, PART_MAX) for part in data.filter_path)
        path = " › ".join(part for part in parts if part) or "—"
    room = (fx1 - 24) - (fx0 + 12 + label_w + 12)
    c.text((fx1 - 24, cy), c.fit(path, "SemiBold", 12.5, room), "SemiBold", 12.5, t["box_text"], anchor="rm")
    c.icon("chevron_right", 10, t["box_label"], (fx1 - 18, cy - 5))
    rx0, ry0, rx1, ry1 = LAYOUT["reserve"]
    c.rrect((rx0, ry0, rx1, ry1), 8, fill=t["side_btn"])
    if data.custom:  # a personalizada não tem reserva: o tipo filtrado (ou "todos os tipos") no lugar
        color, icon = t["side_btn_text"], "list"
        label = clean_text(data.filter_path[-1], PART_MAX) if data.filter_path else tr("visor.all_types")
    else:
        color = t["side_btn_text"] if data.reserve_number else t["transport_dim"]
        icon = "headphone"
        number = data.reserve_number
        label = tr("visor.reserve", number=int(number)) if number else tr("visor.no_reserve")
    ry = (ry0 + ry1) / 2
    c.icon(icon, 14, color, (rx0 + 12, ry - 7))
    c.text((rx0 + 38, ry), c.fit(label, "Medium", 10.5, rx1 - rx0 - 48), "Medium", 10.5, color)


def _draw_next(c: _Canvas, t: Mapping[str, RGB], tr: Translator, data: VisorData) -> None:
    m, y = LAYOUT["margin"], LAYOUT["next_head_y"]
    head_w = c.text((m, y), tr("visor.next"), "SemiBold", 14, t["heading"])
    c.text((m + head_w + 6, y), f"· {max(1, data.total)}", "Medium", 13, t["heading_dim"])
    quality = data.quality if data.quality in QUALITY_COLORS else "good"
    q_w = c.text((WIDTH - m, y), tr(f"visor.quality.{quality}"), "Medium", 12, t["heading_dim"], anchor="r")
    c.dot(WIDTH - m - q_w - 10, y, 4.5, QUALITY_COLORS[quality])
    h, gap = LAYOUT["next_h"], LAYOUT["next_gap"]
    for i, station in enumerate(data.next_stations[:NEXT_MAX]):  # posições fixas: as que faltam somem, nada sobe
        top = LAYOUT["next_top"] + i * (h + gap)
        cy = top + h / 2
        c.rrect((m, top, WIDTH - m, top + h), 10, fill=t["st_pill"])
        c.dot(m + 16, cy, 4, QUALITY_COLORS.get(station.quality, QUALITY_COLORS["good"]))
        rate = str(int(station.bitrate_kbps)) if station.bitrate_kbps else ""
        rate_w = c.text((WIDTH - m - 14, cy), rate, "SemiBold", 13, t["st_pill_dim"], anchor="r") + 12 if rate else 0
        name = clean_text(station.name, 60)
        label = f"{int(station.position)}. {name}" if name else f"{int(station.position)}."
        room = (WIDTH - m - 14 - rate_w) - (m + 30)
        c.text((m + 30, cy), c.fit(label, "Medium", 13, room), "Medium", 13, t["st_pill_text"])


def _draw_controls(c: _Canvas, t: Mapping[str, RGB], tr: Translator, data: VisorData) -> None:
    m, y = LAYOUT["margin"], LAYOUT["volume_y"]
    muted = data.state == "muted"
    level = 0.0 if muted else volume_level(data.volume)
    label_w = c.text((m, y), tr("visor.volume"), "SemiBold", 13, t["heading"])
    sx0, sx1 = m + label_w + 16, WIDTH - m - 56
    c.rrect((sx0, y - 2, sx1, y + 2), 2, fill=t["slider"])
    if level:
        c.rrect((sx0, y - 2, sx0 + (sx1 - sx0) * level, y + 2), 2, fill=t["slider_fill"])
    kx = sx0 + (sx1 - sx0) * level
    knob = [(kx - 7, y - 9), (kx + 7, y - 9), (kx + 7, y + 5), (kx, y + 12), (kx - 7, y + 5)]
    c.draw.polygon(
        [(_px(px), _px(py)) for px, py in knob], fill=t["slider_knob"], outline=t["slider_knob_border"], width=_px(1)
    )
    c.text((sx1 + 12, y), tr("visor.mute") if muted else str(int(data.volume)), "SemiBold", 12.5, t["heading"])
    ty = LAYOUT["transport_y"]
    for i, kind in enumerate(("prev", "speaker_low", "speaker_mute", "speaker", "next")):
        is_mute = kind == "speaker_mute"
        lit = (is_mute and muted) or (not is_mute and not muted)
        c.icon(kind, 22, t["transport"] if lit else t["transport_dim"], (m + i * 46, ty))
    kcx, kcy = LAYOUT["knob"]
    ring = LAYOUT["knob_ring"]
    c.dot(kcx, kcy, ring, t["knob_ring"])
    c.dot(kcx, kcy, LAYOUT["knob_r"], t["knob"])
    c.icon("speaker_mute" if muted else "speaker", 22, t["knob_icon"], (kcx - 11, kcy - 11))
    c.icon("plus", 9, t["knob_pm"], (kcx + ring - 6, kcy - ring - 4))
    c.icon("minus", 9, t["knob_pm"], (kcx - ring - 4, kcy + ring - 6))


def _tokens(skin: str) -> Mapping[str, RGB]:
    """Os tokens do visor para a skin da rádio 24 h; desconhecida = a padrão."""
    name = RADIO_SKINS.get(skin) or RADIO_SKINS[DEFAULT_SKIN]
    return load_radio_skin(name).tokens


def paint(card: PortraitCard, tr: Translator) -> Image.Image:
    """O retrato em RGBA (960×1680, cantos transparentes). A call privada passa pelo ``seal``."""
    card = seal(card)
    tokens = _tokens(card.skin)
    c = _Canvas()
    c.rrect((0, 0, WIDTH, HEIGHT), LAYOUT["radius"], fill=tokens["card"], outline=tokens["card_border"], width=1.5)
    _draw_lcd(c, tokens, tr, card.data)
    _draw_now(c, tokens, tr, card.data)
    _draw_scale(c, tokens, card.data)
    _draw_bottom_row(c, tokens, tr, card.data)
    _draw_filter_reserve(c, tokens, tr, card.data)
    _draw_next(c, tokens, tr, card.data)
    _draw_controls(c, tokens, tr, card.data)
    return c.image


def render_portrait(card: PortraitCard, tr: Translator) -> bytes:
    """O PNG do retrato (960×1680, 2× de 480×840) para ``card``; skin desconhecida = a padrão."""
    return encode_png(paint(card, tr))
