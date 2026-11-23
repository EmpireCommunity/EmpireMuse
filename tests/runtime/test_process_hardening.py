"""Endurecimento do processo na subida (``process_hardening``): a marcação vale no Linux, com um caso de controle sem ela.
Cada caso roda num processo à parte: o do pytest não muda."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from empiremuse import process_hardening

pytestmark = [
    pytest.mark.critical,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl só existe no Linux"),
]

ROOT = str(Path(__file__).resolve().parents[2])

PARENT = """
import os, subprocess, sys
sys.path.insert(0, sys.argv[1])
from empiremuse.process_hardening import is_dumpable, mark_non_dumpable
if sys.argv[2] == "mark":
    assert mark_non_dumpable() and is_dumpable() is False
child = '''
import os
try:
    open(f"/proc/{os.getppid()}/environ", "rb").read()
    print("read")
except PermissionError:
    print("denied")
'''
out = subprocess.run(
    [sys.executable, "-I", "-c", child], env={}, cwd="/", close_fds=True, start_new_session=True,
    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20,
)
print(out.stdout.decode().strip())
"""


def _run(*args: str) -> str:
    done = subprocess.run([sys.executable, "-I", "-c", *args], capture_output=True, timeout=30, check=True, cwd="/")
    return done.stdout.decode().strip()


def test_the_bot_process_is_marked_non_dumpable():
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "from empiremuse.process_hardening import is_dumpable, mark_non_dumpable;"
        "print(is_dumpable(), mark_non_dumpable(), is_dumpable())"
    )

    assert _run(code, ROOT) == "True True False"


def test_outside_linux_it_is_a_no_op():
    assert process_hardening.mark_non_dumpable(platform="darwin") is False
    assert process_hardening.is_dumpable(platform="win32") is None


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="precisa de um usuário sem privilégio")
@pytest.mark.parametrize(("mode", "expected"), [("mark", "denied"), ("plain", "read")])
def test_a_process_of_the_same_user_cannot_inspect_the_marked_process(mode, expected):
    assert _run(PARENT, ROOT, mode) == expected
