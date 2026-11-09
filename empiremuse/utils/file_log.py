"""Log em arquivo do bot (``.logs/disnake-AAAA-MM-DD.log``): um arquivo por dia (UTC), podado por idade, sem segredos e só para o dono.

A poda apaga somente arquivo de nome exato, regular (não link) e mais velho que
``LOG_RETENTION_DAYS``; a idade vem do nome. ``LOG_RETENTION_HOLD`` suspende a poda (ordem de guarda). Um teto de bytes por dia evita
que uma inundação de linhas encha o disco, e nunca apaga arquivo antes da idade.
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from empiremuse.logs.console import FileFormatter, report_emit_failure
from empiremuse.logs.redact import REDACTED, SanitizeAndRedact
from empiremuse.rules.log_retention import DEFAULT_RETENTION_DAYS, is_expired, log_file_name

LOG_FILE_PREFIX = "disnake"
LOG_DAY_MAX_BYTES = 64 * 1024 * 1024
LOG_DIR_MODE = 0o700
LOG_FILE_MODE = 0o600
logger = logging.getLogger(__name__)
PRUNE_INTERVAL = timedelta(hours=1)
TRUNCATED_MARKER = "log do dia truncado: teto diário de bytes atingido; o resto do dia é descartado"

__all__ = [
    "LOG_DAY_MAX_BYTES",
    "LOG_FILE_PREFIX",
    "REDACTED",
    "DailyRetentionFileHandler",
    "RedactWebhookTokens",
    "build_file_log_handler",
    "ensure_private_logs_dir",
]

# Nome antigo do filtro de redação: hoje é o redator único do pacote ``logs`` (webhooks, tokens, credenciais e controles).
RedactWebhookTokens = SanitizeAndRedact


def _utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_private_logs_dir(logs_dir: Path) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True, mode=LOG_DIR_MODE)
    with suppress(OSError):
        os.chmod(logs_dir, LOG_DIR_MODE)


class DailyRetentionFileHandler(logging.FileHandler):
    """Arquivo por dia (UTC) em append, modo 0o600 onde o SO permite, com poda por idade e teto diário de bytes.

    Troca de arquivo à meia-noite UTC (decidida pelo relógio injetado ``now``, não pelo ``LogRecord``). A poda roda na criação, na
    troca de dia e no ``maintain`` (no máximo uma vez por hora). ``hold=True`` pausa a poda por inteiro.
    """

    def __init__(
        self,
        logs_dir: Path,
        *,
        prefix: str = LOG_FILE_PREFIX,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        hold: bool = False,
        max_day_bytes: int = LOG_DAY_MAX_BYTES,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        ensure_private_logs_dir(logs_dir)
        self.logs_dir = logs_dir
        self.prefix = prefix
        self.retention_days = retention_days
        self.hold = hold
        self.max_day_bytes = max_day_bytes
        self._now = now
        self._day: date | None = None
        self._written = 0
        self._truncated = False
        self._last_prune: datetime | None = None
        self.last_prune_removed = 0
        self.last_prune_failed = 0
        start = now()
        # errors="backslashreplace": surrogate solto (vem de json.loads) vira \udXXX em vez de derrubar o registro do arquivo.
        super().__init__(
            str(logs_dir / log_file_name(prefix, start.date())),
            mode="a",
            encoding="utf-8",
            delay=True,
            errors="backslashreplace",
        )
        self._switch_to(start)
        self.stream = (
            self._open()
        )  # abre já: pasta sem permissão ou disco cheio levanta aqui e o ``configure_logging`` segue só no console
        self.setFormatter(FileFormatter())

    def _open(self):
        flags = (
            os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        )  # link simbólico no nome do dia não é seguido
        fd = os.open(self.baseFilename, flags, LOG_FILE_MODE)
        try:
            with suppress(
                OSError, AttributeError
            ):  # Windows não tem fchmod/ignora o modo; falhar aqui não pode impedir o log
                os.fchmod(fd, LOG_FILE_MODE)  # no mesmo descritor: nenhuma troca do nome entre abrir e mudar o modo
            return os.fdopen(fd, self.mode, encoding=self.encoding, errors=self.errors)
        except BaseException:
            os.close(fd)
            raise

    def _switch_to(self, moment: datetime) -> None:
        """Passa a escrever no arquivo do dia de ``moment``: fecha o anterior, mede o que já há e poda."""
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        self._day = moment.date()
        self.baseFilename = os.fspath(self.logs_dir / log_file_name(self.prefix, self._day))
        try:
            self._written = os.lstat(self.baseFilename).st_size
        except FileNotFoundError:
            self._written = 0
        self._truncated = self._written >= self.max_day_bytes
        self._prune(moment)

    def _prune(self, moment: datetime) -> None:
        self._last_prune = moment
        self.last_prune_removed, self.last_prune_failed = prune_logs(
            self.logs_dir, moment.date(), self.retention_days, hold=self.hold
        )

    def maintain(self) -> bool:
        """Gancho periódico (``tick_logging``): refaz a poda se a última foi há uma hora ou mais; devolve se ela rodou."""
        self.acquire()
        try:
            moment = self._now()
            if self._last_prune is not None and moment - self._last_prune < PRUNE_INTERVAL:
                return False
            self._prune(moment)
            return True
        finally:
            self.release()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            moment = self._now()
            if moment.date() != self._day:
                self._switch_to(moment)
            data = self.format(record) + "\n"
            size = len(data.encode("utf-8", "backslashreplace"))
            if self._truncated or self._written + size > self.max_day_bytes:
                self._drop_rest_of_day()
                return
            self._write(data, size)
        except Exception:
            self.handleError(record)

    def _drop_rest_of_day(self) -> None:
        if not self._truncated:
            self._truncated = True  # antes do aviso: o aviso volta a este handler e já cai no "resto do dia"
            marker = self.format(
                logging.LogRecord(__name__, logging.WARNING, __file__, 0, TRUNCATED_MARKER, None, None)
            )
            self._write(marker + "\n", 0)
            logger.warning(
                "Log do dia truncado: o teto diário de %d bytes foi atingido; o resto do dia é descartado.",
                self.max_day_bytes,
                extra={"empiremuse_emit": True},  # um aviso só: não passa pelo agregador nem pelo filtro de nível
            )

    def _write(self, data: str, size: int) -> None:
        if self.stream is None:
            self.stream = self._open()
        self.stream.write(data)
        self.flush()
        self._written += size

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 (nome da API do logging)
        report_emit_failure()


def prune_logs(logs_dir: Path, today: date, retention_days: int, *, hold: bool = False) -> tuple[int, int]:
    """Apaga os logs vencidos de ``logs_dir`` e devolve ``(apagados, falhas)``.

    Só apaga arquivo REGULAR (link simbólico, pasta e outros ficam) cujo nome casa exatamente com o padrão e cuja data do nome é mais
    velha que ``retention_days``. Com ``hold`` não apaga nada. Nunca levanta: falha num arquivo conta em ``falhas`` e segue.
    """
    if hold:
        return 0, 0
    removed = failed = 0
    try:
        entries = list(os.scandir(logs_dir))
    except OSError:
        return 0, 1
    for entry in entries:
        if not is_expired(entry.name, today, retention_days):
            continue
        try:
            if not stat.S_ISREG(entry.stat(follow_symlinks=False).st_mode):
                continue
            os.unlink(entry.path)
            removed += 1
        except FileNotFoundError:
            continue
        except OSError:
            failed += 1
    return removed, failed


def build_file_log_handler(
    logs_dir: Path,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    hold: bool = False,
    now: Callable[[], datetime] = _utc_now,
) -> DailyRetentionFileHandler:
    """Log em arquivo diário podado por idade e em append: os códigos ERR-... sobrevivem a um reinício.

    Sozinho já é seguro (redator e formatter próprios). O ``configure_logging`` troca essa cadeia pela dele, compartilhada com o
    console, para o redator e o agregador valerem uma vez só por destino.
    """
    handler = DailyRetentionFileHandler(logs_dir, retention_days=retention_days, hold=hold, now=now)
    handler.addFilter(SanitizeAndRedact())
    return handler
