"""Montagem do log (item 7): handlers de console e de arquivo no root, com a mesma cadeia de filtros nos dois."""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from empiremuse import paths
from empiremuse.logs.aggregate import RepeatAggregator
from empiremuse.logs.console import ConsoleFormatter, FileFormatter, report_emit_failure
from empiremuse.logs.context import ContextFilter, LevelGate, VoiceDependencyNoiseFilter
from empiremuse.logs.redact import SanitizeAndRedact, configure_secrets, register_secret
from empiremuse.logs.settings import LogSettings

if TYPE_CHECKING:
    from empiremuse.utils.file_log import DailyRetentionFileHandler

__all__ = ["LIBRARY_LOGGERS", "configure_logging", "flush_logging", "register_secret", "tick_logging"]

# Bibliotecas externas ficam sempre em WARNING; o pacote vendorizado empiremuse.wavelink é nosso e segue o LOG_LEVEL.
LIBRARY_LOGGERS = ("disnake", "aiohttp", "asyncio", "tornado", "urllib3", "pymongo", "motor", "websockets", "PIL")
LOGS_DIR_NAME = ".logs"

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_installed: list[logging.Handler] = []
_aggregator: RepeatAggregator | None = None
_debug_guilds: frozenset[int] = frozenset()
_file_handler_installed: DailyRetentionFileHandler | None = None


_console_state = threading.local()


def take_console_failure() -> bool:
    """Se o console falhou ao emitir nesta thread desde a última chamada; limpa a marca.

    O ``emit`` é síncrono, então o gancho de exceções lê a marca logo depois do ``log_unhandled``
    e, se o console perdeu o traceback, escreve o seu próprio. O logger comum não levanta nem muda.
    """
    failed = getattr(_console_state, "failed", False)
    _console_state.failed = False
    return failed


class _ConsoleHandler(logging.StreamHandler):
    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 (nome da API do logging)
        _console_state.failed = True
        report_emit_failure()


def _uninstall() -> None:
    """Descarrega as contagens pendentes e tira do root os handlers que o ``configure_logging`` anterior instalou."""
    global _aggregator, _file_handler_installed
    root = logging.getLogger()
    if _aggregator is not None:
        _aggregator.flush()
        _aggregator = None
    for handler in _installed:
        root.removeHandler(handler)
        handler.close()
    _installed.clear()
    _file_handler_installed = None


def _file_handler(
    logs_dir: Path, settings: LogSettings, now: Callable[[], datetime] | None
) -> tuple[DailyRetentionFileHandler | None, str | None]:
    """O handler do arquivo e, se ele não abriu, o tipo do erro (pasta sem permissão ou disco cheio não derruba a subida)."""
    from empiremuse.utils.file_log import build_file_log_handler  # import tardio: o file_log importa o pacote logs

    options = {"now": now} if now is not None else {}
    try:
        handler = build_file_log_handler(
            logs_dir, retention_days=settings.retention_days, hold=settings.retention_hold, **options
        )
        return handler, None
    except OSError as e:
        return None, type(e).__name__


def configure_logging(
    settings: LogSettings,
    secrets: Iterable[str],
    *,
    stream: TextIO | None = None,
    logs_dir: Path | None = None,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] | None = None,
) -> None:
    """Instala o console e, com ``settings.file``, o arquivo. Idempotente: a segunda chamada troca o que a primeira instalou.

    Cadeia dos dois handlers, nesta ordem: ``ContextFilter`` → ``VoiceDependencyNoiseFilter`` → ``LevelGate`` → ``SanitizeAndRedact`` → ``RepeatAggregator`` →
    formatter. O redator e o agregador são uma instância só, compartilhada, para o console e o arquivo terem o mesmo conteúdo.
    ``now`` (UTC) é o relógio do arquivo diário e da poda por idade; o padrão é o relógio real.
    """
    global _aggregator, _debug_guilds, _file_handler_installed
    with _lock:
        _uninstall()
        configure_secrets(secrets)

        console = _ConsoleHandler(stream if stream is not None else sys.stderr)
        console.setFormatter(ConsoleFormatter(settings.color))
        handlers: list[logging.Handler] = [console]
        file_error = None
        if settings.file:
            file_handler, file_error = _file_handler(
                logs_dir if logs_dir is not None else paths.RUNTIME_DIR / LOGS_DIR_NAME, settings, now
            )
            if file_handler is not None:
                file_handler.setFormatter(FileFormatter())
                handlers.append(file_handler)
                _file_handler_installed = file_handler

        def deliver(record: logging.LogRecord) -> None:
            for handler in list(handlers):
                handler.handle(record)

        redactor = SanitizeAndRedact()
        aggregator = RepeatAggregator(deliver, clock=clock)
        root = logging.getLogger()
        for handler in handlers:
            handler.filters.clear()  # o handler do arquivo já nasce com o redator próprio; aqui a cadeia entra uma vez só
            for filter_ in (
                ContextFilter(),
                VoiceDependencyNoiseFilter(),
                LevelGate(settings.level, settings.debug_guilds),
                redactor,
                aggregator,
            ):
                handler.addFilter(filter_)
            root.addHandler(handler)
        _installed.extend(handlers)
        _aggregator = aggregator

        _debug_guilds = settings.debug_guilds
        # Com LOG_LEVEL=ERROR o root ainda cria registros INFO: é o que deixa a linha de auditoria (``empiremuse_emit=True``) chegar ao
        # LevelGate, que descarta o resto. Abaixo de INFO só com servidores em debug.
        root.setLevel(logging.DEBUG if settings.debug_guilds else min(settings.level, logging.INFO))
        for name in LIBRARY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)
        if file_error is not None:  # depois do addHandler: o aviso sai pelo console já configurado
            logger.warning("Não foi possível abrir o arquivo de log (%s); seguindo só no console.", file_error)
        if _file_handler_installed is not None:
            _report_retention(_file_handler_installed, settings)


def _report_retention(handler: DailyRetentionFileHandler, settings: LogSettings) -> None:
    """Resultado da poda da subida (sem caminhos) e o aviso de que a suspensão legal está ligada."""
    if settings.retention_hold:
        logger.warning("LOG_RETENTION_HOLD ativo: a poda dos logs por idade está suspensa.")
    if handler.last_prune_failed:
        logger.warning("Poda de logs: %d arquivo(s) vencidos não puderam ser apagados.", handler.last_prune_failed)
    if handler.last_prune_removed:
        logger.info(
            "Poda de logs: %d arquivo(s) com mais de %d dias apagados.",
            handler.last_prune_removed,
            settings.retention_days,
        )


def is_configured() -> bool:
    """Se os handlers do ``configure_logging`` estão no root agora (antes disso valem só os ganchos iniciais)."""
    root_handlers = logging.getLogger().handlers
    return any(handler in root_handlers for handler in _installed)


def configured_debug_guilds() -> frozenset[int]:
    """Servidores em ``LOG_DEBUG_GUILDS`` do último ``configure_logging`` (vazio antes dele)."""
    return _debug_guilds


def flush_logging() -> None:
    """Emite as contagens de repetidos pendentes (no ``shutdown`` do ``BotPool``)."""
    with _lock:
        aggregator = _aggregator
    if aggregator is not None:
        aggregator.flush()


def tick_logging() -> None:
    """Fecha as janelas de repetidos já vencidas e refaz a poda dos logs por idade (chamado pelo resumo periódico)."""
    with _lock:
        aggregator = _aggregator
        file_handler = _file_handler_installed
    if aggregator is not None:
        aggregator.tick()
    if file_handler is not None and file_handler.maintain() and file_handler.last_prune_failed:
        logger.warning("Poda de logs: %d arquivo(s) vencidos não puderam ser apagados.", file_handler.last_prune_failed)
