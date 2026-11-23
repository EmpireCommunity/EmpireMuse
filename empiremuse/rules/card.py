"""Os fatos que a imagem de uma skin pode mostrar (``CardSpec``) e as regras puras do render (formato, animação, anexo).

É um modelo de FATOS, não de layout: cada skin escolhe o que desenha e expõe ``project(spec)``, que zera o resto. Tudo é
``NamedTuple`` imutável e comparável por ``==``, só com ``str``, ``int``, ``bool``, ``None``, ``tuple`` e ``frozenset``.

- Texto de fora entra só pelos construtores (``make_track``, ``make_item``, ``make_radio``, ``make_station``, ``card_text``),
  que passam tudo por ``external_text(..., markdown=False)`` já no teto do campo.
- ``seal`` aplica as invariantes de privacidade e de tempo antes de o ``CardSpec`` sair de quem o monta.
- Nada que envelhece entra aqui (posição tocando, "há 45 min"): só a posição de uma pausa (``paused_at_s``).
- O ``CardSpec`` carrega a URL da capa: nunca vai a log, métrica nem mensagem.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import NamedTuple

from empiremuse.rules.text import external_text

RENDER_VERSION = 1  # sobe quando o desenho muda: invalida o cache de quadros num deploy

# Tetos de texto, em unidades UTF-16 do ``external_text``.
TITLE_MAX = 120
ARTISTS_MAX = 120
ORIGIN_MAX = 80
STATION_MAX = 80
NAME_MAX = 60
CODEC_MAX = 8

QUEUE_FACTS_MAX = 8
SESSION_PLAYED_MAX = 5  # a escala da Rádio da Jukebox

# Formato e degradação (os números do orçamento vivem aqui por serem regra pura; ``utils/render/budget.py`` os reexporta).
DEGRADE_AT = 3  # renders esperando a vaga a partir dos quais sai o PNG
COLD_GIF_GAP_S = 3.0  # no máximo um GIF frio por player a cada tantos segundos
DISC_MAX_PX_1X = 280  # disco animado só até este lado, em pixels do arquivo

SOURCES = frozenset({"YT", "SC", "SP", "AM", "DZ", "TD", "BC", "TW", "WEB"})
UNKNOWN_SOURCE = "WEB"


class CardState(StrEnum):
    PLAYING = "playing"
    PAUSED = "paused"
    STREAM = "stream"
    RADIO_LIVE = "radio_live"
    RADIO_RETRY = "radio_retry"
    IDLE = "idle"


class By(StrEnum):
    MEMBER = "member"
    RECOMMENDED = "recommended"
    STATION = "station"


class CardFormat(StrEnum):
    GIF = "gif"
    PNG = "png"


class CardTrack(NamedTuple):
    title: str
    artists: str
    origin: str  # álbum ou playlist; "" se nada
    source: str  # sigla de ``SOURCES``, nunca texto de fora
    duration_s: int | None  # None = transmissão ao vivo
    requester: str  # nome de exibição (sem menção); "" se ``by`` não é ``MEMBER``
    by: By


class CardItem(NamedTuple):
    title: str
    artists: str
    duration_s: int | None
    requester: str


class CardRadio(NamedTuple):
    """Cópia da ``RadioView`` (contrato J §1.5.1), sem favicon: o logo é só o PNG do ``LogoService``, pela chave."""

    name: str
    title: str
    country: str
    place: str
    codec: str
    bitrate: int
    attempt: int
    on_air: bool
    vote: tuple[int, int] | None
    logo_key: str | None


class CardStation(NamedTuple):
    name: str
    kind: str


class CardSpec(NamedTuple):
    skin: str
    theme: str
    scale: int
    render_version: int
    locale: str
    state: CardState
    hidden: bool  # call privada
    track: CardTrack | None
    cover: str | None  # URL exata pós-yarl; None em call privada, no modo R e ocioso
    paused_at_s: int | None  # só em PAUSED
    queue_total: int
    queue: tuple[CardItem, ...]
    queue_kind: str  # "queue" | "recommended" | "empty"
    session_played: int
    flags: frozenset[str]
    repeat_left: int
    volume: int
    bot_name: str
    voice_name: str
    radio: CardRadio | None
    station: CardStation | None
    motion: bool  # a animação é permitida (``motion_allowed``)


class RenderedKey(NamedTuple):
    """O que define os bytes do anexo: a projeção da skin, os pixels da capa (``None`` = logo) e o formato."""

    spec: CardSpec
    cover_digest: str | None
    fmt: CardFormat


class FormatChoice(NamedTuple):
    fmt: CardFormat
    upgrade_due: bool  # o GIF foi adiado (pressão ou teto por player): tentar de novo no próximo ciclo do updater


# ------------------------------------------------------------------------------------------------------------- texto


def card_text(value: object, limit: int) -> str:
    """Texto de fora pronto para a imagem: o ``external_text`` sem escape de markdown (a imagem não interpreta markdown)."""
    return external_text(value, limit, markdown=False)


def _whole(value: object) -> int:
    """Um inteiro não negativo de um valor de fora; o que não é número vira 0."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return 0
    try:
        return max(0, int(value))
    except ValueError, OverflowError:
        return 0


def _duration(value: object) -> int | None:
    seconds = _whole(value)
    return seconds if seconds > 0 else None


def make_track(
    *,
    title: object,
    artists: object,
    origin: object = "",
    source: object = UNKNOWN_SOURCE,
    duration_s: object = None,
    requester: object = "",
    by: By = By.MEMBER,
) -> CardTrack:
    code = str(source or "").upper()
    return CardTrack(
        title=card_text(title, TITLE_MAX),
        artists=card_text(artists, ARTISTS_MAX),
        origin=card_text(origin, ORIGIN_MAX),
        source=code if code in SOURCES else UNKNOWN_SOURCE,
        duration_s=_duration(duration_s),
        requester=card_text(requester, NAME_MAX) if by is By.MEMBER else "",
        by=by,
    )


def make_item(*, title: object, artists: object, duration_s: object = None, requester: object = "") -> CardItem:
    return CardItem(
        title=card_text(title, TITLE_MAX),
        artists=card_text(artists, ARTISTS_MAX),
        duration_s=_duration(duration_s),
        requester=card_text(requester, NAME_MAX),
    )


def make_radio(
    *,
    name: object,
    title: object = "",
    country: object = "",
    place: object = "",
    codec: object = "",
    bitrate: object = 0,
    attempt: object = 0,
    on_air: bool = False,
    vote: tuple[object, object] | None = None,
    logo_key: str | None = None,
) -> CardRadio:
    """O título ICY chega do ``clean_title`` com escapes de markdown; o ``external_text(markdown=False)`` limpa de novo."""
    return CardRadio(
        name=card_text(name, STATION_MAX),
        title=card_text(title, TITLE_MAX),
        country=card_text(country, NAME_MAX),
        place=card_text(place, NAME_MAX),
        codec=card_text(codec, CODEC_MAX).upper(),
        bitrate=_whole(bitrate),
        attempt=_whole(attempt),
        on_air=bool(on_air),
        vote=(_whole(vote[0]), _whole(vote[1])) if vote is not None else None,
        logo_key=logo_key or None,
    )


def make_station(*, name: object, kind: object) -> CardStation:
    return CardStation(name=card_text(name, STATION_MAX), kind=card_text(kind, NAME_MAX))


# ---------------------------------------------------------------------------------------------------------- invariantes


def seal(spec: CardSpec) -> CardSpec:
    """As invariantes que nenhuma skin decide: call privada sem título, artista, capa, call nem quem pediu (só a contagem
    da fila); posição só na pausa; fila e sessão nos tetos."""
    queue = spec.queue[:QUEUE_FACTS_MAX]
    sealed = spec._replace(
        queue=queue,
        queue_total=max(0, spec.queue_total, len(spec.queue)),
        session_played=min(max(0, spec.session_played), SESSION_PLAYED_MAX),
        paused_at_s=spec.paused_at_s if spec.state is CardState.PAUSED else None,
    )
    if spec.hidden:
        return sealed._replace(track=None, cover=None, voice_name="", queue=())
    return sealed


# ------------------------------------------------------------------------------------------------------ formato e anexo


def motion_allowed(disc_motion: bool | None, state: CardState) -> bool:
    """A animação do disco: a configuração do servidor (``None`` = ligada) e o estado tocando (faixa ou transmissão)."""
    return disc_motion is not False and state in (CardState.PLAYING, CardState.STREAM)


def can_animate(disc_box: tuple[float, float, float, float], scale: int) -> bool:
    """O disco desta área cabe no orçamento do GIF? ``disc_box`` em coordenadas lógicas (1×) e ``scale`` a do arquivo:
    o lado em pixels do arquivo não passa de ``DISC_MAX_PX_1X`` (medido em 1×; um layout maior vira PNG até nova medição)."""
    x0, y0, x1, y1 = disc_box
    side = max(x1 - x0, y1 - y0) * max(1, int(scale))
    return 0 < side <= DISC_MAX_PX_1X


def cold_gif_allowed(now: float, last_cold_gif_at: float | None, gap: float = COLD_GIF_GAP_S) -> bool:
    """O teto por player: um GIF frio (sem quadros em cache) só depois de ``gap`` segundos do anterior."""
    return last_cold_gif_at is None or now - last_cold_gif_at >= gap


def choose_format(
    *,
    motion: bool,
    animatable: bool,
    pressure: int,
    cold: bool,
    now: float,
    last_cold_gif_at: float | None,
) -> FormatChoice:
    """GIF só se a animação é permitida, o disco cabe, a vaga não está sob pressão e, num render frio, o teto por player
    deixa. Adiado por pressão ou pelo teto: PNG agora e ``upgrade_due`` (o GIF vem no próximo ciclo do updater)."""
    if not motion or not animatable:
        return FormatChoice(CardFormat.PNG, False)
    if pressure >= DEGRADE_AT:
        return FormatChoice(CardFormat.PNG, True)
    if cold and not cold_gif_allowed(now, last_cold_gif_at):
        return FormatChoice(CardFormat.PNG, True)
    return FormatChoice(CardFormat.GIF, False)


def keep_attachment(want: RenderedKey, last: RenderedKey | None) -> bool:
    """Manter o anexo da mensagem só com a chave IGUAL à última (igualdade de tupla, nunca só o ``hash``)."""
    return last is not None and tuple.__eq__(want, last) is True


def cover_digest(pixels: bytes | None) -> str | None:
    """A identidade dos pixels da capa já reduzida (``None`` = sem capa, o logo). ``sha256``: sem colisão escolhida."""
    return hashlib.sha256(pixels).hexdigest() if pixels is not None else None
