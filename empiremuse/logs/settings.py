"""Configuração do log (item 7): lê as chaves ``LOG_*`` (inclusive retenção e suspensão legal) e ``METRICS_INTERVAL`` numa estrutura imutável, sem logar nada."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass

from empiremuse.rules.log_retention import DEFAULT_RETENTION_DAYS, parse_retention_days
from empiremuse.version import running_from_image

LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR}
DEFAULT_LEVEL = "INFO"
DEFAULT_METRICS_INTERVAL = 300
MIN_METRICS_INTERVAL = 60
MAX_DEBUG_GUILDS = 50
# Só snowflakes: 15 a 21 dígitos ASCII (``\d`` aceitaria algarismos de outros alfabetos).
_SNOWFLAKE = re.compile(r"[0-9]{15,21}")


@dataclass(frozen=True)
class LogSettings:
    level: int
    color: bool
    file: bool
    debug_guilds: frozenset[int]
    metrics_interval: int
    retention_days: int = DEFAULT_RETENTION_DAYS
    retention_hold: bool = False


def _text(config: Mapping[str, object], key: str, default: str) -> str:
    value = config.get(key)
    if value is None:
        return default
    return str(value).strip().lower() or default


def _truthy(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _level(config: Mapping[str, object], warnings: list[str]) -> int:
    raw = str(config.get("LOG_LEVEL") or DEFAULT_LEVEL).strip().upper()
    if raw in LEVELS:
        return LEVELS[raw]
    warnings.append("LOG_LEVEL inválido; usando INFO (valores aceitos: DEBUG, INFO, WARNING, ERROR).")
    return logging.INFO


def _color(config: Mapping[str, object], environ: Mapping[str, str], warnings: list[str]) -> bool:
    value = _text(config, "LOG_COLOR", "on")
    if value not in ("on", "off"):
        warnings.append("LOG_COLOR inválido; usando on (valores aceitos: on, off).")
        value = "on"
    return value == "on" and not environ.get("NO_COLOR")


def _file(config: Mapping[str, object], environ: Mapping[str, str], warnings: list[str]) -> bool:
    value = _text(config, "LOG_FILE", "auto")
    if value not in ("auto", "on", "off"):
        warnings.append("LOG_FILE inválido; usando auto (valores aceitos: auto, on, off).")
        value = "auto"
    if value == "auto" and _truthy(config.get("ENABLE_LOGGER")):
        return True  # sinônimo legado de LOG_FILE=on; o ENABLE_LOGGER nunca desliga nada
    if value == "auto":
        return not running_from_image(environ)
    return value == "on"


def _debug_guilds(config: Mapping[str, object], warnings: list[str]) -> frozenset[int]:
    guilds: list[int] = []
    invalid = 0
    for item in str(config.get("LOG_DEBUG_GUILDS") or "").split(","):
        item = item.strip()
        if not item:
            continue
        if not _SNOWFLAKE.fullmatch(item):
            invalid += 1
        elif int(item) not in guilds:
            guilds.append(int(item))
    if invalid:
        warnings.append(f"LOG_DEBUG_GUILDS: {invalid} valor(es) que não são IDs de servidor foram ignorados.")
    if len(guilds) > MAX_DEBUG_GUILDS:
        warnings.append(f"LOG_DEBUG_GUILDS: só os primeiros {MAX_DEBUG_GUILDS} IDs valem; o excesso foi ignorado.")
        guilds = guilds[:MAX_DEBUG_GUILDS]
    return frozenset(guilds)


def _metrics_interval(config: Mapping[str, object], warnings: list[str]) -> int:
    try:
        seconds = int(config.get("METRICS_INTERVAL", DEFAULT_METRICS_INTERVAL))
    except TypeError, ValueError, OverflowError:
        seconds = -1
    if seconds < 0:
        warnings.append(f"METRICS_INTERVAL inválido; usando {DEFAULT_METRICS_INTERVAL} segundos.")
        return DEFAULT_METRICS_INTERVAL
    if seconds == 0:
        return 0
    return max(seconds, MIN_METRICS_INTERVAL)


def _retention_days(config: Mapping[str, object], warnings: list[str]) -> int:
    days, warning = parse_retention_days(config.get("LOG_RETENTION_DAYS"))
    if warning:
        warnings.append(warning)
    return days


def parse_log_settings(config: Mapping[str, object], environ: Mapping[str, str]) -> tuple[LogSettings, list[str]]:
    """Lê as chaves do log; devolve as configurações e os avisos (logados só depois de configurar o log).

    Pura: não muda ``config`` nem ``environ`` e não loga. ``NO_COLOR`` e a imagem Docker vêm só de ``environ``.
    """
    warnings: list[str] = []
    settings = LogSettings(
        level=_level(config, warnings),
        color=_color(config, environ, warnings),
        file=_file(config, environ, warnings),
        debug_guilds=_debug_guilds(config, warnings),
        metrics_interval=_metrics_interval(config, warnings),
        retention_days=_retention_days(config, warnings),
        retention_hold=_truthy(config.get("LOG_RETENTION_HOLD")),
    )
    return settings, warnings
