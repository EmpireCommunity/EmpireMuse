"""Fase 2, Trilha A2 — YouTube com cliente TV + OAuth (refresh token entregue ao Lavalink local pelo ambiente do processo).

Contrato: BRIEFING-FASE2.md ("Trilha A2" e notas seguintes). Interpretações: .qa-fase2a2-interp.txt (staging).
Sem rede, sem Java, sem Discord e sem produção: `subprocess.Popen` é um dublê e tudo roda em tmp_path.

API exigida do Backend (nomes fixados pelos testes):
  utils/branding.py        LAVALINK_BINARIES_URL -> release 0.0.5; LAVALINK_APPLICATION_YML_SHA256 = hash do yml da release;
                           LAVALINK_APPLICATION_YML_KNOWN_SHA256 inclui os yml 0.0.1 e 0.0.2 (substituídos na atualização).
  config_loader.py         DEFAULT_CONFIG["YOUTUBE_OAUTH_REFRESH_TOKEN"] = "" (segredo, texto).
  .example.env             YOUTUBE_OAUTH_REFRESH_TOKEN= (vazio).
  local_lavalink.py        run_lavalink(..., *, lavalink_file_sha256=None, youtube_oauth_refresh_token=None): com token, o
                           Popen recebe env= (cópia de os.environ + as 3 variáveis PLUGINS_YOUTUBE_OAUTH_*); sem token, nada.
  utils/client.py          start_lavalink passa youtube_oauth_refresh_token=self.config['YOUTUBE_OAUTH_REFRESH_TOKEN'].
  modules/legacy_cmds.py   exportsource nunca grava o token no .env exportado.
"""

import ast
import asyncio
import hashlib
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from empiremuse.utils import branding
from empiremuse.utils.music import local_lavalink as ll
from tests.support.raiz import RAIZ

pytestmark = pytest.mark.critical

ROOT = RAIZ
STAGING_ENV = os.environ.get("EMPIREMUSE_STAGING")
RELEASE_005 = Path(STAGING_ENV) / "ll-binaries-0.0.5" if STAGING_ENV else None


OAUTH_ENV_VARS = (
    "PLUGINS_YOUTUBE_OAUTH_ENABLED",
    "PLUGINS_YOUTUBE_OAUTH_REFRESH_TOKEN",
    "PLUGINS_YOUTUBE_OAUTH_SKIP_INITIALIZATION",
)
CONFIG_KEY = "YOUTUBE_OAUTH_REFRESH_TOKEN"
# Valor sintético com formato de refresh token do Google; nunca é um segredo real.
FAKE_TOKEN = "1//0gFAKE-refresh_TOKEN-qa-fase2a2-zZ9"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def assert_no_token(where: str, *texts) -> None:
    """Falha sem imprimir o texto inspecionado (ele pode conter segredos reais do ambiente de desenvolvimento)."""
    if any(FAKE_TOKEN in text for text in texts):
        pytest.fail(f"o refresh token vazou em: {where}", pytrace=False)


# ============================================================================ (3) env do Popen


@pytest.fixture
def started(monkeypatch, tmp_path):
    """Executa run_lavalink sem rede/Java; registra (args, kwargs) de cada Popen. Os binários verificados já existem em tmp_path."""
    monkeypatch.chdir(tmp_path)
    jar, yml = b"JAR-A2", b"YML-A2\n"
    plugins = {name: f"plugin {name}".encode() for name in branding.LAVALINK_PLUGIN_SHA256}
    monkeypatch.setattr(branding, "LAVALINK_JAR_SHA256", sha(jar))
    monkeypatch.setattr(branding, "LAVALINK_APPLICATION_YML_SHA256", sha(yml))
    monkeypatch.setattr(branding, "LAVALINK_PLUGIN_SHA256", {n: sha(d) for n, d in plugins.items()})
    (tmp_path / "Lavalink.jar").write_bytes(jar)
    (tmp_path / "application.yml").write_bytes(yml)
    (tmp_path / "plugins").mkdir()
    for name, data in plugins.items():
        (tmp_path / "plugins" / name).write_bytes(data)

    def no_network(*args, **kwargs):
        raise AssertionError("rede inesperada: tudo já está presente e verificado")

    monkeypatch.setattr(ll.requests, "get", no_network)
    monkeypatch.setattr(ll, "validate_java", lambda cmd, debug=False: cmd)
    monkeypatch.setattr(ll, "java_major_version", lambda cmd, debug=False: 25)
    monkeypatch.setattr(ll.platform, "architecture", lambda *a, **k: ("64bit", "ELF"))
    for var in OAUTH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/caminho/do/java")
    records = []

    class FakePopen:
        def __init__(self, args, **kwargs):
            records.append((list(args), kwargs))

    monkeypatch.setattr(ll.subprocess, "Popen", FakePopen)
    return records


def start(token=None, **kwargs):
    if token is _OMIT:
        return ll.run_lavalink(lavalink_file_url=branding.LAVALINK_JAR_URL, **kwargs)
    return ll.run_lavalink(lavalink_file_url=branding.LAVALINK_JAR_URL, youtube_oauth_refresh_token=token, **kwargs)


_OMIT = object()


def env_of(record) -> dict:
    kwargs = record[1]
    env = kwargs.get("env")
    return dict(env) if env is not None else {}


class TestRunLavalinkOauthEnv:
    def test_with_token_the_java_process_gets_the_three_variables(self, started):
        start(FAKE_TOKEN)
        assert len(started) == 1
        env = env_of(started[0])
        assert env.get("PLUGINS_YOUTUBE_OAUTH_ENABLED") == "true"
        assert env.get("PLUGINS_YOUTUBE_OAUTH_REFRESH_TOKEN") == FAKE_TOKEN
        assert env.get("PLUGINS_YOUTUBE_OAUTH_SKIP_INITIALIZATION") == "true"

    def test_no_log_record_contains_the_token(self, started, caplog):
        caplog.set_level(0)
        start(FAKE_TOKEN)
        text = "\n".join(f"{r.getMessage()} {r.args!r} {r.exc_text}" for r in caplog.records)
        assert_no_token("registros de log", text)


# ============================================================================ (4) o token nunca vaza


@pytest.fixture
def export_env(monkeypatch, tmp_path):
    """Roda `Owner.exportsource` sem Discord/git: captura o conteúdo do .env que iria dentro do source.zip."""
    from empiremuse.cogs import legacy_cmds

    monkeypatch.chdir(tmp_path)
    shutil.copyfile(ROOT / ".example.env", tmp_path / ".example.env")
    (tmp_path / "git_dir").mkdir()
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "git_dir"))
    monkeypatch.delenv(CONFIG_KEY, raising=False)
    snapshots, sent = [], []

    async def fake_run_command(cmd):
        return ""

    monkeypatch.setattr(legacy_cmds, "run_command", fake_run_command)

    def fake_zip_dir(self, filelist):
        snapshots.append((tmp_path / ".env-temp").read_text(encoding="utf-8"))
        (tmp_path / "source.zip").write_bytes(b"PK")

    monkeypatch.setattr(legacy_cmds.Owner, "zip_dir", fake_zip_dir)

    async def author_send(*args, **kwargs):
        sent.append((args, kwargs))
        return SimpleNamespace(jump_url="https://discord.test/msg")

    async def run_in_executor(executor, func, *args):
        return func(*args)

    bot = SimpleNamespace(
        pool=SimpleNamespace(secret_values=lambda: frozenset()),
        config={"SOURCE_REPO": "https://example.test/repo.git", "EXTRA_HINTS": ""},
        loop=SimpleNamespace(run_in_executor=run_in_executor),
        get_color=lambda me: 0,
        user="bot#0000",
    )
    cog = legacy_cmds.Owner(bot)
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=1, send=author_send), guild=SimpleNamespace(me=None), send=mock.AsyncMock()
    )

    def run_export(flags=""):
        return asyncio.run(legacy_cmds.Owner.exportsource.callback(cog, ctx, flags=flags))

    return SimpleNamespace(
        run=run_export, snapshots=snapshots, sent=sent, tmp=tmp_path, ctx=ctx, monkeypatch=monkeypatch
    )


class TestExportNeverCarriesTheToken:
    def test_token_from_config_json_is_not_exported(self, export_env):
        (export_env.tmp / "config.json").write_text(f'{{"{CONFIG_KEY}": "{FAKE_TOKEN}"}}', encoding="utf-8")
        export_env.run()
        assert_no_token(".env exportado", export_env.snapshots[0])


SINK_NAMES = frozenset(
    {
        "print",
        "debug",
        "info",
        "warning",
        "warn",
        "error",
        "exception",
        "critical",
        "log",
        "send",
        "send_message",
        "respond",
        "followup",
        "reply",
        "edit",
        "edit_message",
        "set_footer",
        "write",
    }
)
PRODUCTION_DIRS = ("empiremuse",)
PRODUCTION_FILES = ("main.py",)


def _production_python_files():
    assert (ROOT / "empiremuse").is_dir(), "empiremuse/ deve ser o pacote raiz de produção"
    files = [ROOT / name for name in PRODUCTION_FILES if (ROOT / name).is_file()]
    for directory in PRODUCTION_DIRS:
        files.extend(p for p in (ROOT / directory).rglob("*.py") if "__pycache__" not in p.parts)
    return sorted(files)


def _call_name(node: ast.Call) -> str:
    func = node.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def _is_whole_config(node: ast.AST) -> bool:
    """`config`, `CONFIG`, `self.config`, `bot.config`, `dict(os.environ)` passados inteiros a um destino de saída."""
    if isinstance(node, ast.Name):
        return node.id in {"config", "CONFIG", "DEFAULT_CONFIG", "SECRETS"}
    if isinstance(node, ast.Attribute):
        return node.attr == "config"
    return False


class TestNoOutputSinkReceivesTheToken:
    """Guarda estática: nenhuma chamada de print/log/envio ao Discord referencia o token nem despeja a config inteira."""

    def test_no_sink_call_dumps_the_whole_config(self):
        offenders = []
        for path in _production_python_files():
            tree = ast.parse(path.read_text("utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and _call_name(node) in SINK_NAMES):
                    continue
                values = list(node.args) + [kw.value for kw in node.keywords]
                if any(
                    _is_whole_config(v) or (isinstance(v, ast.FormattedValue) and _is_whole_config(v.value))
                    for v in values
                ):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
                for sub in ast.walk(node):
                    if isinstance(sub, ast.FormattedValue) and _is_whole_config(sub.value):
                        offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        assert sorted(set(offenders)) == [], (
            f"saída com a configuração inteira (vazaria segredos): {sorted(set(offenders))}"
        )


# ============================================================================ (5) application.yml 0.0.5


@pytest.fixture(scope="module")
def release_005():
    if not RELEASE_005 or not RELEASE_005.is_dir():
        pytest.skip("artefatos da release 0.0.5 ausentes; defina a variável EMPIREMUSE_STAGING para habilitar")
    return RELEASE_005


@pytest.fixture(scope="module")
def yml_005(release_005):
    from ruamel.yaml import YAML

    return YAML(typ="safe").load((release_005 / "application.yml").read_text("utf-8"))
