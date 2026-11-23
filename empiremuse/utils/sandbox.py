"""Sandbox dos decodificadores de imagem: o processo filho que decodifica se confina logo ao começar, antes de importar o Pillow.

Só stdlib e ``ctypes`` (o worker o importa com ``python -I``, antes de qualquer outra coisa). O confinamento tem quatro partes:

- **Landlock** (Linux, ABI ``MIN_ABI`` ou mais): sistema de arquivos só de leitura e só nos caminhos da instalação do Python
  (biblioteca padrão e ``site-packages``) e nas bibliotecas do sistema que ela carrega; nenhuma escrita nem execução; sinais e
  sockets abstratos só dentro do domínio. Um caminho permitido que contenha o diretório do app recusa o sandbox.
- **seccomp:** nenhum socket novo e nenhum anel de E/S assíncrona do kernel (essas chamadas devolvem ``EPERM``), em ``x86_64`` e
  ``aarch64`` com Python de 64 bits; outra arquitetura recusa o sandbox.
- ``RLIMIT_NPROC = 0`` (nenhum processo ou thread novo) e ``RLIMIT_FSIZE = 0`` (nenhum arquivo cresce).
- **No pai:** ``start_new_session`` e, no prazo, o grupo inteiro morre (``kill_worker``).

Política (``FAIL_CLOSED``): sem o sandbox completo, o worker não decodifica (sai com ``SANDBOX_EXIT``) e o pai nem abre o worker
(nem busca a imagem: ``may_spawn`` antes); quem chama cai na imagem padrão. O código vai ao log uma vez por processo.
``FAIL_CLOSED = False`` é o modo degradado: decodifica só com o que conseguiu aplicar, com um aviso no log também uma vez.
"""

from __future__ import annotations

import ctypes
import errno
import logging
import os
import platform
import resource
import signal
import struct
import sys
import sysconfig
from contextlib import suppress
from typing import Any

FAIL_CLOSED = True
MIN_ABI = 6  # o que a política precisa: escrita e truncamento (3) e sinais e sockets abstratos fora do domínio (6)
SANDBOX_EXIT = 70
UNAVAILABLE_CODE = "SANDBOX-OFF"
DEGRADED_CODE = "SANDBOX-DEGRADED"

# landlock(7): os números das chamadas são os mesmos em todas as arquiteturas
_SYS_CREATE_RULESET, _SYS_ADD_RULE, _SYS_RESTRICT_SELF = 444, 445, 446
_CREATE_RULESET_VERSION = 1 << 0
_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2
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

# seccomp (seccomp(2), filter.h): arquitetura conferida e o número de ``socket`` de cada uma
_AUDIT_ARCH = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}
# as chamadas negadas: ``socket`` e as três do io_uring (``io_uring_setup``, ``_enter`` e ``_register``, iguais nas duas arquiteturas)
_DENIED_NR = {"x86_64": (41, 425, 426, 427), "aarch64": (198, 425, 426, 427)}
_X32_SYSCALL_BIT = 0x40000000  # x32 no x86_64: outra numeração, recusada por inteiro
_BPF_LD_W_ABS, _BPF_JEQ_K, _BPF_JGE_K, _BPF_RET_K = 0x20, 0x15, 0x35, 0x06
_RET_ALLOW, _RET_KILL_PROCESS = 0x7FFF0000, 0x80000000
_RET_EPERM = 0x00050000 | errno.EPERM
_DATA_NR, _DATA_ARCH = 0, 4  # offsets em ``struct seccomp_data``

# o que o carregador dinâmico lê para achar as bibliotecas do sistema que o decodificador usa (``libz`` e afins)
_SYSTEM_LIBRARY_DIRS = ("/lib", "/lib64", "/usr/lib", "/usr/lib64")
_SYSTEM_LIBRARY_FILES = ("/etc/ld.so.cache",)
_APP_ROOT = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, os.pardir))

_reported = False


class _RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


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


def _filterable() -> bool:
    """Arquitetura com o filtro e um Python de 64 bits (um Python de 32 bits num kernel de 64 seria morto pelo filtro)."""
    return platform.machine() in _DENIED_NR and sys.maxsize > 2**32


def supported() -> bool:
    """O sandbox inteiro cabe aqui? Landlock com o ABI que a política pede, o filtro de chamadas e nenhum caminho permitido que contenha o
    app. O pai confere isto antes de buscar a imagem; o filho refaz tudo em ``confine``."""
    return landlock_abi() >= MIN_ABI and _filterable() and not exposes_app(python_paths())


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


def exposes_app(paths: list[str], app_root: str | None = None) -> bool:
    """Algum caminho permitido é o diretório do app ou o contém (um Python instalado em ``/`` ou no próprio diretório do app)?"""
    app_root = app_root or _APP_ROOT
    return any(app_root == path or app_root.startswith(path.rstrip(os.sep) + os.sep) for path in paths)


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
        if not _no_new_privs(libc):
            return False
        return libc.syscall(ctypes.c_long(_SYS_RESTRICT_SELF), ruleset, ctypes.c_uint32(0)) == 0
    finally:
        os.close(ruleset)


def _bpf(code: int, jt: int, jf: int, k: int) -> bytes:
    return struct.pack("=HBBI", code, jt, jf, k)


def socket_filter(machine: str) -> bytes:
    """O programa BPF: arquitetura diferente da esperada mata o processo; as chamadas negadas (e, no ``x86_64``, toda chamada x32)
    devolvem ``EPERM``; o resto passa. Os saltos são relativos à instrução seguinte: cada teste salta até o ``EPERM``, logo depois do
    ``ALLOW``."""
    tests = [(_BPF_JGE_K, _X32_SYSCALL_BIT)] if machine == "x86_64" else []
    tests += [(_BPF_JEQ_K, nr) for nr in _DENIED_NR[machine]]
    head = [_bpf(_BPF_LD_W_ABS, 0, 0, _DATA_ARCH), _bpf(_BPF_JEQ_K, 1, 0, _AUDIT_ARCH[machine])]
    head.append(_bpf(_BPF_RET_K, 0, 0, _RET_KILL_PROCESS))
    body = [_bpf(_BPF_LD_W_ABS, 0, 0, _DATA_NR)]
    body += [_bpf(code, len(tests) - index, 0, k) for index, (code, k) in enumerate(tests)]
    tail = [_bpf(_BPF_RET_K, 0, 0, _RET_ALLOW), _bpf(_BPF_RET_K, 0, 0, _RET_EPERM)]
    return b"".join(head + body + tail)


def _no_new_privs(libc: ctypes.CDLL) -> bool:
    zero = ctypes.c_ulong(0)
    return libc.prctl(ctypes.c_int(_PR_SET_NO_NEW_PRIVS), ctypes.c_ulong(1), zero, zero, zero) == 0


def _seccomp(machine: str) -> bool:
    program = socket_filter(machine)
    buffer = ctypes.create_string_buffer(program, len(program))
    fprog = _SockFprog(len(program) // 8, ctypes.cast(buffer, ctypes.c_void_p))
    libc = _libc()
    if not _no_new_privs(libc):
        return False
    zero = ctypes.c_ulong(0)
    mode = ctypes.c_ulong(_SECCOMP_MODE_FILTER)
    return libc.prctl(ctypes.c_int(_PR_SET_SECCOMP), mode, ctypes.byref(fprog), zero, zero) == 0


def _limits() -> None:
    resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))


def confine() -> bool:
    """Aplica os limites, o Landlock e o filtro de chamadas a ESTE processo, sem volta. ``True`` só com tudo aplicado; nunca levanta."""
    try:
        _limits()
        abi, machine = landlock_abi(), platform.machine()
        if abi < MIN_ABI or not _filterable():
            return False
        paths = python_paths()
        if exposes_app(paths):
            return False
        return _landlock(abi, paths) and _seccomp(machine)
    except Exception:  # noqa: BLE001 - qualquer falha é "não confinado", e a política decide
        return False


def enter_sandbox() -> None:
    """No worker, antes de ler a entrada e de importar o decodificador. Na política fechada, sem sandbox o processo sai na hora
    com ``SANDBOX_EXIT`` (nada na saída); no modo degradado segue só com o que conseguiu aplicar."""
    if not confine() and FAIL_CLOSED:
        os._exit(SANDBOX_EXIT)


def may_spawn(log: logging.Logger) -> bool:
    """No pai, antes de buscar a imagem e de abrir um worker: o sandbox cabe aqui? Sem ele, a política decide e o código vai ao log
    uma vez."""
    if supported():
        return True
    report(log)
    return not FAIL_CLOSED


def refused(returncode: object, log: logging.Logger) -> bool:
    """No pai, depois do worker: ele se recusou por falta de sandbox? Nesse caso o código vai ao log (uma vez)."""
    if returncode != SANDBOX_EXIT:
        return False
    report(log)
    return True


def report(log: logging.Logger) -> None:
    """Uma linha por processo, só com o código (sem caminho nem detalhe)."""
    global _reported
    if _reported:
        return
    _reported = True
    if FAIL_CLOSED:
        log.error("Decodificador de imagens recusado: sem sandbox (%s)", UNAVAILABLE_CODE)
    else:
        log.warning("Decodificador de imagens sem sandbox completo (%s)", DEGRADED_CODE)


def kill_worker(process: Any) -> None:
    """Mata o grupo do worker (aberto com ``start_new_session``, então o grupo é o PID dele) e, se não der, o próprio processo. Só
    enquanto o worker não foi recolhido (``returncode is None``): depois disso o PID pode ser de outro."""
    if getattr(process, "returncode", None) is not None:
        return
    with suppress(ProcessLookupError, PermissionError, AttributeError):
        os.killpg(process.pid, signal.SIGKILL)
        return
    with suppress(ProcessLookupError):
        process.kill()  # o laço pode já ter recolhido o worker morto
