"""Registro de uso e banimento de servidores: regras puras (só biblioteca padrão).

Três assuntos, todos sem I/O:

* **texto de terceiros** (nome de servidor, canal e faixa): ``clean_text`` na gravação e ``render_text`` na exibição;
* **evento de uso**: ``normalize_event`` deixa passar só os campos permitidos (nunca quem usou) e ``summarize`` agrega a janela
  consultada pelo dono;
* **banimento**: ``BanRecord`` e a decisão de sair de um servidor banido (``should_leave`` / ``guilds_to_leave``).
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Container, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_RETENTION_DAYS = 30
MAX_RETENTION_DAYS = 365
SUMMARY_DAYS = 7
RECENT_EVENTS = 15
TOP_CHANNELS = 5

MS_PER_SECOND = 1000
MS_PER_DAY = 24 * 60 * 60 * MS_PER_SECOND
MAX_LATENCY_MS = 60 * 60 * MS_PER_SECOND

NAME_LIMIT = 100  # teto do Discord para nome de servidor e de canal
TITLE_LIMIT = 150
LABEL_LIMIT = 64  # comando, botão e origem da faixa
REASON_LIMIT = 200

KIND_SLASH = "slash"
KIND_PREFIX = "prefix"
KIND_BUTTON = "button"
KIND_JUKEBOX = "jukebox"
KIND_PANEL = "panel"
KIND_PLAYBACK = "playback"
KIND_GUILD = "guild"
_LEGACY_JUKEBOX_KIND = "_".join(("request", "channel"))
COMMAND_KINDS = frozenset({KIND_SLASH, KIND_PREFIX, KIND_BUTTON, KIND_JUKEBOX, KIND_PANEL})
KINDS = COMMAND_KINDS | {KIND_PLAYBACK, KIND_GUILD}

END_REASONS = ("finished", "skipped", "stopped", "error")

ERROR_LABELS = ("cooldown", "busy", "forbidden", "rejected", "invalid_input", "unexpected")
_ERROR_BY_CLASS = {
    "CommandOnCooldown": "cooldown",
    "MaxConcurrencyReached": "busy",
    "NotOwner": "forbidden",
    "MissingPermissions": "forbidden",
    "BotMissingPermissions": "forbidden",
    "MissingRole": "forbidden",
    "MissingAnyRole": "forbidden",
    "NotDJorStaff": "forbidden",
    "NotRequester": "forbidden",
    "MissingVoicePerms": "forbidden",
    "UserInputError": "invalid_input",
    "ArgumentParsingError": "invalid_input",
    "CheckFailure": "rejected",
}

# CSI (cor, cursor), OSC (título, link) e escapes de dois caracteres.
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
# Markdown do Discord (inclui spoiler, cabeçalho, citação, link mascarado e menção); ``:`` desarma o ``://`` do link automático.
_MARKDOWN = re.compile(r"([\\*_~`|>#\[\]()<@:])")
_ZERO_WIDTH_SPACE = "​"
_ELLIPSIS = "…"


def clean_text(value: object, limit: int) -> str:
    """Texto de terceiros para gravar: sem ANSI nem controle, uma linha só, no máximo ``limit`` caracteres.

    Controle (``Cc``) vira espaço e formatação (``Cf``, como U+202E e U+200B) some; o excesso é cortado com ``…``.
    """
    if value is None:
        return ""
    text = _ANSI.sub("", str(value))
    kept = []
    for char in text:
        category = unicodedata.category(char)
        if category == "Cf":
            continue
        kept.append(" " if category == "Cc" else char)
    text = " ".join("".join(kept).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + _ELLIPSIS


def escape_markdown(text: str) -> str:
    """Escapa o markdown do Discord e separa o ``@`` do que vem depois (``@everyone`` não vira menção)."""
    return _MARKDOWN.sub(r"\\\1", text).replace("@", "@" + _ZERO_WIDTH_SPACE)


def render_text(value: object, limit: int) -> str:
    """Texto de terceiros para exibir: ``clean_text`` seguido de ``escape_markdown`` (vale mesmo para dado já gravado)."""
    return escape_markdown(clean_text(value, limit))


def fit_lines(lines: Iterable[str], limit: int) -> str:
    """Junta as linhas com quebra, sem ultrapassar ``limit`` caracteres e sem cortar nenhuma linha no meio."""
    out: list[str] = []
    used = 0
    for line in lines:
        extra = len(line) + (1 if out else 0)
        if used + extra > limit:
            break
        out.append(line)
        used += extra
    return "\n".join(out)


def clamp_retention_days(raw: object) -> int:
    """Dias de retenção do registro de uso: inteiro de 1 a ``MAX_RETENTION_DAYS``; qualquer outra coisa vale o padrão."""
    try:
        days = int(raw)  # type: ignore[call-overload]
    except TypeError, ValueError, OverflowError:
        return DEFAULT_RETENTION_DAYS
    if days < 1:
        return DEFAULT_RETENTION_DAYS
    return min(days, MAX_RETENTION_DAYS)


def end_reason(raw: object) -> str:
    """Motivo de fim da faixa (``FINISHED``, ``TrackEndReason.REPLACED``…) como um dos ``END_REASONS``."""
    name = str(raw or "").rsplit(".", 1)[-1].strip().upper()
    if name == "FINISHED":
        return "finished"
    if name == "REPLACED":
        return "skipped"
    if name == "LOAD_FAILED":
        return "error"
    return "stopped"


def error_label(class_names: Iterable[str]) -> str:
    """Rótulo fixo do erro a partir dos nomes das classes da exceção (a mais específica primeiro); nunca o texto dela."""
    for name in class_names:
        if label := _ERROR_BY_CLASS.get(name):
            return label
    return "unexpected"


def _int(value: object, *, low: int = 0, high: int | None = None) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = int(value)
    if number < low:
        return None
    return number if high is None else min(number, high)


_TEXT_FIELDS = {
    "guild_name": NAME_LIMIT,
    "text_name": NAME_LIMIT,
    "voice_name": NAME_LIMIT,
    "name": LABEL_LIMIT,
    "title": TITLE_LIMIT,
    "author": NAME_LIMIT,
    "source": LABEL_LIMIT,
    "via": LABEL_LIMIT,
}
_ID_FIELDS = ("bot", "guild", "text_id", "voice_id")


def normalize_event(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """O evento só com os campos permitidos, tipados e limpos; ``None`` se faltar o essencial (``id``, ``ts``, ``kind``).

    Tudo o que não está na lista (autor, usuário, texto da busca…) é descartado aqui: o registro não guarda quem usou.
    """
    event_id = raw.get("id")
    ts = _int(raw.get("ts"))
    kind = raw.get("kind")
    if kind == _LEGACY_JUKEBOX_KIND:
        kind = KIND_JUKEBOX
    if not isinstance(event_id, str) or not event_id or ts is None or kind not in KINDS:
        return None

    event: dict[str, Any] = {"id": event_id[:64], "ts": ts, "kind": kind}
    for field in _ID_FIELDS:
        if (number := _int(raw.get(field), low=1)) is not None:
            event[field] = number
    for field, limit in _TEXT_FIELDS.items():
        if text := clean_text(raw.get(field), limit):
            event[field] = text

    if isinstance(ok := raw.get("ok"), bool):
        event["ok"] = ok
        if not ok:
            label = raw.get("error")
            event["error"] = label if label in ERROR_LABELS else "unexpected"
    if (latency := _int(raw.get("ms"), high=MAX_LATENCY_MS)) is not None:
        event["ms"] = latency
    if (length := _int(raw.get("length"))) is not None:
        event["length"] = length
    if (listened := _int(raw.get("listened"))) is not None:
        event["listened"] = listened
    if raw.get("end") in END_REASONS:
        event["end"] = raw["end"]
    return event


@dataclass(frozen=True)
class ChannelCount:
    id: int
    name: str
    count: int


@dataclass(frozen=True)
class UsageSummary:
    commands: int
    tracks: int
    listened_seconds: int
    last_ts: int | None
    top_text: tuple[ChannelCount, ...]
    top_voice: tuple[ChannelCount, ...]
    recent: tuple[dict[str, Any], ...]


def _top(events: list[dict[str, Any]], id_field: str, name_field: str, top: int) -> tuple[ChannelCount, ...]:
    """Os canais mais usados; o nome mostrado é o do evento mais recente daquele canal."""
    counts: Counter[int] = Counter()
    names: dict[int, str] = {}
    for event in sorted(events, key=lambda e: e["ts"]):
        if (channel := event.get(id_field)) is None:
            continue
        counts[channel] += 1
        names[channel] = event.get(name_field) or str(channel)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return tuple(ChannelCount(channel, names[channel], count) for channel, count in ranked[:top])


def summarize(
    events: Iterable[Mapping[str, Any]],
    *,
    now_ms: int,
    days: int = SUMMARY_DAYS,
    recent: int = RECENT_EVENTS,
    top: int = TOP_CHANNELS,
) -> UsageSummary:
    """Resumo dos últimos ``days`` dias: comandos, faixas, tempo ouvido, última atividade, canais mais usados e eventos recentes.

    Eventos fora da janela ou malformados são ignorados. Comando conta ``COMMAND_KINDS``, faixa conta ``playback``; entrada e saída de
    servidor aparecem nos recentes, mas não contam como uso nem como última atividade.
    """
    since = now_ms - days * MS_PER_DAY
    window: list[dict[str, Any]] = []
    for raw in events:
        event = normalize_event(raw)
        if event is not None and since <= event["ts"] <= now_ms:
            window.append(event)

    used = [e for e in window if e["kind"] != KIND_GUILD]
    playbacks = [e for e in window if e["kind"] == KIND_PLAYBACK]
    newest_first = sorted(window, key=lambda e: (e["ts"], e["id"]), reverse=True)
    return UsageSummary(
        commands=sum(1 for e in window if e["kind"] in COMMAND_KINDS),
        tracks=len(playbacks),
        listened_seconds=sum(e.get("listened", 0) for e in playbacks),
        last_ts=max((e["ts"] for e in used), default=None),
        top_text=_top([e for e in window if e["kind"] in COMMAND_KINDS], "text_id", "text_name", top),
        top_voice=_top(playbacks, "voice_id", "voice_name", top),
        recent=tuple(newest_first[:recent]),
    )


@dataclass(frozen=True)
class BanRecord:
    guild_id: int
    name: str
    banned_at: int  # milissegundos, UTC
    reason: str


def new_ban_record(guild_id: int, name: object, reason: object, now_ms: int) -> BanRecord:
    """O registro de banimento com o nome (no momento do ban) e o motivo já limpos."""
    return BanRecord(guild_id, clean_text(name, NAME_LIMIT), now_ms, clean_text(reason, REASON_LIMIT))


def ban_to_doc(record: BanRecord) -> dict[str, Any]:
    return {"name": record.name, "banned_at": record.banned_at, "reason": record.reason}


def ban_from_doc(doc: Mapping[str, Any]) -> BanRecord | None:
    """O banimento lido do banco; ``None`` se o ``_id`` não for um ID de servidor (dado de fora nunca é confiável)."""
    raw_id = doc.get("_id")
    if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)):
        return None
    try:
        guild_id = int(raw_id)
    except ValueError:
        return None
    if guild_id < 1:
        return None
    banned_at = _int(doc.get("banned_at")) or 0
    return BanRecord(
        guild_id, clean_text(doc.get("name"), NAME_LIMIT), banned_at, clean_text(doc.get("reason"), REASON_LIMIT)
    )


def should_leave(guild_id: int, banned_ids: Container[int]) -> bool:
    """Ao entrar em um servidor: sair se ele está banido."""
    return guild_id in banned_ids


def guilds_to_leave(joined_ids: Iterable[int], banned_ids: Container[int]) -> list[int]:
    """Ao ficar pronto: os servidores em que o bot ainda está e que foram banidos."""
    return [guild_id for guild_id in joined_ids if guild_id in banned_ids]
