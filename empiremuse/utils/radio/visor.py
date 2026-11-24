"""O visor do painel do player de rádio: a imagem PNG do embed, gerada pelo bot a cada atualização (R7, decisão do dono).

Desenho idêntico às referências do dono (rádio de carro), adaptado às funções do bot, em duas skins (``black`` e ``white``,
``utils/music/skins/radio_player``). Tudo que o painel informa vive AQUI (P1): o embed não repete texto. Entrada = ``VisorData``
(imutável, montado por quem tem o estado da rádio); saída = bytes PNG. Texto de fora (estação, música, artista, filtro) só passa
por ``clean_text`` (sem caracteres de controle nem de formatação, e sem o que a Inter não desenha: emoji, CJK, nunca uma caixa
vazia) e é cortado com reticências para caber: a imagem não interpreta
markdown, menção nem link. Os rótulos vêm do tradutor do servidor (chaves ``visor.*``). Especificação em ``docs/design/radio-player.md``.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Mapping
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, Optional

from PIL import Image, ImageDraw, ImageFont

from empiremuse.rules.text import external_text
from empiremuse.utils.music.skins.radio_player import DEFAULT_RADIO_SKIN, RGB, load_radio_skin
from empiremuse.utils.render.fonts import cmap_points
from empiremuse.utils.render.gif import encode_png
from empiremuse.utils.render.primitives import circle_logo, fit, mono_icon

if TYPE_CHECKING:
    from empiremuse.utils.radio.snapshot import RadioPanelSnapshot

Translator = Callable[..., str]
State = Literal["live", "muted", "offline"]
Quality = Literal["good", "ok", "weak", "failing"]
ListKind = Literal["stations", "favorites"]

FONTS_DIR = Path(__file__).resolve().parents[3] / "assets" / "fonts"
INTER_FILES = ("Inter-Regular.ttf", "Inter-Medium.ttf", "Inter-SemiBold.ttf", "Inter-Bold.ttf")
SCALE = 2  # desenha em 2x (1320×432) para o Discord reduzir com nitidez
WIDTH, HEIGHT = 660, 216
HALF = 344  # divisória vertical, na proporção da referência
LCD_RIGHT = 176
MAX_NEXT = 2
MAX_SCALE_POSITIONS = 25  # acima disso a escala vira só a linha e o marcador (um select tem no máximo 25 opções)
LOGO_SIZE = 24
MAX_VOLUME = 150  # o volume do player vai de 10 a 150 (100 = padrão): o deslizante é a fração disso
QUALITY_COLORS: Mapping[str, RGB] = {
    "good": (34, 197, 94),
    "ok": (234, 179, 8),
    "weak": (249, 115, 22),
    "failing": (239, 68, 68),
}


class NextStation(NamedTuple):
    """Uma das próximas estações da lista ativa (pílulas "A seguir")."""

    position: int
    name: str
    bitrate_kbps: int
    quality: Quality


class VisorData(NamedTuple):
    """Tudo que o visor mostra. Quem monta é o lado do bot (snapshot da rádio); nada aqui vai ao banco."""

    station_name: str
    codec: str = ""
    bitrate_kbps: int = 0
    logo: Optional[bytes] = None
    state: State = "live"
    position: int = 1  # posição da estação na lista ativa (1-based); 0 = fora da lista
    total: int = 1  # tamanho da lista ativa
    list_kind: ListKind = "stations"
    favorite: bool = False  # a estação no ar está nas favoritas do servidor
    filter_path: tuple[str, ...] = ()  # ("Brasil", "São Paulo"); vazio em modo favoritas
    reserve_number: Optional[int] = None  # a reserva no ar (número do bot), ou None
    now_title: str = ""
    now_artist: str = ""
    on_air_minutes: int = 0
    volume: int = 100  # volume do player (0..MAX_VOLUME)
    quality: Quality = "good"
    retry_seconds: int = 0  # só em ``offline``
    next_stations: tuple[NextStation, ...] = ()
    custom: bool = False  # rádio PERSONALIZADA (estações do servidor): badge WEB, sem codec, tipo no lugar da reserva


# ---------------------------------------------------------------------------------------------------------------------- texto


@cache
def inter_points() -> frozenset[int]:
    """Os códigos que as quatro Inter do visor desenham (a interseção: o mesmo texto sai em qualquer peso)."""
    return frozenset.intersection(*(cmap_points((FONTS_DIR / name).read_bytes()) for name in INTER_FILES))


def inter_text(text: str) -> tuple[str, bool]:
    """``text`` só com o que a Inter desenha, espaços colapsados, e se algo saiu (``lossy``)."""
    points = inter_points()
    whole = " ".join(text.split())
    kept = " ".join("".join(char for char in whole if ord(char) in points or char.isspace()).split())
    return kept, kept != whole


def clean_text(value: object, limit: int = 120) -> str:
    """Texto de fora, seguro para desenhar: sem controle, formatação ou quebras, sem o que a Inter não desenha
    (``inter_text``); espaços colapsados; no máximo ``limit``."""
    text = str(value or "")
    kept = []
    for char in text:
        category = unicodedata.category(char)
        if category.startswith("C") or category in ("Zl", "Zp"):
            kept.append(" ")
        else:
            kept.append(char)
    return inter_text("".join(kept))[0][:limit]


@cache
def _font(weight: str, size: float) -> ImageFont.FreeTypeFont:
    path = FONTS_DIR / f"Inter-{weight}.ttf"
    return ImageFont.truetype(str(path), int(round(size * SCALE)))


def _px(value: float) -> int:
    return int(round(value * SCALE))


class _Canvas:
    """Primitivas em coordenadas lógicas (1x) sobre uma imagem desenhada em ``SCALE``x."""

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

    def width(self, text: str, font: ImageFont.FreeTypeFont) -> float:
        return self.draw.textlength(text, font=font) / SCALE

    def text(self, xy, text: str, font: ImageFont.FreeTypeFont, fill, anchor: str = "lm", tracking: float = 0) -> float:
        x, y = xy
        if not tracking:
            self.draw.text((_px(x), _px(y)), text, font=font, fill=fill, anchor=anchor)
            return self.width(text, font)
        cx = _px(x)
        for char in text:
            self.draw.text((cx, _px(y)), char, font=font, fill=fill, anchor=anchor)
            cx += self.draw.textlength(char, font=font) + _px(tracking)
        return (cx - _px(x)) / SCALE

    def fit(self, text: str, font: ImageFont.FreeTypeFont, max_width: float) -> str:
        return fit(text, lambda value: self.width(value, font), max_width)

    def icon(self, kind: str, size: float, color: RGB, xy) -> None:
        self.image.alpha_composite(_mono_icon(kind, size, color), (_px(xy[0]), _px(xy[1])))

    def paste(self, image: Image.Image, xy) -> None:
        self.image.alpha_composite(image, (_px(xy[0]), _px(xy[1])))


def _mono_icon(kind: str, size: float, color: RGB) -> Image.Image:
    return mono_icon(kind, _px(size), color)


def _circle_logo(logo: Optional[bytes], size: float, fallback: RGB) -> Image.Image:
    """O logo da estação (PNG do ``LogoService``) recortado em círculo; sem logo, ou bytes que não são PNG, um disco na cor de reserva."""
    return circle_logo(logo, _px(size), fallback)


def volume_level(volume: object) -> float:
    """A fração do deslizante (0.0 a 1.0) para o volume do player (0..MAX_VOLUME); fora da faixa é cortado."""
    try:
        value = int(volume)  # type: ignore[arg-type]
    except TypeError, ValueError:
        value = 0
    return min(max(value, 0), MAX_VOLUME) / MAX_VOLUME


# -------------------------------------------------------------------------------------------------------------------- desenho


def _state_label(tr: Translator, state: str) -> str:
    return tr({"live": "visor.live", "muted": "visor.muted", "offline": "visor.offline"}.get(state, "visor.live"))


def _draw_left(c: _Canvas, t: Mapping[str, RGB], tr: Translator, data: VisorData) -> None:
    offline = data.state == "offline"
    lcd = t["lcd"] if not offline else tuple(int(v * 0.75) for v in t["lcd"])
    c.rrect((14, 14, LCD_RIGHT, 110), 8, fill=lcd)
    chip_font = _font("SemiBold", 9)
    label = _state_label(tr, data.state)
    label_w = c.width(label, chip_font) + 16
    c.rrect((22, 20, 22 + label_w, 34), 3, fill=t["chip_dark"])
    if data.state == "live":
        c.dot(29, 27, 2.5, t["live"])
        c.text((34, 27), label, chip_font, t["chip_dark_text"])
    else:
        c.text((22 + label_w / 2, 27), label, chip_font, t["chip_dark_text"], anchor="mm")
    position = str(data.position) if data.position >= 1 else "—"
    pos_w = max(18, c.width(position, chip_font) + 10)
    c.rrect((26 + label_w, 20, 26 + label_w + pos_w, 34), 3, fill=t["chip_light"], outline=t["chip_dark"], width=1)
    c.text((26 + label_w + pos_w / 2, 27), position, chip_font, t["chip_light_text"], anchor="mm")

    name_color = t["lcd_text"] if data.state != "muted" else t["lcd_dim"]
    raw_name = clean_text(data.station_name, 80) or "—"
    name_width = LCD_RIGHT - 8 - LOGO_SIZE - 6 - 22
    name_font = next(  # um nome comprido encolhe antes de ser cortado
        (f for f in (_font("Bold", 20), _font("Bold", 17), _font("Bold", 15)) if c.width(raw_name, f) <= name_width),
        _font("Bold", 15),
    )
    name = c.fit(raw_name, name_font, name_width)
    c.text((22, 62), name, name_font, name_color)
    c.paste(_circle_logo(data.logo, LOGO_SIZE, t["lcd_dim"]), (LCD_RIGHT - 8 - LOGO_SIZE, 50))
    badge = "WEB" if data.custom else "FM"
    badge_w = c.text((22, 90), badge, _font("Bold", 12), name_color)
    codec = clean_text(data.codec, 8).upper()
    rate = f"{int(data.bitrate_kbps)} kbps" if data.bitrate_kbps else ""
    detail = " · ".join(part for part in (rate, codec) if part)
    if data.custom and not detail:
        detail = tr("visor.custom.detail")
    detail_x = 22 + badge_w + 8
    c.text(
        (detail_x, 91),
        c.fit(detail, _font("Medium", 9.5), LCD_RIGHT - 8 - detail_x),
        _font("Medium", 9.5),
        t["lcd_dim"],
    )

    np_box = (LCD_RIGHT + 8, 14, HALF - 14, 110)
    c.rrect(np_box, 8, fill=t["np"])
    x = np_box[0] + 12
    np_w = np_box[2] - x - 10
    label_font, title_font, sub_font = _font("SemiBold", 8), _font("SemiBold", 14), _font("Regular", 10.5)
    if offline:
        head, title, sub = (
            tr("visor.no_signal"),
            tr("visor.reconnecting"),
            tr("visor.retry", seconds=int(data.retry_seconds)),
        )
    elif data.state == "muted":
        head, title, sub = tr("visor.silenced"), tr("visor.silenced_title"), tr("visor.silenced_hint")
    else:
        head = tr("visor.now_playing")
        title = clean_text(data.now_title, 120) or clean_text(data.station_name, 80)
        sub = clean_text(data.now_artist, 120)
    c.text((x, 28), head, label_font, t["np_dim"], tracking=1.2)
    c.text((x, 50), c.fit(title, title_font, np_w), title_font, t["np_text"])
    if sub:
        c.text((x, 70), c.fit(sub, sub_font, np_w), sub_font, t["np_dim"])
    minutes = tr("visor.on_air", minutes=int(max(0, data.on_air_minutes)))
    chip_w = c.width(minutes, _font("Medium", 10)) + 32
    chip_x = np_box[2] - 10 - chip_w
    c.rrect((chip_x, 86, np_box[2] - 10, 102), 8, fill=t["np_chip"])
    c.icon("clock", 11, t["np_text"], (chip_x + 8, 88.5))
    c.text((chip_x + 22, 94), minutes, _font("Medium", 10), t["np_text"])

    base = 138
    left, right = 40, HALF - 40
    c.dot(24, base - 4, 8, t["round_btn"])
    c.icon("chevron_left", 10, t["round_icon"], (19, base - 9))
    c.dot(HALF - 24, base - 4, 8, t["round_btn"])
    c.icon("chevron_right", 10, t["round_icon"], (HALF - 29, base - 9))
    total = max(1, data.total)
    current = min(max(1, data.position), total)
    c.line(((left, base), (right, base)), t["scale_line"], 1)
    if total <= MAX_SCALE_POSITIONS:
        num_font = _font("Medium", 8.5)
        for i in range(1, total + 1):
            xi = left + (right - left) * (i - 0.5) / total
            c.line(((xi, base - 7), (xi, base)), t["scale_tick"], 1.5)
            if total <= 14 or i == current or i in (1, total):
                c.text((xi, base + 9), str(i), num_font, t["scale_text"], anchor="mm")
            for k in (0.25, 0.5, 0.75):
                xs = left + (right - left) * (i - 1 + k) / total
                c.line(((xs, base - 3), (xs, base)), t["scale_tick"], 1)
    if data.position >= 1:
        xc = left + (right - left) * (current - 0.5) / total
        c.line(((xc, base - 14), (xc, base + 2)), t["pointer"], 3)

    yb = 182
    c.dot(30, yb, 14, t["round_btn"])
    c.icon("star", 16, t["slider_fill"] if data.favorite else t["round_icon"], (22, yb - 8))
    c.rrect((54, yb - 14, HALF - 54, yb + 14), 14, fill=t["pill"])
    key = "visor.favorites" if data.list_kind == "favorites" else "visor.stations"
    pill = tr(key, total=total)
    c.text((54 + (HALF - 108) / 2, yb), pill, _font("SemiBold", 11), t["pill_text"], anchor="mm", tracking=0.6)
    c.dot(HALF - 30, yb, 14, t["round_btn"])
    c.icon("signal", 16, QUALITY_COLORS.get(data.quality, QUALITY_COLORS["good"]), (HALF - 38, yb - 8))


def _draw_right(c: _Canvas, t: Mapping[str, RGB], tr: Translator, data: VisorData) -> None:
    rx = HALF + 14
    rw = WIDTH - 14 - rx
    c.rrect((rx, 14, rx + rw - 66, 46), 6, fill=t["box"], outline=t["box_border"])
    filter_label = tr("visor.filter")
    c.text((rx + 12, 30), filter_label, _font("Medium", 9.5), t["box_label"], tracking=0.8)
    if data.list_kind == "favorites":
        path = tr("visor.favorites_path")
    else:
        path = " › ".join(clean_text(part, 40) for part in data.filter_path if clean_text(part, 40)) or "—"
    path_font = _font("SemiBold", 11.5)
    label_w = c.width(filter_label, _font("Medium", 9.5)) + 12
    max_path = rw - 66 - 26 - 12 - label_w
    c.text((rx + rw - 66 - 26, 30), c.fit(path, path_font, max_path), path_font, t["box_text"], anchor="rm")
    c.icon("chevron_right", 9, t["box_label"], (rx + rw - 66 - 20, 25.5))
    c.rrect((rx + rw - 58, 14, rx + rw, 46), 6, fill=t["side_btn"])
    if data.custom:  # a personalizada não tem reserva: o botão lateral mostra o tipo filtrado (ou "todos os tipos")
        reserve_color = t["side_btn_text"]
        c.icon("list", 13, reserve_color, (rx + rw - 35.5, 17))
        reserve = clean_text(data.filter_path[-1], 40) if data.filter_path else tr("visor.all_types")
    else:
        reserve_color = t["side_btn_text"] if data.reserve_number else t["transport_dim"]
        c.icon("headphone", 13, reserve_color, (rx + rw - 35.5, 17))
        reserve = (
            tr("visor.reserve", number=int(data.reserve_number)) if data.reserve_number else tr("visor.no_reserve")
        )
    c.text(
        (rx + rw - 29, 38), c.fit(reserve, _font("Medium", 7.5), 54), _font("Medium", 7.5), reserve_color, anchor="mm"
    )

    head_font = _font("SemiBold", 12.5)
    head_w = c.text((rx, 62), tr("visor.next"), head_font, t["heading"])
    c.text((rx + head_w + 5, 62), f"· {max(1, data.total)}", _font("Medium", 12), t["heading_dim"])
    quality = data.quality if data.quality in QUALITY_COLORS else "good"
    q_label = tr(f"visor.quality.{quality}")
    q_font = _font("Medium", 11)
    c.text((rx + rw - 14, 62), q_label, q_font, t["heading_dim"], anchor="rm")
    c.dot(rx + rw - 14 - c.width(q_label, q_font) - 9, 62, 4, QUALITY_COLORS[quality])
    pill_w = (rw - 8) / 2
    for i, station in enumerate(data.next_stations[:MAX_NEXT]):
        px0 = rx + i * (pill_w + 8)
        c.rrect((px0, 74, px0 + pill_w, 100), 6, fill=t["st_pill"])
        c.dot(px0 + 11, 87, 3.5, QUALITY_COLORS.get(station.quality, QUALITY_COLORS["good"]))
        rate = str(int(station.bitrate_kbps)) if station.bitrate_kbps else ""
        rate_w = c.width(rate, _font("SemiBold", 11.5)) + 10 if rate else 0
        name_font = _font("Medium", 11.5)
        name = c.fit(f"{int(station.position)}. {clean_text(station.name, 60)}", name_font, pill_w - 30 - rate_w)
        c.text((px0 + 20, 87), name, name_font, t["st_pill_text"])
        if rate:
            c.text((px0 + pill_w - 10, 87), rate, _font("SemiBold", 11.5), t["st_pill_dim"], anchor="rm")
    c.line(((rx, 112), (WIDTH - 14, 112)), t["divider"], 1)

    muted = data.state == "muted"
    level = 0.0 if muted else volume_level(data.volume)
    c.text((rx, 128), tr("visor.volume"), _font("SemiBold", 11.5), t["heading"])
    sx0, sx1 = rx + 56, rx + rw - 110
    c.rrect((sx0, 126, sx1, 130), 2, fill=t["slider"])
    if level:
        c.rrect((sx0, 126, sx0 + (sx1 - sx0) * level, 130), 2, fill=t["slider_fill"])
    kx = sx0 + (sx1 - sx0) * level
    knob = [(kx - 6, 120), (kx + 6, 120), (kx + 6, 132), (kx, 138), (kx - 6, 132)]
    c.draw.polygon(
        [(_px(x), _px(y)) for x, y in knob], fill=t["slider_knob"], outline=t["slider_knob_border"], width=_px(1)
    )
    value = tr("visor.mute") if muted else str(int(data.volume))
    c.text((sx1 + 10, 128), value, _font("SemiBold", 11), t["heading"])
    for i, kind in enumerate(("prev", "speaker_low", "speaker_mute", "speaker", "next")):
        is_mute = kind == "speaker_mute"
        lit = (is_mute and muted) or (not is_mute and not muted)
        c.icon(kind, 18, t["transport"] if lit else t["transport_dim"], (rx + i * 34, 158))
    kcx, kcy = rx + rw - 36, 160
    c.dot(kcx, kcy, 30, t["knob_ring"])
    c.dot(kcx, kcy, 26, t["knob"])
    c.icon("speaker_mute" if muted else "speaker", 20, t["knob_icon"], (kcx - 10, kcy - 10))
    c.icon("plus", 8, t["knob_pm"], (kcx + 28, kcy - 34))
    c.icon("minus", 8, t["knob_pm"], (kcx - 36, kcy + 26))


def render_visor(data: VisorData, tr: Translator, skin: str = DEFAULT_RADIO_SKIN) -> bytes:
    """O PNG do visor (1320×432, 2x de 660×216) para ``data`` na ``skin`` (nome desconhecido = padrão)."""
    tokens = load_radio_skin(skin).tokens
    c = _Canvas()
    c.rrect((0, 0, WIDTH, HEIGHT), 18, fill=tokens["card"], outline=tokens["card_border"], width=1.5)
    c.line(((HALF, 12), (HALF, HEIGHT - 12)), tokens["divider"], 1)
    _draw_left(c, tokens, tr, data)
    _draw_right(c, tokens, tr, data)
    return encode_png(c.image)


# ------------------------------------------------------------------------------------------------------ snapshot do bot


TRACK_TEXT_MAX = 120


def _track_text(value: object, *, custom: bool) -> str:
    """Título e artista da faixa. A personalizada traz o do Last.fm cru: passa aqui pelo ``external_text`` (link, menção,
    NFKC, marcas combinantes; sem escape, que numa imagem apareceria). A ao vivo já guarda o ICY neutralizado."""
    text = "" if value is None else str(value)
    return external_text(text, TRACK_TEXT_MAX, markdown=False) if custom else text


def from_snapshot(snapshot: "RadioPanelSnapshot | Any", *, custom: bool = False) -> VisorData:
    """``VisorData`` a partir do ``RadioPanelSnapshot`` (``utils/radio/snapshot.py``): as duas estações seguintes vêm da janela
    ``stations`` depois de ``current_index`` (ou, sem índice, as de posição maior que a atual); fora da lista não há seguintes."""
    stations = tuple(getattr(snapshot, "stations", ()) or ())
    index = getattr(snapshot, "current_index", None)
    position = int(getattr(snapshot, "position", 0) or 0)
    if index is not None and 0 <= int(index) < len(stations):
        following = stations[int(index) + 1 :]
    elif position >= 1:
        following = tuple(s for s in stations if int(s.position) > position)
    else:
        following = ()
    next_stations = tuple(
        NextStation(int(s.position), str(s.name), int(s.bitrate_kbps or 0), s.quality) for s in following[:MAX_NEXT]
    )
    return VisorData(
        station_name=str(snapshot.station_name or ""),
        codec=str(snapshot.codec or ""),
        bitrate_kbps=int(snapshot.bitrate_kbps or 0),
        logo=snapshot.logo,
        state=snapshot.state if snapshot.state in ("live", "muted", "offline") else "live",
        position=position,
        total=int(snapshot.total or 0),
        list_kind="favorites" if snapshot.list_kind == "favorites" else "stations",
        favorite=bool(snapshot.favorite),
        filter_path=tuple(str(part) for part in (snapshot.filter_path or ())),
        reserve_number=int(snapshot.reserve_number) if snapshot.reserve_number else None,
        now_title=_track_text(snapshot.now_title, custom=custom),
        now_artist=_track_text(snapshot.now_artist, custom=custom),
        on_air_minutes=int(snapshot.on_air_minutes or 0),
        volume=int(snapshot.volume if snapshot.volume is not None else 100),
        quality=snapshot.quality if snapshot.quality in QUALITY_COLORS else "good",
        retry_seconds=int(snapshot.retry_seconds or 0),
        next_stations=next_stations,
        custom=custom,
    )


def render_snapshot(snapshot: "RadioPanelSnapshot | Any", tr: Translator, *, custom: bool = False) -> bytes:
    """O PNG do visor para um snapshot do bot, na skin gravada no documento da rádio. ``custom`` = rádio personalizada
    (``custom_panel.custom_snapshot``): o mesmo retrato, desenhado com as adaptações da ``VisorData.custom``."""
    return render_visor(
        from_snapshot(snapshot, custom=custom), tr, str(getattr(snapshot, "skin", "") or DEFAULT_RADIO_SKIN)
    )
