"""Rádio da Jukebox (``muse_radio_black`` / ``muse_radio_white``): o visor aprovado da rádio, 660×216 em 1× (arquivo
1320×432 em 2×), com os controles da Jukebox.

O desenho é o ``utils/radio/visor.render_visor``, sem cópia: aqui só se monta a ``VisorData`` e se trocam os rótulos.
- **R** (rádio ao vivo): o visor da rádio como ele é (estação, logo, título ICY, codec, local no lugar do filtro).
- **M/S**: cada módulo troca de conteúdo pela tabela da §3 da spec, até onde a ``VisorData`` deixa: o nome vira o título,
  o logo vira a capa, a escala vira a sessão, a caixa FILTRO vira ORIGEM, a reserva mostra o bot, a fila nas pílulas.
  Os rótulos do visor (chaves ``visor.*``) que mudam de sentido são trocados por textos ``jbx.*`` já traduzidos.
- Sem disco: o visor é só PNG (``disc_box`` = ``None``, ``disc_tiles`` levanta).
- O visor escreve em Inter, sem a reserva por caractere; o título passa antes pelo ``FontBook.runs`` só para a regra do
  texto da direita para a esquerda (``jbx.title_in_text``).
"""

from __future__ import annotations

import io
from collections.abc import Callable
from typing import NamedTuple

from PIL import Image

from empiremuse.rules.card import By, CardItem, CardSpec, CardState
from empiremuse.rules.radio_quality import station_quality
from empiremuse.utils.music.skins.radio_player import load_radio_skin
from empiremuse.utils.radio import visor
from empiremuse.utils.radio.visor import NextStation, VisorData, render_visor
from empiremuse.utils.render.cards import disc
from empiremuse.utils.render.cards.layout import (
    FLAG_ALWAYS_ON,
    FLAG_AUTOPLAY,
    FLAG_NIGHTCORE,
    FLAG_RESTRICTED,
    DEFAULT_THEME,
    LIGHT_THEME_NAME,
    RADIO_STATES,
    VISOR_TEXT_CHECK,
    CardArt,
    Translate,
    clock,
    fill,
    is_station,
    private,
)
from empiremuse.utils.render.fonts import FontBook
from empiremuse.utils.render.gif import encode_png

SKINS = frozenset({"muse_radio_black", "muse_radio_white"})

VISOR_SIZE = (visor.WIDTH, visor.HEIGHT)  # 1×
VISOR_SCALE = visor.SCALE
VISOR_FILE_SIZE = (visor.WIDTH * visor.SCALE, visor.HEIGHT * visor.SCALE)
NEXT_MAX = visor.MAX_NEXT
NO_VALUE = "—"
CHIP_FLAGS: tuple[tuple[str, str], ...] = (
    (FLAG_AUTOPLAY, "jbx.chip.autoplay"),
    (FLAG_NIGHTCORE, "jbx.chip.nightcore"),
    (FLAG_ALWAYS_ON, "jbx.chip.always_on"),
    (FLAG_RESTRICTED, "jbx.chip.restricted"),
)
DRAWN_FLAGS = frozenset(flag for flag, _key in CHIP_FLAGS)


QUALITIES: dict[str, visor.Quality] = {"good": "good", "ok": "ok", "weak": "weak", "failing": "failing"}


class VisorView(NamedTuple):
    """A ``VisorData`` e os rótulos do visor trocados (chave ``visor.*`` → texto pronto)."""

    data: VisorData
    labels: dict[str, str]


def _item(item: CardItem) -> CardItem:
    return CardItem(item.title, "", None, "")  # a pílula mostra só o título


def project(spec: CardSpec) -> CardSpec:
    """O que o visor desenha: sem posição de pausa, call, repetir, votação nem disco (PNG, 2×); fila nas 2 pílulas."""
    spec = private(spec)
    radio_state = spec.state in RADIO_STATES
    no_music = radio_state or spec.state is CardState.IDLE
    track = None if no_music else spec.track
    radio = spec.radio._replace(vote=None) if radio_state and spec.radio is not None else None
    return spec._replace(
        scale=VISOR_SCALE,
        motion=False,
        track=track,
        cover=spec.cover if track is not None else None,
        paused_at_s=None,
        voice_name="",
        queue=() if no_music else tuple(_item(item) for item in spec.queue[:NEXT_MAX]),
        queue_total=0 if no_music else spec.queue_total,
        session_played=0 if no_music else spec.session_played,
        flags=spec.flags & DRAWN_FLAGS,
        repeat_left=0,
        radio=radio,
        station=None if no_music else spec.station,
    )


def disc_box(spec: CardSpec) -> tuple[int, int, int, int] | None:
    return None


def disc_tiles(spec: CardSpec, art: CardArt, frames: int) -> list[Image.Image]:
    raise ValueError("o visor da Rádio da Jukebox não tem disco")


# ------------------------------------------------------------------------------------------------- VisorData


def _radio_view(spec: CardSpec, art: CardArt, tr: Translate) -> VisorView:
    radio = spec.radio
    if radio is None:
        raise ValueError("estado R sem os fatos da rádio")
    retry = spec.state is CardState.RADIO_RETRY
    labels = {"visor.on_air": tr("jbx.on_air"), "visor.stations": tr("jbx.on_air").upper()}
    if retry:
        labels["visor.retry"] = fill(tr("jbx.retry"), n=radio.attempt)
    quality = station_quality(radio.bitrate, check_ok=None, unavailable=False)
    data = VisorData(
        station_name=radio.name,
        codec=radio.codec,
        bitrate_kbps=radio.bitrate,
        logo=art.logo,
        state="offline" if retry else "live",
        position=0,
        total=1,
        filter_path=tuple(part for part in (radio.country, radio.place) if part),
        now_title=radio.title,
        volume=spec.volume,
        quality=QUALITIES.get(quality, "good"),
    )
    return VisorView(data, labels)


def _state_word(spec: CardSpec, tr: Translate) -> str:
    keys = {
        CardState.PAUSED: "jbx.state.paused",
        CardState.STREAM: "jbx.state.radio",
        CardState.RADIO_LIVE: "jbx.state.radio",
        CardState.RADIO_RETRY: "jbx.state.reconnecting",
        CardState.IDLE: "jbx.state.idle",
    }
    key = keys.get(spec.state) or ("jbx.state.station" if is_station(spec) else "jbx.state.playing")
    return tr(key).upper()


def _who(spec: CardSpec, tr: Translate) -> str:
    track = spec.track
    if track is None:
        return ""
    if track.by is By.RECOMMENDED:
        return tr("jbx.recommended")
    if track.by is By.STATION:
        return fill(tr("jbx.from_station"), nome=spec.station.name) if spec.station is not None else ""
    return fill(tr("jbx.requested_by"), nome=track.requester) if track.requester else ""


def _origin(spec: CardSpec, tr: Translate) -> tuple[str, ...]:
    track = spec.track
    if spec.station is not None:
        return (spec.station.name,)
    if track is not None and track.by is By.RECOMMENDED:
        return (tr("jbx.chip.autoplay"),)
    return (track.origin,) if track is not None and track.origin else ()


def _logo(spec: CardSpec, art: CardArt) -> bytes | None:
    """A capa vai no lugar do logo do LCD (como PNG, o único formato que o visor abre); sem capa, o logo do bot."""
    cover = disc.cover_image(art.cover) if disc.uses_cover(spec) else None
    return encode_png(cover) if cover is not None else art.logo


def _title(spec: CardSpec, tr: Translate, book: FontBook) -> str:
    if spec.state is CardState.IDLE:
        return tr("jbx.idle.title")
    if spec.hidden or spec.track is None:
        return tr("jbx.hidden_track") if spec.hidden else tr("jbx.idle.title")
    title = spec.track.title
    return tr("jbx.title_in_text") if book.runs(title, *VISOR_TEXT_CHECK).in_message else title


def _music_labels(spec: CardSpec, tr: Translate) -> dict[str, str]:
    word = _state_word(spec, tr)
    track = spec.track
    length = NO_VALUE
    if track is not None:
        length = tr("jbx.state.radio") if track.duration_s is None else clock(track.duration_s)
    chips = " · ".join(tr(key) for flag, key in CHIP_FLAGS if flag in spec.flags)
    labels = {
        "visor.live": word,
        "visor.offline": word,
        "visor.on_air": length,
        "visor.stations": fill(tr("jbx.up_next"), n=spec.queue_total).upper(),
        "visor.filter": tr("jbx.origin").upper(),
        "visor.no_reserve": spec.bot_name or NO_VALUE,
        "visor.quality.good": chips,
    }
    if track is not None:
        labels["visor.now_playing"] = tr("jbx.artist").upper()
    if spec.state is CardState.IDLE:
        labels |= {
            "visor.no_signal": word,
            "visor.reconnecting": tr("jbx.idle.title"),
            "visor.retry": tr("jbx.idle.hint"),
        }
    return labels


def _music_view(spec: CardSpec, art: CardArt, tr: Translate, book: FontBook) -> VisorView:
    position = spec.session_played + 1
    pills = tuple(NextStation(position + i + 1, item.title, 0, "good") for i, item in enumerate(spec.queue[:NEXT_MAX]))
    data = VisorData(
        station_name=_title(spec, tr, book),
        codec=spec.track.source if spec.track is not None else "",
        logo=_logo(spec, art),
        state="offline" if spec.state is CardState.IDLE else "live",
        position=0 if spec.state is CardState.IDLE else position,
        total=position + spec.queue_total,
        filter_path=_origin(spec, tr),
        now_title=spec.track.artists if spec.track is not None else "",
        now_artist=_who(spec, tr),
        volume=spec.volume,
        next_stations=pills,
    )
    return VisorView(data, _music_labels(spec, tr))


def visor_view(spec: CardSpec, art: CardArt, tr: Translate, book: FontBook) -> VisorView:
    """A ``VisorData`` e os rótulos de um ``CardSpec`` (projetado aqui): R pelo visor da rádio, o resto pela tabela M/S."""
    spec = project(spec)
    if spec.state in RADIO_STATES and spec.radio is not None:
        return _radio_view(spec, art, tr)
    return _music_view(spec, art, tr, book)


def translator(tr: Translate, labels: dict[str, str]) -> Callable[..., str]:
    """O tradutor que o ``render_visor`` usa: as chaves trocadas saem prontas; as outras, do catálogo com ``{nome}``."""

    def translate(key: str, **values: object) -> str:
        return labels[key] if key in labels else fill(tr(key), **values)

    return translate


def draw(spec: CardSpec, art: CardArt, tr: Translate, book: FontBook) -> Image.Image:
    """O visor em RGB (1320×432). O PNG do visor tem cantos transparentes; o RGB os pinta na cor ``card`` do tema."""
    view = visor_view(spec, art, tr, book)
    skin = LIGHT_THEME_NAME if spec.theme == LIGHT_THEME_NAME else DEFAULT_THEME
    png = render_visor(view.data, translator(tr, view.labels), skin)
    with Image.open(io.BytesIO(png), formats=["PNG"]) as opened:
        picture = opened.convert("RGBA")
    out = Image.new("RGB", picture.size, load_radio_skin(skin).tokens["card"])
    out.paste(picture, (0, 0), picture)
    return out
