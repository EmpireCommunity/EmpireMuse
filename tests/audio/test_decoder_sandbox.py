"""Sandbox dos decodificadores de imagem (``utils/sandbox.py``): o worker do logo decodifica dentro dele, fora dele não lê nada, não cria
processo nem arquivo, e sem sandbox (política fechada) não decodifica."""

import asyncio
import io
import logging
import os
import select
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from empiremuse.utils import sandbox
from empiremuse.utils.radio import logo as logo_module
from empiremuse.utils.radio.logo import LogoError, decode_in_worker

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "empiremuse" / "utils" / "radio" / "logo_worker.py"
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
needs_landlock = pytest.mark.skipif(sandbox.landlock_abi() == 0, reason="kernel sem Landlock")


def image_bytes(fmt: str) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(out, format=fmt)
    return out.getvalue()


def run_probe(tmp_path: Path, body: str, *args: str) -> list[str]:
    """Um processo como o worker (``python -I``, ``env={}``) que entra no sandbox e roda ``body``; devolve as linhas que ele imprimiu."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        f"import os, sys\nsys.path.insert(0, {str(ROOT)!r})\nfrom empiremuse.utils import sandbox\n{body}\n",
        encoding="utf-8",
    )
    done = subprocess.run(
        [sys.executable, "-I", str(probe), *args],
        capture_output=True,
        text=True,
        env={},
        cwd="/",
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.split()


def attempt(path_expr: str) -> str:
    return (
        f"try:\n    open({path_expr}, 'rb').read(1)\n    print('read')\nexcept PermissionError:\n    print('denied')\n"
    )


@needs_landlock
@pytest.mark.critical
@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
async def test_the_logo_worker_decodes_inside_the_sandbox(fmt):
    out = await decode_in_worker(fmt, image_bytes(fmt))
    assert out.startswith(b"\x89PNG\r\n\x1a\n")


@needs_landlock
@pytest.mark.critical
@pytest.mark.regression
def test_a_sandboxed_decoder_reads_nothing_outside_the_python_install(tmp_path):
    outside = tmp_path / "fora.txt"
    outside.write_text("x", encoding="utf-8")
    body = (
        "print('confined' if sandbox.confine() else 'open')\n"
        + attempt("sys.argv[1]")
        + attempt("'/proc/self/status'")
        + attempt("'/etc/passwd'")
        + "import PIL.Image\nprint('decoder')\n"
    )
    assert run_probe(tmp_path, body, str(outside)) == ["confined", "denied", "denied", "denied", "decoder"]


@needs_landlock
@pytest.mark.critical
@pytest.mark.regression
def test_a_sandboxed_decoder_grows_no_file(tmp_path):
    target = tmp_path / "saida.bin"
    body = (
        "f = open(sys.argv[1], 'wb', buffering=0)\n"
        "sandbox.confine()\n"
        "try:\n    f.write(b'x')\n    print('wrote')\nexcept OSError:\n    print('refused')\n"
    )
    assert run_probe(tmp_path, body, str(target)) == ["refused"]
    assert target.read_bytes() == b""


@needs_landlock
@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.skipif(IS_ROOT, reason="o limite de processos não vale para root; o CI roda sem root")
def test_a_sandboxed_decoder_starts_no_process(tmp_path):
    body = "sandbox.confine()\ntry:\n    os.fork()\n    print('forked')\nexcept OSError:\n    print('refused')\n"
    assert run_probe(tmp_path, body) == ["refused"]


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_without_the_sandbox_the_parent_opens_no_decoder_and_logs_one_code(monkeypatch, caplog):
    monkeypatch.setattr(sandbox, "landlock_abi", lambda: 0)
    monkeypatch.setattr(sandbox, "_reported", False)

    async def never(*args, **kwargs):
        raise AssertionError("o worker não pode abrir")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", never)
    with caplog.at_level(logging.WARNING, logger=logo_module.logger.name):
        for _ in range(2):
            with pytest.raises(LogoError) as raised:
                await decode_in_worker("PNG", image_bytes("PNG"))
            assert raised.value.code == "sandbox"
    assert [record.getMessage() for record in caplog.records].count(
        f"Decodificador de imagens recusado: sem sandbox ({sandbox.UNAVAILABLE_CODE})"
    ) == 1


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_the_logo_worker_refuses_to_decode_when_the_sandbox_fails(monkeypatch, tmp_path):
    """O worker de verdade, com o sandbox falhando: sai sem decodificar (política fechada), e o pai vê o código do sandbox."""
    monkeypatch.setattr(sandbox, "landlock_abi", lambda: 6)  # o pai acha que há sandbox; quem falha é o filho
    wrapper = tmp_path / "sem_sandbox.py"
    wrapper.write_text(
        "import runpy, sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from empiremuse.utils import sandbox\n"
        "sandbox.confine = lambda: False\n"
        f"sys.argv[0] = {str(WORKER)!r}\n"
        f"runpy.run_path({str(WORKER)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(logo_module, "_WORKER", wrapper)
    with pytest.raises(LogoError) as raised:
        await decode_in_worker("PNG", image_bytes("PNG"))
    assert raised.value.code == "sandbox"


@needs_landlock
@pytest.mark.critical
@pytest.mark.regression
def test_a_sandboxed_decoder_opens_no_socket(tmp_path):
    body = (
        "import socket\n"
        "print('confined' if sandbox.confine() else 'open')\n"
        "for family, kind in ((socket.AF_INET, socket.SOCK_STREAM), (socket.AF_INET6, socket.SOCK_STREAM),\n"
        "                     (socket.AF_INET, socket.SOCK_DGRAM), (socket.AF_UNIX, socket.SOCK_STREAM)):\n"
        "    try:\n        socket.socket(family, kind)\n        print('opened')\n"
        "    except PermissionError:\n        print('refused')\n"
    )
    assert run_probe(tmp_path, body) == ["confined", "refused", "refused", "refused", "refused"]


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.parametrize("abi", [0, 1, 3, 5])
def test_a_kernel_below_the_minimum_abi_is_not_enough(monkeypatch, caplog, abi):
    monkeypatch.setattr(sandbox, "landlock_abi", lambda: abi)
    monkeypatch.setattr(sandbox, "_reported", False)
    assert sandbox.supported() is False
    assert sandbox.may_spawn(logo_module.logger) is (not sandbox.FAIL_CLOSED)


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.parametrize(
    ("allowed", "app_root", "exposed"),
    [
        (["/"], "/app", True),
        (["/usr"], "/usr/src/app", True),
        (["/srv/app"], "/srv/app", True),
        (["/usr/local", "/opt/venv", "/usr/lib"], "/app", False),
        (["/ap"], "/app", False),
    ],
)
def test_an_allowed_path_that_contains_the_app_is_refused(allowed, app_root, exposed):
    assert sandbox.exposes_app(allowed, app_root) is exposed


@needs_landlock
@pytest.mark.critical
@pytest.mark.regression
def test_the_sandbox_refuses_when_an_allowed_path_contains_the_app(tmp_path):
    body = "sandbox._APP_ROOT = sandbox.python_paths()[0]\nprint('confined' if sandbox.confine() else 'refused')\n"
    assert run_probe(tmp_path, body) == ["refused"]


@pytest.mark.critical
def test_only_the_standard_library_loads_before_the_sandbox():
    code = (
        "import sys\n"
        "before = set(sys.modules)\n"
        f"sys.path.append({str(ROOT)!r})\n"
        "from empiremuse.utils import sandbox\n"
        "new = sorted(m for m in set(sys.modules) - before if m.split('.')[0] not in sys.stdlib_module_names)\n"
        "print(' '.join(new))\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, env={}, cwd="/", check=True
    )
    assert done.stdout.split() == ["empiremuse", "empiremuse.utils", "empiremuse.utils.sandbox"]


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="sem pidfd")
def test_killing_a_worker_ends_its_whole_group():
    """Um worker que deixou outro processo para trás: ``kill_worker`` encerra o grupo inteiro, não só o worker."""
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    worker = subprocess.Popen(
        [sys.executable, "-I", "-c", script], stdout=subprocess.PIPE, text=True, start_new_session=True
    )
    try:
        grandchild = int(worker.stdout.readline())  # bloqueia até o neto existir: sem espera por relógio
        sandbox.kill_worker(worker)
        worker.wait(timeout=10)
        try:
            fd = os.pidfd_open(grandchild)
        except ProcessLookupError:
            return  # já terminou e foi recolhido
        try:
            readable, _, _ = select.select([fd], [], [], 10)  # legível quando o processo termina
        finally:
            os.close(fd)
        assert readable
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.stdout.close()


class _StuckProcess:
    """Um worker falso que nunca responde: o prazo vence e o pai tem de encerrá-lo pelo ``kill_worker``."""

    pid = 4_000_000
    returncode = None

    def __init__(self):
        self.stdin = NS(write=lambda data: None, drain=AsyncMock(), close=lambda: None)
        self.stdout = NS(read=self._never)
        self.ended = asyncio.Event()

    async def _never(self, size):
        await self.ended.wait()
        return b""

    async def wait(self):
        await self.ended.wait()
        return self.returncode


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_the_deadline_hands_the_worker_to_kill_worker(monkeypatch):
    stuck = _StuckProcess()
    killed = []

    def kill_worker(process):
        killed.append(process)
        process.returncode = -9
        process.ended.set()

    async def spawn(*args, **kwargs):
        return stuck

    monkeypatch.setattr(sandbox, "landlock_abi", lambda: sandbox.MIN_ABI)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(sandbox, "kill_worker", kill_worker)
    monkeypatch.setattr(logo_module, "WORKER_TIMEOUT_S", 0.01)
    with pytest.raises(LogoError) as raised:
        await decode_in_worker("PNG", b"x")
    assert raised.value.code == "deadline" and killed == [stuck]


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_without_the_sandbox_the_service_fetches_nothing():
    fetched = []

    async def fetch(url):
        fetched.append(url)
        return b""

    class Source:
        def favicon_of(self, uuid):
            return "https://cdn.example/l.png"

        def knows(self, uuid):
            return True

    svc = logo_module.LogoService(Source(), fetch=fetch, ready=lambda: False)
    result = await svc.png("00000000-0000-0000-0000-000000000001")
    assert result == logo_module.LogoResult() and fetched == [] and svc.fetches == 0
