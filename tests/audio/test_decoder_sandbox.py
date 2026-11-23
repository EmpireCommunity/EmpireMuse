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


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="sem pidfd")
async def test_the_deadline_ends_the_whole_decoder_group(monkeypatch, tmp_path):
    """Um worker que deixa outro processo para trás: no prazo o grupo inteiro termina, não só o worker."""
    pid_file = tmp_path / "neto.pid"
    slow = tmp_path / "lento.py"
    slow.write_text(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(logo_module, "WORKER_TIMEOUT_S", 1.0)
    monkeypatch.setattr(logo_module, "_WORKER", slow)
    with pytest.raises(LogoError):
        await decode_in_worker("PNG", image_bytes("PNG"))
    try:
        fd = os.pidfd_open(int(pid_file.read_text()))
    except ProcessLookupError:
        return  # já terminou e foi recolhido
    try:
        readable, _, _ = select.select(
            [fd], [], [], 5
        )  # fica legível quando o processo termina; espera limitada, sem sleep
    finally:
        os.close(fd)
    assert readable
