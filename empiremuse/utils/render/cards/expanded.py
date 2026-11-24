"""Expandida v2 (``muse_expanded_black`` / ``muse_expanded_white``, 480×840 em 1×): a jukebox de verdade, padrão da Jukebox.

Também guarda o pintor dividido com a Slim (``draw_card`` e ``project_card``, parametrizados pelo ``CardLayout``): a Slim
é a Expandida da rodada 1 achatada, com as mesmas regras e sem gabinete, linha do tempo nem linha de informação.

- Todo texto passa pelo ``FontBook.runs`` (fonte da skin e a reserva por caractere) e é cortado com ``…`` para caber.
  Título que não dá para desenhar (``in_message``) vira ``jbx.title_in_text``; o título real vai no embed.
- Nenhum emoji: todo ícone é desenhado aqui (``icon``) ou vem de ``primitives.mono_icon``.
- Nada que envelhece: tocando, a linha do tempo é a régua (só a duração); pausada, a posição exata da pausa.
- Call privada: o desenho usa ``layout.private`` mesmo sem o ``seal`` de quem montou o ``CardSpec``.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import NamedTuple

from PIL import Image, ImageDraw

from empiremuse.rules.card import By, CardItem, CardRadio, CardSpec, CardState
from empiremuse.utils.render import primitives
from empiremuse.utils.render.cards import disc
from empiremuse.utils.render.cards.layout import (
    ARC_STOPS,
    BRAND_FONT,
    BRAND_TEXT,
    BRAND_TRACKING,
    BUBBLE_ALPHA,
    BUBBLE_SIDE,
    CABINET_COLUMNS,
    CABINET_SPAN,
    CHIP_FONT,
    CHIP_GAP,
    CHIP_ICON,
    CHIP_ICON_GAP,
    CHIP_MORE_W,
    CHIP_PAD,
    CHIP_TEXT_MAX,
    EXPANDED,
    FLAG_ALWAYS_ON,
    FLAG_AUTOPLAY,
    FLAG_NIGHTCORE,
    FLAG_REPEAT_QUEUE,
    FLAG_REPEAT_TRACK,
    FLAG_RESTRICTED,
    GRILL_COLUMNS,
    GRILL_DOT,
    GRILL_SPAN,
    GRILL_STEP,
    HEADER_BOT_TEXT_MAX,
    HEADER_STATE_TEXT_MAX,
    HEADING_FONT,
    ICON_SHAPES,
    INFO_FONT,
    INFO_GAP,
    INFO_ICON,
    KNOWN_FLAGS,
    LIGHT_ALPHA,
    LIGHT_HALO,
    LIVE_TRACK_ALPHA,
    MAX_VOLUME,
    NUMBER_FONT,
    RADIO_STATES,
    ROW_COLUMN_GAP,
    ROW_DURATION_W,
    ROW_NUMBER_W,
    ROW_REQUESTER_W,
    ROW_WEIGHT,
    RULER_FIVE,
    RULER_FIVE_ABOVE,
    RULER_MINUTE,
    RULER_NONE_ABOVE,
    SUB_WEIGHT,
    TIMELINE_HEAD,
    TIMELINE_ICON,
    TIMELINE_LIVE_DOT,
    TIMELINE_LIVE_GAP,
    TIMELINE_MARK_H,
    TIMELINE_MARK_W,
    TIMELINE_RING,
    TIMELINE_TRACK,
    TIMELINE_TRACK_H,
    TITLE_WEIGHT,
    TRANSPORT_ICON,
    TRANSPORT_STEP,
    TUBE_BUBBLES,
    TUBE_SPAN,
    TUBE_WIDTH,
    VOLUME_FONT,
    VOLUME_ICON,
    VOLUME_ICON_GAP,
    VOLUME_KNOB,
    VOLUME_SLIDER_H,
    VOLUME_SLIDER_W,
    VOLUME_VALUE_W,
    INFO_TEXT_MAX,
    LIVE_LABEL_MAX,
    QUEUE_KIND_EMPTY,
    QUEUE_KIND_RECOMMENDED,
    TIME_TEXT_W,
    CardArt,
    CardLayout,
    FontSpec,
    Theme,
    Translate,
    clock,
    is_station,
    private,
    theme_for,
)
from empiremuse.utils.render.fonts import FontBook, TextRuns, draw_runs, text_length

RGB = tuple[int, int, int]
RGBA = tuple[int, int, int, int]
CLEAR: RGBA = (0, 0, 0, 0)

SKINS = frozenset({"muse_expanded_black", "muse_expanded_white"})

RULER, PAUSED, LIVE, NONE = "ruler", "paused", "live", "none"


# ------------------------------------------------------------------------------------------------- regras puras


def ruler_marks(duration_s: int | None) -> tuple[int, ...]:
    """As marcas da régua, em segundos: a cada minuto; a cada 5 min acima de 10 min; nenhuma acima de 1 h nem ao vivo."""
    if duration_s is None or duration_s <= 0 or duration_s > RULER_NONE_ABOVE:
        return ()
    step = RULER_FIVE if duration_s > RULER_FIVE_ABOVE else RULER_MINUTE
    return tuple(range(step, duration_s, step))


def timeline_state(spec: CardSpec) -> str:
    """O estado da linha do tempo: ``live`` (transmissão ou rádio R), ``paused`` (posição exata), ``ruler`` (tocando:
    só a duração) ou ``none`` (ocioso, reconectando, call privada, sem faixa)."""
    if spec.state in (CardState.STREAM, CardState.RADIO_LIVE):
        return LIVE
    track = spec.track
    if spec.hidden or track is None or spec.state not in (CardState.PLAYING, CardState.PAUSED):
        return NONE
    if track.duration_s is None:
        return LIVE
    if spec.state is CardState.PAUSED and spec.paused_at_s is not None:
        return PAUSED
    return RULER


def queue_plan(spec: CardSpec, rows_max: int) -> tuple[int, int]:
    """(músicas listadas, ``n`` do "e mais {n}") em no máximo ``rows_max`` linhas; no S a última é "Depois: estação"."""
    budget = rows_max - (1 if is_station(spec) else 0)
    if spec.queue_kind == QUEUE_KIND_RECOMMENDED:
        return min(len(spec.queue), rows_max - 1), 0
    if spec.queue_total <= budget:
        return min(len(spec.queue), spec.queue_total), 0
    shown = min(len(spec.queue), budget - 1)
    return shown, spec.queue_total - shown


def _radio_facts(radio: CardRadio | None, radio_state: bool) -> CardRadio | None:
    if radio is None or radio_state:
        return radio
    if radio.vote is None:
        return None
    return CardRadio("", "", "", "", "", 0, 0, False, radio.vote, None)  # fora de R, só a votação aparece


def project_card(spec: CardSpec, layout: CardLayout, *, voice: bool, paused: bool) -> CardSpec:
    """A projeção dos cartões com disco: zera o que o layout não desenha (fila além das linhas, flags desconhecidas,
    escala, sessão; sem ``voice``, a call; sem ``paused``, a posição da pausa). Idempotente."""
    spec = private(spec)
    radio_state = spec.state in RADIO_STATES
    no_music = radio_state or spec.state is CardState.IDLE
    flags = spec.flags & KNOWN_FLAGS
    track = None if no_music else spec.track
    station = None if no_music else spec.station
    shown, _more = queue_plan(spec._replace(station=station), layout.rows_max)
    return spec._replace(
        scale=1,
        session_played=0,
        track=track,
        cover=spec.cover if track is not None else None,
        paused_at_s=spec.paused_at_s if paused and spec.state is CardState.PAUSED else None,
        queue=() if no_music else spec.queue[:shown],
        queue_total=0 if no_music else spec.queue_total,
        queue_kind=QUEUE_KIND_EMPTY if no_music else spec.queue_kind,
        flags=flags,
        repeat_left=spec.repeat_left if FLAG_REPEAT_TRACK in flags else 0,
        voice_name=spec.voice_name if voice else "",
        radio=_radio_facts(spec.radio, radio_state),
        station=station,
    )


def project(spec: CardSpec) -> CardSpec:
    return project_card(spec, EXPANDED, voice=True, paused=True)


def disc_box(spec: CardSpec) -> tuple[int, int, int, int] | None:
    """A caixa do disco em pixels do arquivo (a Expandida é desenhada em 1×)."""
    return EXPANDED.disc


# ------------------------------------------------------------------------------------------------- cores e ícones


def mix(color: RGB, base: RGB, alpha: float) -> RGB:
    return (
        round(color[0] * alpha + base[0] * (1 - alpha)),
        round(color[1] * alpha + base[1] * (1 - alpha)),
        round(color[2] * alpha + base[2] * (1 - alpha)),
    )


def gradient_at(position: float) -> RGB:
    """A cor do degradê ``arco`` em ``position`` (0 a 1)."""
    position = min(max(position, 0.0), 1.0)
    for (left, start), (right, end) in zip(ARC_STOPS, ARC_STOPS[1:]):
        if position <= right:
            return mix(end, start, (position - left) / (right - left))
    return ARC_STOPS[-1][1]


def icon(kind: str, side: int, color: RGB) -> Image.Image:
    """Ícone monocromático desenhado (RGBA ``side``×``side``): os de ``layout.ICON_SHAPES`` ou os do ``primitives.mono_icon``."""
    shape = ICON_SHAPES.get(kind)
    if shape is None:
        return primitives.mono_icon(kind, side, color)
    canvas = Image.new("RGBA", (primitives.ICON_CANVAS, primitives.ICON_CANVAS), CLEAR)
    shape(ImageDraw.Draw(canvas), color + (255,))
    return canvas.resize((side, side), Image.Resampling.LANCZOS)


# ------------------------------------------------------------------------------------------------- texto


class Ctx(NamedTuple):
    image: Image.Image
    draw: ImageDraw.ImageDraw
    theme: Theme
    tr: Translate
    book: FontBook
    layout: CardLayout


def _measure(book: FontBook, font: FontSpec) -> Callable[[str], float]:
    return lambda value: text_length(book.runs(value, *font))


def fitted_runs(book: FontBook, text: str, font: FontSpec, max_width: float) -> TextRuns | None:
    """Os trechos de ``text`` cortados com ``…`` para caber; ``None`` quando nada pode ser desenhado (``in_message``)."""
    runs = book.runs(text, *font)
    if runs.in_message or not runs.runs:
        return None
    if text_length(runs) <= max_width:
        return runs
    return book.runs(primitives.fit(text, _measure(book, font), max_width), *font)


def put_text(
    ctx: Ctx, text: str, font: FontSpec, at: tuple[float, float], color: RGB, max_width: float, align: str = "l"
) -> float:
    """Desenha ``text`` centrado na vertical em ``at[1]`` (``align``: ``l``, ``m`` ou ``r`` em ``at[0]``); a largura."""
    if not text or max_width <= 0:
        return 0.0
    runs = fitted_runs(ctx.book, text, font, max_width)
    if runs is None:
        return 0.0
    width = text_length(runs)
    x = at[0] - width if align == "r" else at[0] - width / 2 if align == "m" else at[0]
    ascent, descent = ctx.book.font(*font).getmetrics()
    draw_runs(ctx.draw, (x, at[1] + (ascent - descent) / 2), runs, color)
    return width


def split_line(text: str, measure: Callable[[str], float], width: float) -> tuple[str, str]:
    """A 1ª linha (o maior começo que cabe, voltando ao último espaço quando há um) e o resto."""
    low, high = 1, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if measure(text[:middle]) <= width:
            low = middle
        else:
            high = middle - 1
    space = text.rfind(" ", 0, low + 1)
    cut = space if space > 0 else low
    return text[:cut].rstrip(), text[cut:].lstrip()


def wrap(book: FontBook, text: str, font: FontSpec, width: float, lines: int) -> list[str]:
    """``text`` em até ``lines`` linhas; a última leva ``…`` se ainda sobrar texto."""
    measure = _measure(book, font)
    out: list[str] = []
    rest = text
    while rest and len(out) < lines:
        if len(out) == lines - 1 or measure(rest) <= width:
            out.append(primitives.fit(rest, measure, width))
            break
        head, rest = split_line(rest, measure, width)
        out.append(head)
    return out


def paste_icon(ctx: Ctx, kind: str, side: int, color: RGB, xy: tuple[float, float]) -> None:
    x, y = round(xy[0]), round(xy[1])
    if kind == "live":
        ctx.draw.ellipse((x, y, x + side, y + side), fill=ctx.theme.live)
        return
    glyph = icon(kind, side, color)
    ctx.image.paste(glyph, (x, y), glyph)


# ------------------------------------------------------------------------------------------------- chips


class Chip(NamedTuple):
    icon: str | None
    text: str
    color: RGB | None = None  # ``None`` = ``chip_text``


def chip_width(ctx: Ctx, chip: Chip, max_text: float) -> float:
    runs = fitted_runs(ctx.book, chip.text, CHIP_FONT, max_text) if chip.text else None
    text_w = text_length(runs) if runs else 0.0
    icon_w = CHIP_ICON + (CHIP_ICON_GAP if text_w else 0) if chip.icon else 0
    return 2 * CHIP_PAD + icon_w + text_w


def paint_chip(ctx: Ctx, chip: Chip, x: float, span: tuple[int, int], max_text: float, *, right: bool = False) -> float:
    """Um chip arredondado; ``right`` = ``x`` é a borda direita. Devolve a outra borda."""
    width = chip_width(ctx, chip, max_text)
    x0 = x - width if right else x
    y0, y1 = span
    ctx.draw.rounded_rectangle((x0, y0, x0 + width, y1), radius=(y1 - y0) // 2, fill=ctx.theme.chip)
    color = chip.color or ctx.theme.chip_text
    cursor, cy = x0 + CHIP_PAD, (y0 + y1) / 2
    if chip.icon:
        paste_icon(ctx, chip.icon, CHIP_ICON, color, (cursor, cy - CHIP_ICON / 2))
        cursor += CHIP_ICON + CHIP_ICON_GAP
    put_text(ctx, chip.text, CHIP_FONT, (cursor, cy), color, max_text)
    return x0 if right else x0 + width


def state_chip(spec: CardSpec, tr: Translate, theme: Theme) -> Chip:
    state = spec.state
    if state is CardState.PAUSED:
        return Chip("pause", tr("jbx.state.paused").upper(), theme.warn)
    if state in (CardState.STREAM, CardState.RADIO_LIVE):
        return Chip("live", tr("jbx.state.radio").upper())
    if state is CardState.RADIO_RETRY:
        return Chip(None, tr("jbx.state.reconnecting").upper(), theme.warn)
    if state is CardState.IDLE:
        return Chip("stop", tr("jbx.state.idle").upper())
    if is_station(spec):
        return Chip("radio", tr("jbx.state.station").upper())
    return Chip("play", tr("jbx.state.playing").upper())


def _requester_chip(spec: CardSpec, tr: Translate) -> Chip | None:
    track = spec.track
    if track is None:
        return None
    if track.by is By.RECOMMENDED:
        return Chip("sparkle", tr("jbx.recommended"))
    if track.by is By.STATION:
        name = spec.station.name if spec.station is not None else ""
        return Chip("radio", tr("jbx.from_station", nome=name)) if name else None
    return Chip("person", track.requester) if track.requester else None


def _flag_chips(spec: CardSpec, tr: Translate) -> list[Chip]:
    flags = spec.flags
    table = (
        (
            FLAG_REPEAT_TRACK,
            "repeat",
            lambda: tr("jbx.repeat.track", n=spec.repeat_left) if spec.repeat_left else tr("jbx.repeat.track_on"),
        ),
        (FLAG_REPEAT_QUEUE, "repeat", lambda: tr("jbx.repeat.queue")),
        (FLAG_AUTOPLAY, "sparkle", lambda: tr("jbx.chip.autoplay")),
        (FLAG_NIGHTCORE, "moon", lambda: tr("jbx.chip.nightcore")),
        (FLAG_ALWAYS_ON, "clock", lambda: tr("jbx.chip.always_on")),
        (FLAG_RESTRICTED, "lock", lambda: tr("jbx.chip.restricted")),
    )
    return [Chip(kind, text()) for flag, kind, text in table if flag in flags]


def chip_items(spec: CardSpec, tr: Translate) -> list[Chip]:
    """Os chips da linha (fonte e duração, quem pediu, repetir, autoplay, nightcore, rádio, restrito, local, votação)."""
    items: list[Chip] = []
    radio, track = spec.radio, spec.track
    if spec.state in RADIO_STATES and radio is not None:
        place = ", ".join(part for part in (radio.place, radio.country) if part)
        if place:
            items.append(Chip("pin", place))
    elif track is not None:
        length = tr("jbx.state.radio") if track.duration_s is None else clock(track.duration_s)
        items.append(Chip(None, f"{track.source} {length}"))
        who = _requester_chip(spec, tr)
        if who is not None:
            items.append(who)
    items += _flag_chips(spec, tr)
    if radio is not None and radio.vote is not None:
        items.append(Chip("ballot", tr("jbx.vote", votos=radio.vote[0], quorum=radio.vote[1])))
    return items


def paint_chips(ctx: Ctx, spec: CardSpec) -> None:
    layout = ctx.layout
    items = chip_items(spec, ctx.tr)
    x = float(layout.x0)
    for index, item in enumerate(items):
        reserve = CHIP_MORE_W + CHIP_GAP if index < len(items) - 1 else 0
        if x + chip_width(ctx, item, CHIP_TEXT_MAX) > layout.x1 - reserve:
            paint_chip(ctx, Chip(None, f"+{len(items) - index}"), x, layout.chips, CHIP_TEXT_MAX)
            return
        x = paint_chip(ctx, item, x, layout.chips, CHIP_TEXT_MAX) + CHIP_GAP


# ------------------------------------------------------------------------------------------------- decoração


def paint_crown(ctx: Ctx) -> None:
    """O arco no degradê, o arco interno e as luzes (estáticas: animá-las custaria quadros no GIF)."""
    crown, draw, theme = ctx.layout.crown, ctx.draw, ctx.theme
    steps = max(2, int(180 / crown.step_deg))
    for i in range(steps):
        start = 180 + i * crown.step_deg
        draw.arc(
            crown.outer, start, start + crown.step_deg + 0.5, fill=gradient_at(i / (steps - 1)), width=crown.outer_width
        )
    if not crown.inner_inset:
        return
    x0, y0, x1, y1 = crown.outer
    inset = crown.inner_inset
    draw.arc((x0 + inset, y0 + inset, x1 - inset, y1 - inset), 180, 360, fill=theme.line, width=1)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    rx, ry = (x1 - x0) / 2 - inset / 2, (y1 - y0) / 2 - inset / 2
    for k in range(1, crown.lights + 1):
        angle = math.radians(180 + 180 * k / (crown.lights + 1))
        lx, ly = cx + rx * math.cos(angle), cy + ry * math.sin(angle)
        halo, side = crown.light_side, crown.light_side / 2
        draw.ellipse((lx - halo, ly - halo, lx + halo, ly + halo), fill=mix(theme.text_2, theme.card, LIGHT_HALO))
        draw.ellipse((lx - side, ly - side, lx + side, ly + side), fill=mix(theme.text_2, theme.card, LIGHT_ALPHA))


def paint_cabinet(ctx: Ctx) -> None:
    """As laterais: colunas ``vitrine``, o tubo de luz no degradê com 3 bolhas e a grade de alto-falante (decorativas)."""
    draw, theme = ctx.draw, ctx.theme
    top, bottom = CABINET_SPAN
    tube_top, tube_bottom = TUBE_SPAN
    for left, right in CABINET_COLUMNS:
        draw.rectangle((left, top, right - 1, bottom), fill=theme.vitrine)
        tube_x = (left + right) / 2 - TUBE_WIDTH / 2
        for y in range(tube_top, tube_bottom):
            draw.line(
                (tube_x, y, tube_x + TUBE_WIDTH - 1, y), fill=gradient_at((y - tube_top) / (tube_bottom - tube_top))
            )
        for spot in TUBE_BUBBLES:
            by = tube_top + (tube_bottom - tube_top) * spot
            bx = (left + right) / 2
            color = mix(theme.text, gradient_at(spot), BUBBLE_ALPHA)
            draw.ellipse(
                (bx - BUBBLE_SIDE / 2, by - BUBBLE_SIDE / 2, bx + BUBBLE_SIDE / 2, by + BUBBLE_SIDE / 2), fill=color
            )
        for y in range(GRILL_SPAN[0], GRILL_SPAN[1] - GRILL_DOT + 1, GRILL_STEP):
            for gx in GRILL_COLUMNS:
                draw.ellipse((left + gx, y, left + gx + GRILL_DOT - 1, y + GRILL_DOT - 1), fill=theme.line)


def gradient_strip(width: int, height: int, base: RGB, alpha: float) -> Image.Image:
    """Uma faixa RGB no degradê ``arco`` da esquerda para a direita, misturada a ``alpha`` sobre ``base``."""
    row = Image.new("RGB", (max(1, width), 1))
    row.putdata([mix(gradient_at(x / max(1, width - 1)), base, alpha) for x in range(max(1, width))])
    return row.resize((max(1, width), height), Image.Resampling.NEAREST)


def paint_brand(ctx: Ctx, cy: float) -> None:
    x = float(ctx.layout.x0)
    for char in BRAND_TEXT:
        x += put_text(ctx, char, BRAND_FONT, (x, cy), ctx.theme.text, ctx.layout.x1 - x) + BRAND_TRACKING


def paint_header(ctx: Ctx, spec: CardSpec) -> None:
    span = ctx.layout.header
    paint_brand(ctx, (span[0] + span[1]) / 2)
    x = float(ctx.layout.x1)
    if spec.bot_name:
        x = paint_chip(ctx, Chip("headphone", spec.bot_name), x, span, HEADER_BOT_TEXT_MAX, right=True) - CHIP_GAP
    paint_chip(ctx, state_chip(spec, ctx.tr, ctx.theme), x, span, HEADER_STATE_TEXT_MAX, right=True)


# ------------------------------------------------------------------------------------------------- título e linha 2


def title_and_sub(spec: CardSpec, tr: Translate) -> tuple[str, str, bool]:
    """(título, linha 2, o título é texto de fora). Call privada: "Faixa oculta", sem linha 2."""
    if spec.state is CardState.IDLE:
        return tr("jbx.idle.title"), tr("jbx.idle.hint"), False
    if spec.hidden:
        return tr("jbx.hidden_track"), "", False
    radio = spec.radio
    if spec.state in RADIO_STATES:
        if radio is None:
            return tr("jbx.state.radio"), "", False
        if spec.state is CardState.RADIO_RETRY:
            return radio.name, tr("jbx.retry", n=radio.attempt), True
        return (radio.title, radio.name, True) if radio.title else (radio.name, "", True)
    track = spec.track
    if track is None:
        return tr("jbx.idle.title"), "", False
    second = tr("jbx.from_station", nome=spec.station.name) if spec.station is not None else track.origin
    return track.title, " · ".join(part for part in (track.artists, second) if part), True


def paint_title(ctx: Ctx, spec: CardSpec) -> None:
    layout, book = ctx.layout, ctx.book
    title, sub, external = title_and_sub(spec, ctx.tr)
    font = FontSpec(HEADING_FONT.name, TITLE_WEIGHT, layout.title_size)
    if external and book.runs(title, *font).in_message:
        title = ctx.tr("jbx.title_in_text")
    width = layout.x1 - layout.x0
    lines = wrap(book, title, font, width, layout.title_lines)
    for index, line in enumerate(lines):
        cy = layout.title_top + index * layout.title_line_h + layout.title_line_h / 2
        put_text(ctx, line, font, (layout.x0, cy), ctx.theme.text, width)
    top = layout.title_top + max(1, len(lines)) * layout.title_line_h + layout.sub_gap
    sub_font = FontSpec(HEADING_FONT.name, SUB_WEIGHT, layout.sub_size)
    put_text(ctx, sub, sub_font, (layout.x0, top + layout.sub_line_h / 2), ctx.theme.text_2, width)


# ------------------------------------------------------------------------------------------------- linha do tempo


def _track_bar(ctx: Ctx, x0: float, x1: float, cy: float, color: RGB) -> None:
    half = TIMELINE_TRACK_H / 2
    if x1 > x0:
        ctx.draw.rounded_rectangle((x0, cy - half, x1, cy + half), radius=half, fill=color)


def _paint_live(ctx: Ctx, cy: float) -> None:
    x0, x1 = ctx.layout.x0, ctx.layout.x1
    paste_icon(ctx, "live", TIMELINE_LIVE_DOT, ctx.theme.live, (x0, cy - TIMELINE_LIVE_DOT / 2))
    label_x = x0 + TIMELINE_LIVE_DOT + CHIP_ICON_GAP
    width = put_text(ctx, ctx.tr("jbx.state.radio").upper(), NUMBER_FONT, (label_x, cy), ctx.theme.text, LIVE_LABEL_MAX)
    start = round(label_x + width + TIMELINE_LIVE_GAP)
    strip = gradient_strip(x1 - start, TIMELINE_TRACK_H, ctx.theme.card, LIVE_TRACK_ALPHA)
    mask = Image.new("L", strip.size, 0)
    radius = TIMELINE_TRACK_H // 2
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, strip.width - 1, strip.height - 1), radius=radius, fill=255)
    ctx.image.paste(strip, (start, round(cy - radius)), mask)


def _paint_position(ctx: Ctx, spec: CardSpec, duration: int, cy: float) -> None:
    theme, (tx0, tx1) = ctx.theme, TIMELINE_TRACK
    position = min(max(spec.paused_at_s or 0, 0), duration)
    put_text(ctx, clock(position), NUMBER_FONT, (ctx.layout.x0, cy), theme.muted, TIME_TEXT_W)
    head_x = tx0 + (tx1 - tx0) * position / duration
    _track_bar(ctx, tx0, head_x, cy, theme.warn)
    half = TIMELINE_HEAD / 2
    ctx.draw.ellipse(
        (head_x - half, cy - half, head_x + half, cy + half), fill=theme.card, outline=theme.warn, width=TIMELINE_RING
    )


def _paint_ruler(ctx: Ctx, duration: int, cy: float) -> None:
    theme, (tx0, tx1) = ctx.theme, TIMELINE_TRACK
    paste_icon(ctx, "play", TIMELINE_ICON, theme.muted, (ctx.layout.x0, cy - TIMELINE_ICON / 2))
    for mark in ruler_marks(duration):
        x = tx0 + (tx1 - tx0) * mark / duration
        half_w, half_h = TIMELINE_MARK_W / 2, TIMELINE_MARK_H / 2
        ctx.draw.rectangle((x - half_w, cy - half_h, x + half_w - 1, cy + half_h - 1), fill=theme.line)


def paint_timeline(ctx: Ctx, spec: CardSpec) -> None:
    """A linha do tempo (§4.4 da spec): régua tocando, posição exata pausada, AO VIVO; nada sem faixa."""
    span = ctx.layout.timeline
    state = timeline_state(spec)
    if span is None or state == NONE:
        return
    cy = (span[0] + span[1]) / 2
    if state == LIVE:
        _paint_live(ctx, cy)
        return
    duration = spec.track.duration_s if spec.track is not None else None
    if not duration:
        return
    put_text(ctx, clock(duration), NUMBER_FONT, (ctx.layout.x1, cy), ctx.theme.muted, TIME_TEXT_W, "r")
    _track_bar(ctx, TIMELINE_TRACK[0], TIMELINE_TRACK[1], cy, ctx.theme.volume_bg)
    if state == PAUSED:
        _paint_position(ctx, spec, duration, cy)
    else:
        _paint_ruler(ctx, duration, cy)


# ------------------------------------------------------------------------------------------------- informação e fila


def info_items(spec: CardSpec, tr: Translate) -> list[Chip]:
    """A linha de informação: a call e a origem (a playlist ou o álbum, ``Auto`` ou a estação)."""
    items = [Chip("headphone", spec.voice_name)] if spec.voice_name else []
    track = spec.track
    if spec.station is not None:
        items.append(Chip("radio", spec.station.name))
    elif track is not None and track.by is By.RECOMMENDED:
        items.append(Chip("folder", tr("jbx.chip.autoplay")))
    elif track is not None and track.origin:
        items.append(Chip("folder", track.origin))
    return items


def paint_info(ctx: Ctx, spec: CardSpec) -> None:
    span = ctx.layout.info
    if span is None:
        return
    cy, x = (span[0] + span[1]) / 2, float(ctx.layout.x0)
    for item in info_items(spec, ctx.tr):
        room = min(INFO_TEXT_MAX, ctx.layout.x1 - x - INFO_ICON - CHIP_ICON_GAP)
        if room <= 0:
            return
        paste_icon(ctx, item.icon or "list", INFO_ICON, ctx.theme.muted, (x, cy - INFO_ICON / 2))
        x += INFO_ICON + CHIP_ICON_GAP
        x += put_text(ctx, item.text, INFO_FONT, (x, cy), ctx.theme.muted, room) + INFO_GAP


def _row_y(layout: CardLayout, index: int) -> float:
    return layout.rows_top + index * layout.row_h + layout.row_h / 2


def paint_row(ctx: Ctx, index: int, number: int, item: CardItem) -> None:
    layout, theme = ctx.layout, ctx.theme
    cy = _row_y(layout, index)
    row_font = FontSpec(INFO_FONT.name, ROW_WEIGHT, layout.row_size)
    put_text(ctx, f"{number:02d}", NUMBER_FONT, (layout.x0, cy), theme.muted, ROW_NUMBER_W)
    requester_x = layout.x1 - ROW_REQUESTER_W
    duration_right = requester_x - ROW_COLUMN_GAP
    text_x = layout.x0 + ROW_NUMBER_W
    label = f"{item.title} — {item.artists}" if item.artists else item.title
    put_text(ctx, label, row_font, (text_x, cy), theme.text, duration_right - ROW_DURATION_W - ROW_COLUMN_GAP - text_x)
    if item.duration_s:
        put_text(ctx, clock(item.duration_s), row_font, (duration_right, cy), theme.muted, ROW_DURATION_W, "r")
    put_text(ctx, item.requester, row_font, (requester_x, cy), theme.text_2, ROW_REQUESTER_W)


def _note_row(ctx: Ctx, index: int, text: str) -> None:
    layout = ctx.layout
    font = FontSpec(INFO_FONT.name, ROW_WEIGHT, layout.row_size)
    put_text(ctx, text, font, (layout.x0 + ROW_NUMBER_W, _row_y(layout, index)), ctx.theme.muted, layout.x1 - layout.x0)


def paint_on_air(ctx: Ctx) -> None:
    """R: "NO AR" no lugar de "A seguir", só o título: a qualidade, o bitrate e o codec não dizem nada a quem ouve."""
    layout, theme = ctx.layout, ctx.theme
    put_text(ctx, ctx.tr("jbx.on_air").upper(), HEADING_FONT, (layout.x0, sum(layout.upnext) / 2), theme.muted, 200)


def paint_queue(ctx: Ctx, spec: CardSpec) -> None:
    """ "A seguir": até ``rows_max`` linhas (músicas, "e mais {n}", "Depois: estação"); recomendadas; fila vazia."""
    layout, tr = ctx.layout, ctx.tr
    if spec.state is CardState.IDLE:
        return
    if spec.state in RADIO_STATES:
        paint_on_air(ctx)
        return
    recommended = spec.queue_kind == QUEUE_KIND_RECOMMENDED and not spec.hidden
    heading = tr("jbx.recommended_list") if recommended else tr("jbx.up_next", n=spec.queue_total)
    put_text(ctx, heading.upper(), HEADING_FONT, (layout.x0, sum(layout.upnext) / 2), ctx.theme.muted, 300)
    if spec.hidden:
        return
    shown, more = queue_plan(spec, layout.rows_max)
    for index, item in enumerate(spec.queue[:shown]):
        paint_row(ctx, index, index + 1, item)
    notes = [tr("jbx.and_more", n=more)] if more else []
    if spec.station is not None:
        notes.append(tr("jbx.then_station", nome=spec.station.name))
    elif not shown and not more:
        notes.append(tr("jbx.queue.empty"))
    for offset, note in enumerate(notes):
        _note_row(ctx, shown + offset, note)


# ------------------------------------------------------------------------------------------------- base e cartão


def paint_base(ctx: Ctx, spec: CardSpec) -> None:
    """A legenda da linha 1 (a fila apagada sem fila nem autoplay) e o volume."""
    layout, theme = ctx.layout, ctx.theme
    cy = sum(layout.base) / 2
    queue_off = spec.queue_total == 0 and FLAG_AUTOPLAY not in spec.flags
    for index, kind in enumerate(("playpause", "prev", "stop", "next", "list")):
        color = theme.line if kind == "list" and queue_off else theme.text_2
        paste_icon(ctx, kind, TRANSPORT_ICON, color, (layout.x0 + index * TRANSPORT_STEP, cy - TRANSPORT_ICON / 2))
    put_text(ctx, str(spec.volume), VOLUME_FONT, (layout.x1, cy), theme.text, VOLUME_VALUE_W, "r")
    right = layout.x1 - VOLUME_VALUE_W - VOLUME_ICON_GAP
    left = right - VOLUME_SLIDER_W
    level = min(max(spec.volume, 0), MAX_VOLUME) / MAX_VOLUME
    half = VOLUME_SLIDER_H / 2
    ctx.draw.rounded_rectangle((left, cy - half, right, cy + half), radius=half, fill=theme.volume_bg)
    knob_x = left + (right - left) * level
    if level:
        ctx.draw.rounded_rectangle((left, cy - half, knob_x, cy + half), radius=half, fill=theme.volume)
    knob = VOLUME_KNOB / 2
    ctx.draw.ellipse((knob_x - knob, cy - knob, knob_x + knob, cy + knob), fill=theme.text)
    speaker_x = left - VOLUME_ICON_GAP - VOLUME_ICON
    paste_icon(ctx, "speaker", VOLUME_ICON, theme.text_2, (speaker_x, cy - VOLUME_ICON / 2))


def draw_card(spec: CardSpec, art: CardArt, tr: Translate, book: FontBook, layout: CardLayout) -> Image.Image:
    """O cartão estático completo (RGB opaco, ``layout.size``) de um ``CardSpec`` já projetado: é o quadro 0 e o PNG."""
    theme = theme_for(spec.theme)
    image = Image.new("RGB", layout.size, theme.card)
    ctx = Ctx(image, ImageDraw.Draw(image), theme, tr, book, layout)
    paint_crown(ctx)
    if layout.cabinet:
        paint_cabinet(ctx)
    scene = disc.stage(spec, art, layout)
    image.paste(scene.showcase, layout.vitrine[:2])
    image.paste(disc.tiles(scene, layout, 1)[0], layout.disc[:2])
    paint_header(ctx, spec)
    paint_title(ctx, spec)
    paint_timeline(ctx, spec)
    paint_chips(ctx, spec)
    paint_info(ctx, spec)
    for y in (layout.divider_top, layout.divider_bottom):
        ctx.draw.line((layout.x0, y, layout.x1, y), fill=theme.line, width=1)
    paint_queue(ctx, spec)
    paint_base(ctx, spec)
    return image


def draw(spec: CardSpec, art: CardArt, tr: Translate, book: FontBook) -> Image.Image:
    return draw_card(project(spec), art, tr, book, EXPANDED)


def disc_tiles(spec: CardSpec, art: CardArt, frames: int) -> list[Image.Image]:
    return disc.card_tiles(project(spec), art, EXPANDED, frames)
