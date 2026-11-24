"""Os fatos do cartão de uma skin de imagem (``CardSpec``, contrato §1) lidos do player, mais as regras puras do texto que
acompanha a imagem (barra de texto da opção C, texto alternativo e título no autor do embed).

- ``build(player, settings)`` é síncrono, sem ``await`` e sem I/O: lê o player, o modo rádio pela cópia imutável
  ``JukeboxRadio.view(player)`` (nunca ``player.radio_mode``) e devolve o ``CardSpec`` já selado (``rules.card.seal``).
- **Call privada:** um ponto só, ``jukebox_panel.player_hides_details``: oculta numa call privada (ou sem call conhecida),
  salvo com o player no chat de texto da própria call.
- **Estação do servidor (S):** pela cópia ``JukeboxRadio.station_view(player)`` (J2b); a faixa que veio da estação sai
  com ``By.STATION`` (sem quem pediu).
- **Nada que envelhece:** a posição só entra na pausa (``paused_at_s``); o tempo vivo vai ao embed como ``<t:…:R>``.
- **Texto de fora:** todo campo passa pelos construtores de ``rules.card`` (``external_text(markdown=False)`` no teto).
"""

from __future__ import annotations

import math
from itertools import islice
from typing import Any, Callable, NamedTuple, Optional

from empiremuse.rules.card import (
    QUEUE_FACTS_MAX,
    RENDER_VERSION,
    UNKNOWN_SOURCE,
    By,
    CardItem,
    CardRadio,
    CardSpec,
    CardState,
    CardTrack,
    card_text,
    make_item,
    make_radio,
    make_station,
    make_track,
    motion_allowed,
    seal,
)
from empiremuse.rules.skins import disc_motion_on
from empiremuse.utils.jukebox_panel import player_hides_details, player_voice_channel
from empiremuse.utils.music.cover_fetch import cover_url

# A sigla fixa da fonte (``sourceName`` do Lavalink); o que não está aqui vira ``WEB``.
SOURCE_CODES = {
    "youtube": "YT",
    "soundcloud": "SC",
    "spotify": "SP",
    "applemusic": "AM",
    "deezer": "DZ",
    "tidal": "TD",
    "bandcamp": "BC",
    "twitch": "TW",
}

FLAG_AUTOPLAY = "autoplay"
FLAG_NIGHTCORE = "nightcore"
FLAG_ALWAYS_ON = "24_7"
FLAG_RESTRICTED = "restrict"
FLAG_REPEAT_TRACK = "repeat_track"
FLAG_REPEAT_QUEUE = "repeat_queue"

QUEUE_KIND_QUEUE = "queue"
QUEUE_KIND_RECOMMENDED = "recommended"
QUEUE_KIND_EMPTY = "empty"

# Barra em texto do embed (opção C das respostas do Alicerce): N = 15 s, 12 segmentos, edição só quando o segmento muda.
TEXT_BAR_SEGMENTS = 12
TEXT_BAR_MIN_INTERVAL_S = 15
TEXT_BAR_PLAYERS_PER_SECOND = 5  # players tocando no bot por segundo de intervalo, acima do mínimo
EDIT_STOP_STATUSES = frozenset({403, 404})  # no primeiro destes, o player para de editar aquela mensagem

ALT_TEXT_MAX = 1024  # ``disnake.File(description=…)``
AUTHOR_MAX = 256  # autor do embed

Translate = Callable[..., str]


class CardSettings(NamedTuple):
    """O que vem da configuração, não do player: a skin já resolvida (registro), o tema dela, o idioma e o valor gravado
    da animação do disco (``None`` = ligada)."""

    skin: str
    theme: str
    locale: str
    disc_motion: object = None
    scale: int = 1


# ---------------------------------------------------------------------- montagem


def _jukebox(player: Any) -> Any:
    return getattr(getattr(getattr(getattr(player, "bot", None), "pool", None), "radio", None), "jukebox", None)


def _radio_view(player: Any) -> Any:
    jukebox = _jukebox(player)
    return jukebox.view(player) if jukebox is not None else None


def _station_view(player: Any) -> Any:
    read = getattr(_jukebox(player), "station_view", None)
    return read(player) if read is not None else None


def _member_name(player: Any, member_id: object) -> str:
    if not str(member_id).isdigit():
        return ""
    member = player.guild.get_member(int(str(member_id)))
    return member.display_name if member is not None else ""


def _seconds(ms: object) -> Optional[int]:
    if isinstance(ms, bool) or not isinstance(ms, int | float) or not math.isfinite(ms) or ms < 0:
        return None
    return int(ms // 1000)


def _state(player: Any, view: Any) -> CardState:
    if view is not None:
        return CardState.RADIO_LIVE if view.on_air else CardState.RADIO_RETRY
    current = getattr(player, "current", None)
    if current is None:
        return CardState.IDLE
    if getattr(player, "paused", False):
        return CardState.PAUSED
    return CardState.STREAM if current.is_stream else CardState.PLAYING


def _track(player: Any, current: Any, from_station: bool) -> CardTrack:
    by = By.STATION if from_station else By.RECOMMENDED if current.autoplay else By.MEMBER
    return make_track(
        title=current.single_title,
        artists=current.authors_string,
        origin=current.album_name or current.playlist_name or "",
        source=SOURCE_CODES.get(str(current.info.get("sourceName", "")), UNKNOWN_SOURCE),
        duration_s=None if current.is_stream else _seconds(current.duration),
        requester=_member_name(player, current.requester) if by is By.MEMBER else "",
        by=by,
    )


def _item(player: Any, track: Any) -> CardItem:
    return make_item(
        title=track.single_title,
        artists=track.authors_string,
        duration_s=None if track.is_stream else _seconds(track.duration),
        requester="" if track.autoplay else _member_name(player, track.requester),
    )


def _head(player: Any, tracks: Any) -> tuple[int, tuple[CardItem, ...]]:
    """O total e só as primeiras ``QUEUE_FACTS_MAX`` faixas: a fila inteira nunca é copiada."""
    return len(tracks), tuple(_item(player, t) for t in islice(tracks, QUEUE_FACTS_MAX))


def _queue(player: Any) -> tuple[str, int, tuple[CardItem, ...]]:
    queue = getattr(player, "queue", ())
    if queue:
        return QUEUE_KIND_QUEUE, *_head(player, queue)
    recommended = getattr(player, "queue_autoplay", ()) if getattr(player, "autoplay", False) else ()
    if recommended:
        return QUEUE_KIND_RECOMMENDED, *_head(player, recommended)
    return QUEUE_KIND_EMPTY, 0, ()


def _flags(player: Any) -> frozenset[str]:
    loop = getattr(player, "loop", False)
    candidates = {
        FLAG_AUTOPLAY: getattr(player, "autoplay", False),
        FLAG_NIGHTCORE: getattr(player, "nightcore", False),
        FLAG_ALWAYS_ON: getattr(player, "keep_connected", False),
        FLAG_RESTRICTED: getattr(player, "restrict_mode", False),
        FLAG_REPEAT_TRACK: loop == "current",
        FLAG_REPEAT_QUEUE: loop == "queue",
    }
    return frozenset(flag for flag, on in candidates.items() if on is True)


def _place(player: Any, node_id: object, locale: str) -> str:
    tree = getattr(getattr(getattr(player.bot, "pool", None), "radio", None), "locations", None)
    node = tree.node(node_id) if tree is not None and node_id else None
    return tree.label(node, locale) if node is not None else ""


def _radio(player: Any, view: Any, locale: str) -> CardRadio:
    vote = view.vote
    return make_radio(
        name=view.name,
        title=view.title,
        country=view.country,
        place=_place(player, view.place_node, locale),
        codec=view.codec,
        bitrate=view.bitrate,
        attempt=view.attempt,
        on_air=view.on_air,
        vote=(vote.votes, vote.needed) if vote is not None else None,
        logo_key=view.uuid or None,
    )


def _cover(current: Any) -> Optional[str]:
    if current is None:
        return None  # modo rádio e ocioso: o rótulo é o logo, nunca uma busca
    url = cover_url(getattr(current, "thumb", None))
    return str(url) if url is not None else None


def build(player: Any, settings: CardSettings) -> CardSpec:
    """O ``CardSpec`` do player agora: síncrono, sem I/O. A skin projeta depois (``project``) o que desenha."""
    view = _radio_view(player)
    station = _station_view(player) if view is None else None
    state = _state(player, view)
    voice = player_voice_channel(player)
    current = getattr(player, "current", None) if view is None else None
    queue_kind, queue_total, queue = _queue(player)
    paused_at = _seconds(getattr(player, "position", None)) if state is CardState.PAUSED else None
    spec = CardSpec(
        skin=settings.skin,
        theme=settings.theme,
        scale=max(1, int(settings.scale)),
        render_version=RENDER_VERSION,
        locale=settings.locale,
        state=state,
        hidden=player_hides_details(player),
        track=_track(player, current, station is not None and station.playing) if current is not None else None,
        cover=_cover(current),
        paused_at_s=paused_at if current is not None and not current.is_stream else None,
        queue_total=queue_total,
        queue=queue,
        queue_kind=queue_kind,
        session_played=len(getattr(player, "played", ())),
        flags=_flags(player),
        repeat_left=int(getattr(current, "track_loops", 0) or 0) if current is not None else 0,
        volume=int(getattr(player, "volume", 100)),
        bot_name=getattr(getattr(player.guild, "me", None), "display_name", ""),
        voice_name=getattr(voice, "name", ""),
        radio=_radio(player, view, settings.locale) if view is not None else None,
        station=make_station(name=station.name, kind=station.kind) if station is not None else None,
        motion=motion_allowed(disc_motion_on(settings.disc_motion), state),
    )
    return seal(spec)


# ---------------------------------------------------------------------- texto que acompanha a imagem


def text_bar_segment(position_ms: object, duration_ms: object) -> Optional[int]:
    """Quantos dos ``TEXT_BAR_SEGMENTS`` segmentos estão cheios (0 a 12). ``None`` = sem barra (transmissão ou duração
    desconhecida). É só a posição da barra: as skins em texto mostram também o decorrido em ``m:ss`` (arredondado a cada 5 s: é a precisão
    do texto, não a frequência), então editam a cada ciclo do atualizador (``text_bar_interval_s``), não só quando este número muda."""
    position, duration = _seconds(position_ms), _seconds(duration_ms)
    if position is None or not duration:
        return None
    return min(TEXT_BAR_SEGMENTS, position * TEXT_BAR_SEGMENTS // duration)


def text_bar_interval_s(playing_on_bot: object) -> int:
    """O intervalo da barra em texto: 15 s, crescendo com os players tocando no mesmo bot (``max(15, n / 5)``)."""
    count = playing_on_bot if isinstance(playing_on_bot, int) and not isinstance(playing_on_bot, bool) else 0
    return max(TEXT_BAR_MIN_INTERVAL_S, math.ceil(max(0, count) / TEXT_BAR_PLAYERS_PER_SECOND))


def stops_editing(status: object) -> bool:
    """A resposta do Discord manda parar de editar esta mensagem (403 ou 404) até o player ter outra."""
    return status in EDIT_STOP_STATUSES


def author_title(spec: CardSpec) -> str:
    """O título real no autor do embed (o Discord desenha escritas que a imagem não cobre). Vazio em call privada."""
    if spec.hidden:
        return ""
    if spec.radio is not None:
        return card_text(spec.radio.title or spec.radio.name, AUTHOR_MAX)
    return card_text(spec.track.title, AUTHOR_MAX) if spec.track is not None else ""


def alt_text(spec: CardSpec, tr: Translate) -> str:
    """O texto alternativo do anexo, do mesmo ``CardSpec`` e pelo mesmo ``card_text``. Call privada: só o rótulo oculto."""
    if spec.hidden:
        return card_text(tr("jbx.card.alt_hidden"), ALT_TEXT_MAX)
    if spec.radio is not None:
        parts = [spec.radio.title, spec.radio.name]
    elif spec.track is not None:
        parts = [spec.track.title, spec.track.artists]
    else:
        parts = [tr("jbx.idle.title")]
    return card_text(" · ".join(p for p in parts if p), ALT_TEXT_MAX)
