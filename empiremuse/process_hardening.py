"""Endurecimento do processo do bot na subida: no Linux ele é marcado como não despejável (``prctl(PR_SET_DUMPABLE, 0)``).

Roda primeiro no ``main``, antes do ``boot_guard`` e de qualquer processo filho. Só stdlib, e nunca levanta: fora do Linux, ou
se a chamada falha, não faz nada e a subida segue.

Efeitos que valem saber na operação:

- o processo do bot não gera core dump;
- anexar um depurador ou profiler ao processo do bot passa a exigir privilégio elevado (``CAP_SYS_PTRACE``);
- os filhos voltam ao padrão do sistema no ``exec`` (a marcação é só deste processo).
"""

from __future__ import annotations

import ctypes
import logging
import sys

logger = logging.getLogger("empiremuse.boot")

PR_GET_DUMPABLE = 3
PR_SET_DUMPABLE = 4


def _prctl(option: int, value: int = 0) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    return int(prctl(option, value, 0, 0, 0))


def is_dumpable(platform: str = sys.platform) -> bool | None:
    """O estado atual do processo; ``None`` fora do Linux ou sem ``prctl``."""
    if not platform.startswith("linux"):
        return None
    try:
        return _prctl(PR_GET_DUMPABLE) == 1
    except OSError, AttributeError:
        return None


def mark_non_dumpable(platform: str = sys.platform) -> bool:
    """Marca o processo como não despejável. ``True`` se a marcação valeu; registra o resultado uma vez, em debug."""
    if not platform.startswith("linux"):
        logger.debug("Processo: marcação não despejável não se aplica (%s)", platform)
        return False
    try:
        done = _prctl(PR_SET_DUMPABLE, 0) == 0
    except OSError, AttributeError:
        done = False
    if done:
        logger.debug("Processo marcado como não despejável")
    else:
        logger.debug("Processo: não foi possível marcar como não despejável")
    return done
