"""Sandbox dos decodificadores de imagem: o processo filho que decodifica se confina logo ao começar, antes de importar o Pillow.

Só stdlib e ``ctypes`` (o worker o importa com ``python -I``, antes de qualquer outra coisa). O confinamento tem três partes:

- **Landlock** (Linux): sistema de arquivos só de leitura e só nos caminhos da instalação do Python (biblioteca padrão e
  ``site-packages``); nenhuma escrita. A partir do ABI 4 também fecha TCP e, a partir do ABI 6, sinais e sockets abstratos para
  fora do domínio.
- ``RLIMIT_NPROC = 0`` (nenhum processo ou thread novo) e ``RLIMIT_FSIZE = 0`` (nenhum arquivo cresce).
- **No pai:** ``start_new_session`` e, no prazo, o grupo inteiro morre (``kill_group``).

Política (``FAIL_CLOSED``): sem Landlock, ou se ele falha, o worker não decodifica (sai com ``SANDBOX_EXIT``) e o pai nem abre o
worker; quem chama cai na imagem padrão. O código vai ao log uma vez por processo. ``FAIL_CLOSED = False`` é o modo degradado:
decodifica só com os limites, com um aviso no log também uma vez.
"""

from __future__ import annotations

import ctypes
import logging
import os
import resource
import signal
import struct
import sys
import sysconfig
from contextlib import suppress

FAIL_CLOSED = True
SANDBOX_EXIT = 70
UNAVAILABLE_CODE = "SANDBOX-OFF"
DEGRADED_CODE = "SANDBOX-DEGRADED"

# landlock(7): os números das chamadas são os mesmos em todas as arquiteturas
_SYS_CREATE_RULESET, _SYS_ADD_RULE, _SYS_RESTRICT_SELF = 444, 445, 446
_CREATE_RULESET_VERSION = 1 << 0
_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38
_O_PATH = getattr(os, "O_PATH", 0o10000000)

_FS_READ_FILE = 1 << 2
_FS_READ_DIR = 1 << 3
_FS_ABI1 = (1 << 13) - 1  # da execução ao link simbólico (bits 0 a 12)
_FS_REFER = 1 << 13  # ABI 2
_FS_TRUNCATE = 1 << 14  # ABI 3
_FS_IOCTL_DEV = 1 << 15  # ABI 5
_NET_ALL = (1 << 0) | (1 << 1)  # ABI 4: bind e connect TCP
_SCOPE_ALL = (1 << 0) | (1 << 1)  # ABI 6: socket abstrato e sinal
_READ_ONLY = _FS_READ_FILE | _FS_READ_DIR

# o que o carregador dinâmico lê para achar as bibliotecas do sistema que o decodificador usa (``libz`` e afins)
_SYSTEM_LIBRARY_DIRS = ("/lib", "/lib64", "/usr/lib", "/usr/lib64")
_SYSTEM_LIBRARY_FILES = ("/etc/ld.so.cache",)

_reported = False


class _RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


def _path_beneath(allowed: int, fd: int) -> ctypes.Array:
    """``struct landlock_path_beneath_attr`` (empacotada, 12 bytes): direitos e o descritor do diretório."""
    return ctypes.create_string_buffer(struct.pack("=Qi", allowed, fd), 12)


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def landlock_abi() -> int:
    """O ABI do Landlock que o kernel oferece; ``0`` se não há (outro sistema, kernel sem Landlock, seccomp que barra a chamada)."""
    if not sys.platform.startswith("linux"):
        return 0
    try:
        version = _libc().syscall(
            ctypes.c_long(_SYS_CREATE_RULESET), None, ctypes.c_size_t(0), ctypes.c_uint32(_CREATE_RULESET_VERSION)
        )
    except OSError, AttributeError:
        return 0
    return max(0, int(version))


def python_paths() -> list[str]:
    """Os diretórios que o decodificador pode ler: a instalação do Python (prefixos, biblioteca padrão e ``site-packages``) e as
    bibliotecas do sistema que ela carrega."""
    found: list[str] = []
    candidates = [sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix, *sysconfig.get_paths().values()]
    for candidate in (*candidates, *_SYSTEM_LIBRARY_DIRS):
        path = os.path.realpath(candidate)
        if os.path.isdir(path) and path not in found:
            found.append(path)
    return found


def _library_files() -> list[str]:
    return [path for path in map(os.path.realpath, _SYSTEM_LIBRARY_FILES) if os.path.isfile(path)]


def _fs_rights(abi: int) -> int:
    rights = _FS_ABI1
    if abi >= 2:
        rights |= _FS_REFER
    if abi >= 3:
        rights |= _FS_TRUNCATE
    if abi >= 5:
        rights |= _FS_IOCTL_DEV
    return rights


def _landlock(abi: int, paths: list[str]) -> bool:
    libc = _libc()
    attr = _RulesetAttr(_fs_rights(abi), _NET_ALL if abi >= 4 else 0, _SCOPE_ALL if abi >= 6 else 0)
    size = 24 if abi >= 6 else 16 if abi >= 4 else 8  # só os campos que este kernel conhece
    ruleset = libc.syscall(
        ctypes.c_long(_SYS_CREATE_RULESET), ctypes.byref(attr), ctypes.c_size_t(size), ctypes.c_uint32(0)
    )
    if ruleset < 0:
        return False
    try:
        rules = [(path, os.O_DIRECTORY, _READ_ONLY) for path in paths]
        rules += [(path, 0, _FS_READ_FILE) for path in _library_files()]  # num arquivo só cabe o direito de arquivo
        for path, kind, allowed in rules:
            fd = os.open(path, _O_PATH | os.O_CLOEXEC | kind)
            try:
                rule = _path_beneath(allowed, fd)
                added = libc.syscall(
                    ctypes.c_long(_SYS_ADD_RULE), ruleset, ctypes.c_uint32(_RULE_PATH_BENEATH), rule, ctypes.c_uint32(0)
                )
            finally:
                os.close(fd)
            if added:
                return False
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0):
            return False
        return libc.syscall(ctypes.c_long(_SYS_RESTRICT_SELF), ruleset, ctypes.c_uint32(0)) == 0
    finally:
        os.close(ruleset)


def _limits() -> None:
    resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))


def confine() -> bool:
    """Aplica os limites e o Landlock a ESTE processo, sem volta. ``True`` só com tudo aplicado; nunca levanta."""
    try:
        _limits()
        abi = landlock_abi()
        return abi > 0 and _landlock(abi, python_paths())
    except Exception:  # noqa: BLE001 - qualquer falha é "não confinado", e a política decide
        return False


def enter_sandbox() -> None:
    """No worker, antes de ler a entrada e de importar o decodificador. Na política fechada, sem sandbox o processo sai na hora
    com ``SANDBOX_EXIT`` (nada na saída); no modo degradado segue só com os limites que conseguiu."""
    if not confine() and FAIL_CLOSED:
        os._exit(SANDBOX_EXIT)


def may_spawn(log: logging.Logger) -> bool:
    """No pai, antes de abrir um worker: o kernel oferece Landlock? Sem ele, a política decide e o código vai ao log uma vez."""
    if landlock_abi() > 0:
        return True
    report(log)
    return not FAIL_CLOSED


def report(log: logging.Logger) -> None:
    """Uma linha por processo, só com o código (sem caminho nem detalhe)."""
    global _reported
    if _reported:
        return
    _reported = True
    if FAIL_CLOSED:
        log.error("Decodificador de imagens recusado: sem sandbox (%s)", UNAVAILABLE_CODE)
    else:
        log.warning("Decodificador de imagens sem sandbox, só com limites (%s)", DEGRADED_CODE)


def kill_group(pid: int) -> None:
    """Mata o grupo do worker (aberto com ``start_new_session``, então o grupo é o PID dele). Só enquanto o PID não foi recolhido."""
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)
