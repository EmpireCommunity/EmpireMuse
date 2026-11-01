"""Métricas em memória do player e resumo periódico de uma linha (item 7, 3.9 e 3.12).

Sem I/O e sem relógio próprio: ``snapshot_and_reset`` recebe o ``now``. Tudo é contado por inteiros; o ``kind`` de erro é rótulo
fixo (``ERROR_KINDS``), nunca texto de exceção, então nada de terceiros vira chave nem linha.
"""

from __future__ import annotations

import dataclasses
import math
import threading
from collections.abc import Mapping, Sequence
from types import MappingProxyType

from empiremuse.logs.redact import sanitize_value

MIB = 1024 * 1024
ERROR_KINDS = ("track_exception", "track_stuck", "node_unavailable", "command_error", "usage_write")
OTHER_KIND = "other"
DASH = "—"
SUMMARY_PREFIX = "📊 "


@dataclasses.dataclass(frozen=True)
class MetricsSnapshot:
    """Foto imutável de um intervalo: ``interval`` em segundos desde a foto anterior."""

    interval: float
    tracks_started: int
    players_peak: int
    errors: Mapping[str, int]


@dataclasses.dataclass(frozen=True)
class NodeSample:
    """Amostra de um nó do Lavalink; tudo ``None`` significa ``indisponível``."""

    identifier: str
    players: int | None
    cpu_percent: float | None
    memory_mib: float | None
    # ``frameStats`` do último minuto (``sent``/``nulled``/``deficit``); ``None`` quando o Lavalink não os enviou (sem players).
    frames_sent: int | None = None
    frames_nulled: int | None = None
    frames_deficit: int | None = None
    # Players tocando no nó todo (``playingPlayers``); sem eles o Lavalink não manda ``frameStats``, e o alarme precisa distinguir isso de dado perdido.
    players_playing: int | None = None


class MetricsCollector:
    """Contadores em memória com trava; por faixa só dois incrementos."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tracks_started = 0
        self._players_peak = 0
        self._errors: dict[str, int] = {}
        self._last_snapshot: float | None = None

    def track_started(self, guild: int) -> None:
        with self._lock:
            self._tracks_started += 1

    def track_ended(self, guild: int, reason: str) -> None:
        """Só existe para o par ``track_started``/``track_ended`` do dispatch: o fim não entra no resumo."""

    def error(self, kind: str) -> None:
        label = kind if kind in ERROR_KINDS else OTHER_KIND
        with self._lock:
            self._errors[label] = self._errors.get(label, 0) + 1

    def sample_players(self, active: int) -> None:
        with self._lock:
            if active > self._players_peak:
                self._players_peak = active

    def snapshot_and_reset(self, now: float) -> MetricsSnapshot:
        with self._lock:
            interval = 0.0 if self._last_snapshot is None else max(0.0, now - self._last_snapshot)
            snapshot = MetricsSnapshot(
                interval=interval,
                tracks_started=self._tracks_started,
                players_peak=self._players_peak,
                errors=MappingProxyType(dict(self._errors)),
            )
            self._tracks_started = 0
            self._players_peak = 0
            self._errors = {}
            self._last_snapshot = now
        return snapshot


_processes: dict[int | None, object] = {}


def process_stats(pid: int | None = None) -> tuple[float | None, float | None]:
    """``(cpu_percent, rss_mib)`` do processo Python (sem ``pid``) ou do Java do Lavalink local (com o ``pid``).

    O ``psutil.Process`` fica guardado por ``pid``: o ``cpu_percent`` mede o intervalo desde a chamada anterior (a primeira, que só
    inicia a medida, vale ``None``). Qualquer falha (sem ``psutil``, processo que acabou, sem permissão) devolve ``(None, None)``.
    """
    try:
        import psutil

        process = _processes.get(pid)
        first_call = process is None
        if process is None:
            process = _processes[pid] = psutil.Process(pid) if pid else psutil.Process()
        cpu = process.cpu_percent(interval=None)
        return (None if first_call else cpu), process.memory_info().rss / MIB
    except Exception:  # noqa: BLE001 - o campo vira "—" e o resumo segue
        _processes.pop(pid, None)
        return None, None


def _p95(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _ms(value: float | None) -> str:
    return DASH if value is None else f"{value:.0f}ms"


def _percent(value: float | None) -> str:
    return DASH if value is None else f"{value:.1f}%"


def _mib(value: float | None) -> str:
    return DASH if value is None else f"{value:.1f} MiB"


def _count(value: int | None) -> str:
    return DASH if value is None else str(value)


def format_summary(
    snapshot: MetricsSnapshot,
    *,
    guilds: int,
    players_active: int,
    gateway_ms: Sequence[float],
    voice_ms: Sequence[float],
    cpu_percent: float | None,
    rss_mib: float | None,
    nodes: Sequence[NodeSample],
) -> list[str]:
    """Pura: no máximo ``1 + len(nodes)`` linhas; campo sem dado vira ``—``."""
    errors = ", ".join(f"{kind}: {count}" for kind, count in snapshot.errors.items())
    gateway_max = max(gateway_ms) if gateway_ms else None
    gateway_avg = sum(gateway_ms) / len(gateway_ms) if gateway_ms else None
    lines = [
        f"{SUMMARY_PREFIX}servidores {guilds} · players ativos {players_active} (pico {snapshot.players_peak})"
        f" · faixas iniciadas {snapshot.tracks_started} · erros {{{errors}}}"
        f" · gateway {_ms(gateway_avg)} (máx {_ms(gateway_max)}) · voz p95 {_ms(_p95(voice_ms))}"
        f" · processo CPU {_percent(cpu_percent)} RSS {_mib(rss_mib)}"
    ]
    for node in nodes:
        name = sanitize_value(str(node.identifier))
        if node.players is None and node.cpu_percent is None and node.memory_mib is None:
            lines.append(f"{SUMMARY_PREFIX}nó {name}: indisponível")
            continue
        players = DASH if node.players is None else str(node.players)
        lines.append(
            f"{SUMMARY_PREFIX}nó {name}: players {players} · CPU {_percent(node.cpu_percent)} · memória {_mib(node.memory_mib)}"
            f" · quadros enviados {_count(node.frames_sent)} anulados {_count(node.frames_nulled)}"
            f" déficit {_count(node.frames_deficit)}"
        )
    return lines
