"""Retenção dos arquivos de log por idade: só decisões, sem I/O.

A idade vem do NOME do arquivo (``disnake-AAAA-MM-DD.log``, data em UTC), nunca do ``mtime``: copiar, restaurar um backup ou tocar o
arquivo não pode adiar nem antecipar a poda.
"""

from __future__ import annotations

import re
from datetime import date

DEFAULT_RETENTION_DAYS = 183
MIN_RETENTION_DAYS = 30
MAX_RETENTION_DAYS = 730
# Só estes nomes podem ser apagados pela poda; qualquer outro arquivo da pasta (inclusive ``disnake-2026-01-01.log.1``) fica.
_PRUNABLE_NAME = re.compile(r"(?:disnake|access)-([0-9]{4})-([0-9]{2})-([0-9]{2})\.log")


def log_file_name(prefix: str, day: date) -> str:
    """``disnake-2026-11-09.log``: o nome do arquivo de log do dia (UTC)."""
    return f"{prefix}-{day.isoformat()}.log"


def log_file_date(name: str) -> date | None:
    """A data do nome, ou ``None`` se ele não casa EXATAMENTE com o padrão (ou a data não existe)."""
    match = _PRUNABLE_NAME.fullmatch(name)
    if match is None:
        return None
    try:
        return date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return None


def is_expired(name: str, today: date, retention_days: int) -> bool:
    """Se o arquivo é mais velho que ``retention_days``: 184 dias com retenção de 183 sai; 183 fica."""
    day = log_file_date(name)
    return day is not None and (today - day).days > retention_days


def parse_retention_days(raw: object) -> tuple[int, str | None]:
    """``LOG_RETENTION_DAYS`` em dias e o aviso, se houver.

    Ausente ou vazio usa 183. Texto que não é número inteiro usa 183 com aviso. Número fora de 30 a 730 vai para o limite mais
    próximo, com aviso: o lado errado do erro é apagar log que o operador queria guardar (suspensão legal) ou antes do mínimo.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return DEFAULT_RETENTION_DAYS, None
    try:
        if isinstance(raw, bool):
            raise ValueError
        days = int(str(raw).strip())
    except ValueError:
        return DEFAULT_RETENTION_DAYS, f"LOG_RETENTION_DAYS inválido; usando {DEFAULT_RETENTION_DAYS} dias."
    bounded = min(max(days, MIN_RETENTION_DAYS), MAX_RETENTION_DAYS)
    if bounded != days:
        return (
            bounded,
            f"LOG_RETENTION_DAYS fora de {MIN_RETENTION_DAYS} a {MAX_RETENTION_DAYS}; usando {bounded} dias.",
        )
    return days, None
