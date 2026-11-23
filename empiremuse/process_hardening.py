"""Endurecimento do processo do bot na subida: no Linux ele é marcado como não despejável (``prctl(PR_SET_DUMPABLE, 0)``).

Roda primeiro no ``main``, antes do ``boot_guard`` e de qualquer processo filho. Só stdlib, e nunca levanta: fora do Linux, ou
se a chamada falha, não faz nada e a subida segue.

Efeitos que valem saber na operação:

- o processo do bot não gera core dump;
- anexar um depurador ou profiler ao processo do bot passa a exigir privilégio elevado (``CAP_SYS_PTRACE``);
- um filho criado por ``fork`` herda a marcação até fazer ``exec``; no ``exec`` de um programa comum o kernel a desfaz
  (hoje todo processo filho do bot faz ``exec``).

O resultado vai para o relatório de subida (etapa ``Processo``, via ``hardening_step``): ``ok`` quando a marcação valeu
ou não se aplica, ``warn`` quando falhou no Linux.
"""

from __future__ import annotations

import ctypes
import sys

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
    """Marca o processo como não despejável. ``True`` se a marcação valeu; ``False`` fora do Linux ou se a chamada falhou."""
    if not platform.startswith("linux"):
        return False
    try:
        return _prctl(PR_SET_DUMPABLE, 0) == 0
    except OSError, AttributeError:
        return False


def hardening_step(done: bool, platform: str = sys.platform) -> tuple[str, str]:
    """O status (``ok`` ou ``warn``) e o detalhe da etapa ``Processo`` no relatório de subida."""
    if not platform.startswith("linux"):
        return "ok", f"marcação não despejável não se aplica ({platform})"
    if done:
        return "ok", "marcado como não despejável"
    return "warn", "não foi possível marcar como não despejável"
