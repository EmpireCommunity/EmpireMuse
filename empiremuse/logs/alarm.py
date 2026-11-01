"""Alarme de degradação (item 8): avisa no mesmo log do resumo quando os quadros de áudio ou a CPU do processo passam do limite.

Pura e sem relógio próprio (recebe o ``now``), como o ``metrics``. Uma amostra por resumo periódico. O alarme tem histerese:
``degradado`` só depois de ``TRIGGER_SAMPLES`` amostras seguidas acima do limite de entrada, ``normalizado`` só depois de
``RECOVER_SAMPLES`` seguidas abaixo do limite de saída (que é menor); entre os dois limites nada muda. Cada chave (CPU, cada nó) tem
no máximo um aviso de ``degradado`` por ``COOLDOWN_S`` e o ``normalizado`` só sai se o ``degradado`` saiu. O texto leva só números e o
identificador do nó (já saneado): nenhum nome de servidor, canal ou usuário.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

from empiremuse.logs.metrics import NodeSample
from empiremuse.logs.redact import sanitize_value

# CPU do processo Python (``psutil``; um núcleo cheio é 100%).
CPU_DEGRADED_PERCENT = 90.0
CPU_RECOVERED_PERCENT = 70.0
# Quadros de áudio do último minuto: (anulados + déficit) / esperados. O Lavalink manda médias POR PLAYER e por sessão (um bot), com
# divisão inteira nos três campos: a soma de um minuto cheio dá até 2 quadros a menos que 3000. Abaixo de ``FRAMES_MIN_EXPECTED``
# (um player por um minuto, menos essa folga) a amostra é pequena demais para julgar e conta como saudável.
FRAMES_DEGRADED_RATIO = 0.05
FRAMES_RECOVERED_RATIO = 0.02
FRAMES_PER_PLAYER_MINUTE = 3000
FRAMES_ROUNDING_SLACK = 3
FRAMES_MIN_EXPECTED = FRAMES_PER_PLAYER_MINUTE - FRAMES_ROUNDING_SLACK
TRIGGER_SAMPLES = 2
RECOVER_SAMPLES = 2
COOLDOWN_S = 1800.0

CPU_KEY = "cpu"


def frame_failure_ratio(node: NodeSample) -> float | None:
    """Fração de quadros que falharam no último minuto numa sessão do nó; ``None`` quando não há informação (o estado não muda).

    Sem ``frameStats`` a amostra só é saudável se NINGUÉM está tocando no nó (``players_playing == 0``): é assim que o Lavalink avisa
    que não há o que medir. Nó indisponível (mesmo com CPU e memória do Java preenchidas depois) ou sessão sem dado enquanto há
    players tocando é ``None``, nunca normalização.
    """
    if node.frames_sent is None or node.frames_nulled is None or node.frames_deficit is None:
        return 0.0 if node.players_playing == 0 else None
    failed = node.frames_nulled + max(node.frames_deficit, 0)
    expected = node.frames_sent + failed
    if expected < FRAMES_MIN_EXPECTED:
        return 0.0
    return failed / expected


def _worst_session(samples: Sequence[NodeSample]) -> tuple[float, NodeSample] | None:
    """A pior das sessões (bots) do mesmo nó que tem informação; ``None`` se nenhuma tem."""
    rated = [(ratio, sample) for sample in samples if (ratio := frame_failure_ratio(sample)) is not None]
    return max(rated, key=lambda item: item[0], default=None)


@dataclasses.dataclass
class _Track:
    bad: int = 0
    good: int = 0
    alerted: bool = False
    last_alert: float | None = None


class DegradationAlarm:
    """Estado do alarme por chave; ``observe`` devolve as linhas novas (vazio na maioria das amostras)."""

    def __init__(
        self,
        *,
        trigger_samples: int = TRIGGER_SAMPLES,
        recover_samples: int = RECOVER_SAMPLES,
        cooldown: float = COOLDOWN_S,
    ) -> None:
        self._trigger = trigger_samples
        self._recover = recover_samples
        self._cooldown = cooldown
        self._tracks: dict[str, _Track] = {}

    def _step(self, key: str, now: float, value: float | None, high: float, low: float) -> str | None:
        """``"degraded"``, ``"recovered"`` ou ``None``."""
        if value is None:
            return None
        track = self._tracks.setdefault(key, _Track())
        if value >= high:
            track.bad, track.good = track.bad + 1, 0
            cooled = track.last_alert is None or now - track.last_alert >= self._cooldown
            if not track.alerted and track.bad >= self._trigger and cooled:
                track.alerted, track.last_alert = True, now
                return "degraded"
        elif value < low:
            track.bad, track.good = 0, track.good + 1
            if track.alerted and track.good >= self._recover:
                track.alerted = False
                return "recovered"
        else:
            track.bad = track.good = 0
        return None

    def observe(self, now: float, *, cpu_percent: float | None, nodes: Sequence[NodeSample]) -> list[str]:
        lines: list[str] = []
        event = self._step(CPU_KEY, now, cpu_percent, CPU_DEGRADED_PERCENT, CPU_RECOVERED_PERCENT)
        if event is not None and cpu_percent is not None:
            lines.append(_cpu_line(event, cpu_percent))
        by_node: dict[str, list[NodeSample]] = {}
        for node in nodes:  # uma amostra por sessão (bot): o alarme olha a pior de cada nó
            by_node.setdefault(sanitize_value(str(node.identifier)), []).append(node)
        for name, sessions in by_node.items():
            worst = _worst_session(sessions)
            ratio, sample = worst if worst is not None else (None, None)
            event = self._step(f"frames:{name}", now, ratio, FRAMES_DEGRADED_RATIO, FRAMES_RECOVERED_RATIO)
            if event is not None and ratio is not None and sample is not None:
                lines.append(_frames_line(event, name, ratio, sample))
        return lines


def _cpu_line(event: str, cpu_percent: float) -> str:
    if event == "degraded":
        return (
            f"alerta: CPU do processo degradada em {cpu_percent:.1f}% (limite {CPU_DEGRADED_PERCENT:.0f}%,"
            f" {TRIGGER_SAMPLES} resumos seguidos)"
        )
    return f"alerta: CPU do processo normalizada em {cpu_percent:.1f}% (abaixo de {CPU_RECOVERED_PERCENT:.0f}%)"


def _frames_line(event: str, name: str, ratio: float, node: NodeSample) -> str:
    if event == "degraded":
        return (
            f"alerta: nó {name} com falha nos quadros de áudio: {ratio:.1%} no último minuto"
            f" (anulados {node.frames_nulled}, déficit {node.frames_deficit}, enviados {node.frames_sent};"
            f" limite {FRAMES_DEGRADED_RATIO:.0%}, {TRIGGER_SAMPLES} resumos seguidos)"
        )
    return (
        f"alerta: nó {name} com os quadros de áudio normalizados: {ratio:.1%} (abaixo de {FRAMES_RECOVERED_RATIO:.0%})"
    )
