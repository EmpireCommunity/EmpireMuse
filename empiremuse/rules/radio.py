"""Regras puras da rádio 24/7 (item 8, R1): o modelo, a validação, o teto por servidor, a presença, o recuo da reconexão e o relógio virtual.

Uma rádio é um bot reserva fixo numa call (voz ou palco), com a fonte (uma playlist do servidor; na R3 uma estação ao vivo) e as regras do
chat da própria call. Aqui só há dados e decisões: o banco, o Discord e o Lavalink são de ``utils/radio``. Toda validação devolve o valor
limpo ou um ``RuleError`` (``code`` estável, ``key`` = o campo); nada aqui monta texto para o usuário.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from empiremuse.rules.bot_roles import identifier_sort_key
from empiremuse.rules.errors import RuleError

KIND_CUSTOM = "custom"
KIND_LIVE = "live"
KINDS = (KIND_CUSTOM, KIND_LIVE)

MAX_PER_GUILD = 3  # decisão do dono: 3 rádios por servidor, contando as pausadas
PREMIUM_MAX = MAX_PER_GUILD
PLAYLIST_NAME_MAX = 100

CHAT_FIELDS = ("voice_delete_after_s", "stage_max_messages", "stage_max_age_s")
CHAT_LIMITS: dict[str, tuple[int, int, int]] = {  # campo: (mínimo, máximo, padrão)
    "voice_delete_after_s": (5, 600, 30),
    "stage_max_messages": (3, 50, 7),
    "stage_max_age_s": (60, 3600, 600),
}

# Códigos de erro (estáveis; quem mostra traduz).
ID_INVALID = "radio_id_invalid"
CHANNEL_INVALID = "radio_channel_invalid"
KIND_INVALID = "radio_kind_invalid"
SOURCE_INVALID = "radio_source_invalid"
CHAT_INVALID = "radio_chat_invalid"
CHAT_UNKNOWN_FIELD = "radio_chat_unknown_field"
ENABLED_INVALID = "radio_enabled_invalid"
REV_INVALID = "radio_rev_invalid"

_SNOWFLAKE = re.compile(r"[0-9]{1,20}")
_RADIO_ID = re.compile(r"([0-9]{1,20}):([0-9]{1,20})")
_STATION_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass(frozen=True)
class Radio:
    """Uma rádio já validada. ``id`` = ``'<bot_id>:<guild_id>'`` (uma por bot e por servidor); ``updated_by`` nunca sai do documento (R-12)."""

    id: str
    bot_id: int
    guild_id: int
    channel_id: int
    kind: str
    source: dict[str, Any]
    chat: dict[str, int]
    enabled: bool = False
    rev: int = 0
    panel_message_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict, compare=False)


def radio_id(bot_id: int, guild_id: int) -> str:
    return f"{int(bot_id)}:{int(guild_id)}"


def split_radio_id(value: object) -> tuple[int, int] | None:
    """``(bot_id, guild_id)`` de um id de rádio, ou ``None`` se não tem o formato."""
    match = _RADIO_ID.fullmatch(value) if isinstance(value, str) else None
    return (int(match.group(1)), int(match.group(2))) if match else None


def belongs_to_guild(value: object, guild_id: int) -> bool:
    """O id da rádio termina em ``:{guild_id}``? A conferência da página web e dos botões (R-6): nunca confiar no que veio de fora."""
    parts = split_radio_id(value)
    return parts is not None and parts[1] == int(guild_id)


def _snowflake(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and _SNOWFLAKE.fullmatch(value) and int(value) > 0:
        return int(value)
    return None


def default_chat() -> dict[str, int]:
    return {name: limits[2] for name, limits in CHAT_LIMITS.items()}


def check_chat(raw: object, *, base: Mapping[str, int] | None = None) -> dict[str, int] | RuleError:
    """As regras do chat: só os 3 campos conhecidos, inteiros (nunca booleano) dentro dos limites. O que falta vem de ``base`` (ou do padrão)."""
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        return RuleError(CHAT_INVALID, "chat")
    unknown = next((key for key in raw if key not in CHAT_LIMITS), None)
    if unknown is not None:
        return RuleError(CHAT_UNKNOWN_FIELD, str(unknown)[:40])
    chat = {**default_chat(), **(base or {})}
    for name, value in raw.items():
        low, high, _ = CHAT_LIMITS[name]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            return RuleError(CHAT_INVALID, name, limit=high)
        chat[name] = value
    return chat


def check_source(kind: str, raw: object) -> dict[str, Any] | RuleError:
    """A fonte: ``{playlist}`` na personalizada; ``{station, reserve}`` na ao vivo (a R3 usa; aqui só se confere o formato)."""
    if not isinstance(raw, Mapping):
        return RuleError(SOURCE_INVALID, "source")
    if kind == KIND_CUSTOM:
        playlist = raw.get("playlist")
        if set(raw) != {"playlist"} or not isinstance(playlist, str):
            return RuleError(SOURCE_INVALID, "source")
        name = playlist.strip()
        if not 1 <= len(name) <= PLAYLIST_NAME_MAX or any(ord(c) < 32 or ord(c) == 127 for c in name):
            return RuleError(SOURCE_INVALID, "playlist")
        return {"playlist": name}
    station, reserve = raw.get("station"), raw.get("reserve")
    if set(raw) - {"station", "reserve"} or not isinstance(station, str) or not _STATION_UUID.fullmatch(station):
        return RuleError(SOURCE_INVALID, "station")
    if reserve is not None and not (isinstance(reserve, str) and _STATION_UUID.fullmatch(reserve)):
        return RuleError(SOURCE_INVALID, "reserve")
    return {"station": station, "reserve": reserve}


def _identity(raw: Mapping[str, Any]) -> tuple[int, int] | None:
    """``(bot_id, guild_id)`` do ``_id``/``id``; se o mapping traz ``bot_id``/``guild_id`` também, eles precisam concordar com o id."""
    parts = split_radio_id(raw.get("_id", raw.get("id")))
    if parts is None:
        return None
    for position, name in enumerate(("bot_id", "guild_id")):
        if name in raw and _snowflake(raw[name]) != parts[position]:
            return None
    return parts


def check_radio(raw: object) -> Radio | RuleError:
    """O documento (ou dicionário) de uma rádio validado; ``RuleError(code, campo)`` no primeiro defeito. Campo desconhecido é ignorado."""
    if not isinstance(raw, Mapping):
        return RuleError(ID_INVALID, "id")
    parts = _identity(raw)
    if parts is None:
        return RuleError(ID_INVALID, "id")
    bot_id, guild_id = parts
    channel_id = _snowflake(raw.get("channel_id"))
    if channel_id is None:
        return RuleError(CHANNEL_INVALID, "channel_id")
    kind = raw.get("kind")
    if kind not in KINDS:
        return RuleError(KIND_INVALID, "kind")
    source = check_source(kind, raw.get("source"))
    if isinstance(source, RuleError):
        return source
    chat = check_chat(raw.get("chat"))
    if isinstance(chat, RuleError):
        return chat
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        return RuleError(ENABLED_INVALID, "enabled")
    rev = raw.get("rev", 0)
    if isinstance(rev, bool) or not isinstance(rev, int) or rev < 0:
        return RuleError(REV_INVALID, "rev")
    panel = raw.get("panel_message_id")
    panel_id = None if panel in (None, "") else _snowflake(panel)
    if panel not in (None, "") and panel_id is None:
        return RuleError(ID_INVALID, "panel_message_id")
    return Radio(
        id=radio_id(bot_id, guild_id),
        bot_id=bot_id,
        guild_id=guild_id,
        channel_id=channel_id,
        kind=kind,
        source=source,
        chat=chat,
        enabled=enabled,
        rev=rev,
        panel_message_id=panel_id,
    )


def to_document(radio: Radio, *, updated_by: str = "") -> dict[str, Any]:
    """O documento do banco (ids como texto, para não perder precisão em JSON); ``updated_by`` só aqui, nunca de volta no ``Radio``."""
    return {
        "_id": radio.id,
        "bot_id": str(radio.bot_id),
        "guild_id": str(
            radio.guild_id
        ),  # para filtrar no banco; o ``_id`` continua sendo a verdade (``check_radio`` confere)
        "channel_id": str(radio.channel_id),
        "kind": radio.kind,
        "source": dict(radio.source),
        "chat": dict(radio.chat),
        "enabled": radio.enabled,
        "panel_message_id": None if radio.panel_message_id is None else str(radio.panel_message_id),
        "rev": radio.rev,
        "updated_by": str(updated_by)[:40],
    }


# ---------------------------------------------------------------------- qual bot toca a rádio


class BotCandidate(NamedTuple):
    """Um bot do pool visto de um servidor. ``busy`` = já tem player ou está numa call aí; ``has_radio_here`` = já roda uma rádio deste servidor."""

    identifier: str
    bot_id: int
    main: bool
    radio_only: bool
    ready: bool
    in_guild: bool
    busy: bool
    has_radio_here: bool = False


def pick_radio_bot(candidates: Iterable[BotCandidate]) -> int | None:
    """O bot que vai tocar a rádio: primeiro um reserva ``radio_only`` livre, depois qualquer reserva livre (ordem natural do identificador).

    Nunca o principal, nunca um bot que já está em call no servidor, nem um que já roda uma rádio aqui (uma rádio por bot e servidor).
    """
    usable = [c for c in candidates if c.ready and c.in_guild and not c.main and not c.busy and not c.has_radio_here]
    usable.sort(key=lambda c: (not c.radio_only, identifier_sort_key(c.identifier)))
    return usable[0].bot_id if usable else None


# ---------------------------------------------------------------------- teto por servidor (R-2)


def has_room(active: int, cap: int = MAX_PER_GUILD) -> bool:
    """Cabe mais uma rádio ligada neste servidor? (``active`` conta as ligadas, pausadas incluídas.)"""
    return 0 <= active < cap


def clamp_active(value: object) -> int:
    """O contador lido do banco como inteiro dentro de 0..teto (lixo vira 0: o recálculo na subida corrige)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, min(PREMIUM_MAX, value))


# ---------------------------------------------------------------------- presença e reconexão (R-5)

ABSENCE_PAUSE_S = 30  # sem ouvinte por 30 s: a rádio pausa
PRESENCE_RESUME_S = 5  # ouvinte presente por 5 s: a rádio retoma
BACKOFF_STEPS_S = (5, 10, 20, 40, 60)
BACKOFF_JITTER = 0.2


def should_pause(empty_since: float | None, now: float) -> bool:
    """A call está sem ouvinte (que não esteja ensurdecido) há ``ABSENCE_PAUSE_S`` ou mais?"""
    return empty_since is not None and now - empty_since >= ABSENCE_PAUSE_S


def should_resume(listening_since: float | None, now: float) -> bool:
    return listening_since is not None and now - listening_since >= PRESENCE_RESUME_S


def backoff_s(attempt: int, unit: float = 0.5) -> float:
    """Quanto esperar antes da tentativa ``attempt`` (0 = a primeira queda): 5, 10, 20, 40, 60 e depois 60 s, com ±20 % de jitter.

    ``unit`` é um número em [0, 1] (o sorteio, injetável para testar): 0,5 = sem jitter. Nunca há volta imediata.
    """
    base = BACKOFF_STEPS_S[min(max(attempt, 0), len(BACKOFF_STEPS_S) - 1)]
    spread = max(0.0, min(1.0, unit))
    return round(base * (1 - BACKOFF_JITTER + 2 * BACKOFF_JITTER * spread), 3)


# ---------------------------------------------------------------------- varredura do chat (R-4)

STAGE_SWEEP_MIN_S = 30
SWEEP_BATCH = 100
SWEEP_MAX_AGE_S = 14 * 24 * 3600  # o Discord só apaga em lote mensagens com menos de 14 dias


def voice_sweep_interval_s(delete_after_s: int) -> float:
    """De quanto em quanto tempo varrer o chat de uma call de voz: ``max(5, after/2)``."""
    return max(5.0, delete_after_s / 2)


def stage_sweep_interval_s() -> float:
    return float(STAGE_SWEEP_MIN_S)


def sweep_victims(
    messages: Sequence[tuple[int, float, bool]], now: float, *, stage: bool, chat: Mapping[str, int]
) -> list[int]:
    """Quais mensagens apagar. ``messages``: ``(id, criada_em, protegida)`` (a protegida é a fixada, o painel ou o aviso do bot: nunca sai).

    Na voz: as mais velhas que ``voice_delete_after_s``. No palco: as mais velhas que ``stage_max_age_s`` e, sobrando mais de
    ``stage_max_messages``, as mais antigas até caber. Nunca passa de ``SWEEP_BATCH`` nem inclui mensagem com 14 dias ou mais.
    """
    candidates = [(created, mid) for mid, created, protected in messages if not protected]
    candidates.sort()
    if stage:
        age = chat["stage_max_age_s"]
        old = [pair for pair in candidates if now - pair[0] >= age]
        rest = [pair for pair in candidates if now - pair[0] < age]
        excess = max(0, len(rest) - chat["stage_max_messages"])
        chosen = old + rest[:excess]
    else:
        chosen = [pair for pair in candidates if now - pair[0] >= chat["voice_delete_after_s"]]
    chosen = [pair for pair in chosen if now - pair[0] < SWEEP_MAX_AGE_S]
    return [mid for _, mid in chosen[:SWEEP_BATCH]]


# ---------------------------------------------------------------------- relógio virtual (rádio personalizada, Q3)


def virtual_position(durations_ms: Sequence[int], elapsed_ms: int) -> tuple[int, int]:
    """``(índice da faixa, posição em ms dentro dela)`` depois de ``elapsed_ms`` de programação virtual, dando a volta na lista.

    Sem faixas (ou com duração total zero) devolve ``(0, 0)``. A rádio que pausou por falta de ouvinte volta nesta posição, como se
    tivesse continuado a tocar.
    """
    positive = [max(0, int(d)) for d in durations_ms]
    total = sum(positive)
    if total <= 0:
        return 0, 0
    rest = max(0, int(elapsed_ms)) % total
    for index, duration in enumerate(positive):
        if rest < duration:
            return index, rest
        rest -= duration
    return 0, 0
