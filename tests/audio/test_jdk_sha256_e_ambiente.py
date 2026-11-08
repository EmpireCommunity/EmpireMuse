"""Fase 2.1 — (1) JDK baixado com SHA-256 fixo e (2) allowlist de ambiente do processo do Lavalink.

Contrato: BRIEFING-FASE2_1.md (itens 1 e 2). Interpretações: .qa-fase2_1-interp.txt (staging).
Sem rede, sem Java e sem produção: `requests.get` é um dublê, os arquivos de JDK são arquivos MÍNIMOS gerados em memória e
tudo roda em tmp_path. Os valores de SHA-256 reais do JDK NÃO são conferidos aqui (vêm do DevOps): só formato e uso.

API exigida do Backend (nomes fixados pelos testes):
  utils/branding.py       JDK_DOWNLOADS: Mapping[str, tuple[str, str]] com as chaves "windows-amd64", "windows-i586",
                          "linux-amd64", "linux-i586" -> (url oficial, sha256 em 64 hex minúsculos). Lido em tempo de chamada.
  local_lavalink.py       os 4 downloads do JDK passam por download_verified(url, destino, sha256); a extração só ocorre
                          depois da verificação; nenhum java.zip/java.tar.gz pré-existente é usado sem verificação;
                          lavalink_child_env(refresh_token, environ=os.environ) -> dict[str, str] (função pura);
                          o Popen do Lavalink SEMPRE recebe env=lavalink_child_env(token).
"""

import hashlib
import io
import os
import tarfile
import types
import zipfile
from pathlib import Path

import pytest

from empiremuse.utils import branding
from empiremuse.utils.music import local_lavalink as ll

# ============================================================================ constantes do contrato

JDK_KEYS = ("windows-amd64", "windows-i586", "linux-amd64", "linux-i586")
WINDOWS_ONLY_ALLOWED = (
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
)
OAUTH_VARS = (
    "PLUGINS_YOUTUBE_OAUTH_ENABLED",
    "PLUGINS_YOUTUBE_OAUTH_REFRESH_TOKEN",
    "PLUGINS_YOUTUBE_OAUTH_SKIP_INITIALIZATION",
)
# Valores sintéticos: nunca são segredos reais.
FAKE_TOKEN = "1//0gFAKE-refresh_TOKEN-qa-fase2-1-zZ9"
SECRET_SENTINELS = {
    "TOKEN": "sentinela-token-do-bot",
    "MONGO": "mongodb://usuario:sentinela-senha@host/db",
    "SPOTIFY_CLIENT_SECRET": "sentinela-spotify",
    "PANEL_COOKIE_SECRET": "sentinela-cookie-do-painel",
    "PANEL_OAUTH_CLIENT_SECRET": "sentinela-oauth-do-painel",
    "LASTFM_SECRET": "sentinela-lastfm",
}
NEVER_PASSED = (
    "JAVA_TOOL_OPTIONS",
    "_JAVA_OPTIONS",
    "JDK_JAVA_OPTIONS",
    "CLASSPATH",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "PYTHONPATH",
    "SPRING_APPLICATION_JSON",
    "SPRING_CONFIG_LOCATION",
    "LAVALINK_SERVER_PASSWORD",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "AWS_SECRET_ACCESS_KEY",
    "GITHUB_TOKEN",
    "DISCORD_TOKEN",
    "TOKEN_RESERVE_1",
    "PLUGINS_YOUTUBE_ENABLED",
    *SECRET_SENTINELS,
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fail(message: str):
    """Falha SEM eco de valores (o ambiente do desenvolvedor pode conter segredos reais)."""
    pytest.fail(message, pytrace=False)


# ============================================================================ fixtures do item 1 (JDK)


class Net:
    """Atende só as URLs em ``served``; qualquer outra é requisição inesperada. Suporta `with` e uso direto."""

    def __init__(self):
        self.served, self.calls, self.error = {}, [], None

    def get(self, url, *args, **kwargs):
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        assert url in self.served, f"requisição inesperada: {url}"
        return _Response(self.served[url])


class _Response:
    def __init__(self, data):
        self.data = data
        self.headers = {"content-length": str(len(data))}
        self.status_code = 200

    def iter_content(self, chunk_size=1):
        yield self.data

    def raise_for_status(self):
        return None

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_tar_gz(files: dict) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def make_zip(files: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def good_archive(key: str) -> bytes:
    """Mini-JDK: uma pasta raiz com bin/java (ou bin/java.exe); o conteúdo identifica a plataforma escolhida."""
    marker = f"JAVA-BOM-{key}".encode()
    if key.startswith("windows"):
        return make_zip({"jdk-17.0.13/bin/java.exe": marker, "jdk-17.0.13/release": b"ok"})
    return make_tar_gz({"jdk-21.0.3-lite/bin/java": marker, "jdk-21.0.3-lite/release": b"ok"})


def evil_archive(key: str) -> bytes:
    if key.startswith("windows"):
        return make_zip({"jdk-evil/bin/java.exe": b"JAVA-MALICIOSO", "jdk-evil/EVIL_MARKER": b"x"})
    return make_tar_gz({"jdk-evil/bin/java": b"JAVA-MALICIOSO", "jdk-evil/EVIL_MARKER": b"x"})


class OsProxy:
    """Só o módulo local_lavalink enxerga `os.name == "nt"`; o resto do processo (pytest, tmp_path) segue POSIX."""

    def __init__(self, real, name):
        self._real, self.name = real, name

    def __getattr__(self, attr):
        return getattr(self._real, attr)


def pretend_windows(monkeypatch):
    monkeypatch.setattr(ll, "os", OsProxy(os, "nt"))
    monkeypatch.setattr(ll.platform, "system", lambda: "Windows")
    if hasattr(ll, "sys"):
        monkeypatch.setattr(ll, "sys", types.SimpleNamespace(platform="win32"))


def tree(base: Path) -> set:
    return {str(p.relative_to(base)) for p in base.rglob("*") if p.is_file()}


@pytest.fixture
def jdk(monkeypatch, tmp_path):
    """Ambiente sem Java: Lavalink.jar/yml/plugins já presentes e verificados; JDK_DOWNLOADS trocado por mini-arquivos."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JAVA_HOME", raising=False)
    jar, yml = b"JAR", b"YML\n"
    plugins = {
        n: f"plugin {n}".encode()
        for n in (
            "youtube-plugin-2be8e542d3f6f178e048dca565892684c2e40177.jar",
            "lavasrc-plugin-4.8.3.jar",
            "lavalink-2.6.1.jar",
        )
    }
    monkeypatch.setattr(branding, "LAVALINK_JAR_SHA256", sha(jar))
    monkeypatch.setattr(branding, "LAVALINK_APPLICATION_YML_SHA256", sha(yml))
    monkeypatch.setattr(branding, "LAVALINK_PLUGIN_SHA256", {n: sha(d) for n, d in plugins.items()})
    (tmp_path / "Lavalink.jar").write_bytes(jar)
    (tmp_path / "application.yml").write_bytes(yml)
    (tmp_path / "plugins").mkdir()
    for name, data in plugins.items():
        (tmp_path / "plugins" / name).write_bytes(data)

    archives = {key: good_archive(key) for key in JDK_KEYS}
    test_urls = {
        key: f"https://example.test/jdk/{key}.{'zip' if key.startswith('windows') else 'tar.gz'}" for key in JDK_KEYS
    }
    monkeypatch.setattr(
        branding, "JDK_DOWNLOADS", {k: (test_urls[k], sha(archives[k])) for k in JDK_KEYS}, raising=False
    )

    net = Net()
    net.served = {test_urls[k]: archives[k] for k in JDK_KEYS}
    monkeypatch.setattr(ll.requests, "get", net.get)

    monkeypatch.setattr(
        ll, "validate_java", lambda cmd, debug=False: cmd if cmd != "java" and os.path.isfile(cmd) else None
    )

    monkeypatch.setattr(ll, "java_major_version", lambda cmd, debug=False: 25)
    real_popen = ll.subprocess.Popen
    started, extractions = [], []

    def popen(args, **kwargs):
        if args and args[0] == "tar":
            extractions.append(("tar", _archive_hash_in(args)))
            return real_popen(args, **kwargs)
        started.append((list(args), kwargs))

    monkeypatch.setattr(ll.subprocess, "Popen", popen)

    real_tar_open, real_zip = tarfile.open, zipfile.ZipFile

    def tar_open(name=None, *a, **k):
        if isinstance(name, (str, os.PathLike)):
            extractions.append(("tarfile", sha(Path(name).read_bytes()) if Path(name).is_file() else None))
        return real_tar_open(name, *a, **k)

    class RecordingZip(real_zip):
        def __init__(self, file, *a, **k):
            if isinstance(file, (str, os.PathLike)) and Path(file).is_file():
                extractions.append(("zipfile", sha(Path(file).read_bytes())))
            super().__init__(file, *a, **k)

    monkeypatch.setattr(tarfile, "open", tar_open)
    monkeypatch.setattr(zipfile, "ZipFile", RecordingZip)

    return types.SimpleNamespace(
        net=net,
        archives=archives,
        urls=test_urls,
        started=started,
        extractions=extractions,
        base=tmp_path,
        baseline=tree(tmp_path),
        monkeypatch=monkeypatch,
    )


def _archive_hash_in(args):
    for arg in args:
        if str(arg).endswith(".tar.gz") and Path(arg).is_file():
            return sha(Path(arg).read_bytes())
    return None


def linux(env, bits):
    env.monkeypatch.setattr(ll.platform, "architecture", lambda *a, **k: (bits, "ELF"))
    env.monkeypatch.setattr(ll.platform, "system", lambda: "Linux")
    key = "linux-amd64" if bits == "64bit" else "linux-i586"
    java = env.base / ".java" / f"Linux-{bits}-ELF" / "bin" / "java"
    return key, java, env.base / "java.tar.gz"


def windows(env, bits):
    pretend_windows(env.monkeypatch)
    env.monkeypatch.setattr(ll.platform, "architecture", lambda *a, **k: (bits, "WindowsPE"))
    wintmp = env.base / "wintmp"
    wintmp.mkdir()
    env.monkeypatch.setattr(ll.tempfile, "gettempdir", lambda: str(wintmp))
    env.baseline = tree(env.base)
    key = "windows-amd64" if bits == "64bit" else "windows-i586"
    java = wintmp / ".java" / f"Windows-{bits}-WindowsPE" / "bin" / "java.exe"
    return key, java, wintmp / "java.zip"


PLATFORMS = [("linux", "64bit"), ("linux", "32bit"), ("windows", "64bit"), ("windows", "32bit")]


def setup_platform(env, system, bits):
    return (linux if system == "linux" else windows)(env, bits)


# ============================================================================ (1b) comportamento dos downloads


@pytest.mark.parametrize("system,bits", PLATFORMS)
class TestJdkDownloadIsVerified:
    def test_extraction_happens_only_after_the_archive_matches_the_pinned_hash(self, jdk, system, bits):
        key, _, _ = setup_platform(jdk, system, bits)
        ll.run_lavalink()
        assert jdk.extractions, "nenhuma extração observada (tar/tarfile/zipfile)"
        expected = branding.JDK_DOWNLOADS[key][1]
        assert all(digest == expected for _, digest in jdk.extractions), (
            "o arquivo foi aberto/extraído antes de estar verificado (ou diferente do esperado)"
        )

    def test_a_preexisting_unverified_archive_is_never_extracted_when_the_download_fails(self, jdk, system, bits):
        key, java, archive = setup_platform(jdk, system, bits)
        archive.write_bytes(evil_archive(key))
        jdk.net.error = ll.requests.ConnectionError("sem rede")
        with pytest.raises(ll.LavalinkDownloadError):
            ll.run_lavalink()
        assert jdk.extractions == [] and not java.exists() and jdk.started == []

    def test_a_preexisting_archive_with_the_correct_hash_may_be_used(self, jdk, system, bits):
        key, java, archive = setup_platform(jdk, system, bits)
        archive.write_bytes(jdk.archives[key])
        ll.run_lavalink()
        assert java.read_bytes() == f"JAVA-BOM-{key}".encode() and len(jdk.started) == 1


# ============================================================================ (2a) lavalink_child_env — função pura


def sentinel_environ(**extra):
    base = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/bot",
        "LANG": "pt_BR.UTF-8",
        "LC_ALL": "C.UTF-8",
        "LC_CTYPE": "UTF-8",
        "TZ": "America/Sao_Paulo",
        "TMPDIR": "/tmp",
        "TEMP": "/tmp",
        "TMP": "/tmp",
        "JAVA_HOME": "/opt/jdk",
        "SERVER_PORT": "8123",
    }
    base.update({name: f"valor-{name}" for name in NEVER_PASSED if name not in SECRET_SENTINELS})
    base.update(SECRET_SENTINELS)
    base.update(extra)
    return base


class TestChildEnvContract:
    @pytest.mark.parametrize("name", list(NEVER_PASSED)[:2])
    def test_nothing_outside_the_allowlist_ever_passes(self, name):
        env = ll.lavalink_child_env(FAKE_TOKEN, {**sentinel_environ(), name: "qualquer"})
        assert name not in env

    def test_windows_only_names_are_not_passed_on_posix(self):
        assert ll.lavalink_child_env(None, {n: "x" for n in WINDOWS_ONLY_ALLOWED}) == {"SERVER_ADDRESS": "127.0.0.1"}

    def test_never_prints_or_logs_the_environment_or_the_token(self, capsys, caplog):
        caplog.set_level(0)
        ll.lavalink_child_env(FAKE_TOKEN, sentinel_environ())
        out = capsys.readouterr()
        text = out.out + out.err + "\n".join(f"{r.getMessage()} {r.args!r}" for r in caplog.records)
        if FAKE_TOKEN in text or any(value in text for value in SECRET_SENTINELS.values()):
            fail("lavalink_child_env escreveu ambiente/token em stdout, stderr ou log")


class TestChildEnvOauth:
    def test_the_token_only_exists_in_the_returned_dict(self):
        source = {"PATH": "/bin"}
        ll.lavalink_child_env(FAKE_TOKEN, source)
        assert source == {"PATH": "/bin"} and not [v for v in OAUTH_VARS if v in os.environ]


# ============================================================================ (2b) o Popen do Lavalink sempre recebe env=


@pytest.fixture
def lavalink_start(monkeypatch, tmp_path):
    """run_lavalink com Java 'instalado' e tudo verificado; registra os kwargs do Popen. Ambiente do bot com segredos fictícios."""
    monkeypatch.chdir(tmp_path)
    jar, yml = b"JAR", b"YML\n"
    plugins = {
        n: f"plugin {n}".encode()
        for n in (
            "youtube-plugin-2be8e542d3f6f178e048dca565892684c2e40177.jar",
            "lavasrc-plugin-4.8.3.jar",
            "lavalink-2.6.1.jar",
        )
    }
    monkeypatch.setattr(branding, "LAVALINK_JAR_SHA256", sha(jar))
    monkeypatch.setattr(branding, "LAVALINK_APPLICATION_YML_SHA256", sha(yml))
    monkeypatch.setattr(branding, "LAVALINK_PLUGIN_SHA256", {n: sha(d) for n, d in plugins.items()})
    (tmp_path / "Lavalink.jar").write_bytes(jar)
    (tmp_path / "application.yml").write_bytes(yml)
    (tmp_path / "plugins").mkdir()
    for name, data in plugins.items():
        (tmp_path / "plugins" / name).write_bytes(data)

    def no_network(*args, **kwargs):
        raise AssertionError("rede inesperada")

    monkeypatch.setattr(ll.requests, "get", no_network)
    monkeypatch.setattr(ll, "validate_java", lambda cmd, debug=False: cmd)
    monkeypatch.setattr(ll, "java_major_version", lambda cmd, debug=False: 25)
    monkeypatch.setattr(ll.platform, "architecture", lambda *a, **k: ("64bit", "ELF"))
    for name, value in SECRET_SENTINELS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/caminho/do/java")
    monkeypatch.setenv("SERVER_PORT", "8123")
    monkeypatch.setenv("JAVA_TOOL_OPTIONS", "-javaagent:/tmp/evil.jar")
    for name in OAUTH_VARS:
        monkeypatch.delenv(name, raising=False)
    records = []
    monkeypatch.setattr(ll.subprocess, "Popen", lambda args, **kwargs: records.append((list(args), kwargs)))
    return records


class TestPopenAlwaysGetsTheAllowlistedEnv:
    @pytest.mark.parametrize("token", [None, FAKE_TOKEN])
    def test_path_and_server_port_reach_the_child(self, lavalink_start, token):
        ll.run_lavalink(youtube_oauth_refresh_token=token)
        env = lavalink_start[0][1]["env"]
        assert env.get("PATH") == "/usr/bin:/bin:/caminho/do/java"
        assert env.get("SERVER_PORT") == "8123", "sem SERVER_PORT o Lavalink sobe numa porta que o bot não usa"

    def test_the_token_is_never_on_the_command_line_or_in_other_popen_arguments(self, lavalink_start):
        ll.run_lavalink(youtube_oauth_refresh_token=FAKE_TOKEN)
        args, kwargs = lavalink_start[0]
        assert not any(FAKE_TOKEN in a for a in args)
        assert FAKE_TOKEN not in repr({k: v for k, v in kwargs.items() if k != "env"})
