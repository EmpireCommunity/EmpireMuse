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
WHOLE_MAX = (
    1_000_000  # teto de número de fora (bitrate, tentativa, votos, duração em segundos): acima disso é cosmético
)
WHOLE_DIGITS_MAX = 32

QUEUE_FACTS_MAX = 8
SESSION_PLAYED_MAX = 5  # a escala da Rádio da Jukebox

# Formato e degradação (os números do orçamento vivem aqui por serem regra pura; ``utils/render/budget.py`` os reexporta).
DEGRADE_AT = 3  # renders esperando a vaga a partir dos quais sai o PNG

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


# ------------------------------------------------------------------------------------------------------------- texto


def card_text(value: object, limit: int) -> str:
    """Texto de fora pronto para a imagem: o ``external_text`` sem escape de markdown (a imagem não interpreta markdown)."""
    return external_text(value, limit, markdown=False)


def _whole(value: object) -> int:
    """Um inteiro de 0 a ``WHOLE_MAX`` de um valor de fora; o que não é número vira 0 e o que passa do teto vira o teto."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return 0
    if isinstance(value, str) and len(value) > WHOLE_DIGITS_MAX:
        return 0
    try:
        return min(max(0, int(value)), WHOLE_MAX)
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
        codec=card_text(card_text(codec, CODEC_MAX).upper(), CODEC_MAX),  # ``upper`` pode alongar (ß → SS)
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
    """As invariantes que nenhuma skin decide: call privada sem título, artista, capa, call, quem pediu, rádio nem estação
    (só a contagem da fila); posição só na pausa; fila e sessão nos tetos; nome do bot e da call (editáveis no servidor)
    pelo ``card_text``."""
    queue = spec.queue[:QUEUE_FACTS_MAX]
    sealed = spec._replace(
        queue=queue,
        queue_total=max(0, spec.queue_total, len(spec.queue)),
        session_played=min(max(0, spec.session_played), SESSION_PLAYED_MAX),
        paused_at_s=spec.paused_at_s if spec.state is CardState.PAUSED else None,
        bot_name=card_text(spec.bot_name, NAME_MAX),
        voice_name=card_text(spec.voice_name, NAME_MAX),
    )
    if spec.hidden:  # como o painel de texto de hoje: nada do que toca, nem a rádio (título ICY) nem a estação
        return sealed._replace(track=None, cover=None, voice_name="", queue=(), radio=None, station=None)
    return sealed


# ------------------------------------------------------------------------------------------------------ formato e anexo


def motion_allowed(disc_motion: bool | None, state: CardState) -> bool:
    """A animação do disco: a configuração do servidor (``None`` = ligada) e o estado tocando (faixa ou transmissão)."""
    return disc_motion is not False and state in (CardState.PLAYING, CardState.STREAM)
