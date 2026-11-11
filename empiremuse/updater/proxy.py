"""Proxy do Docker para a atualização automática (P3): o bot nunca fala com o ``docker.sock``.

Arquivo único, só stdlib, rodado fora do venv: ``/usr/local/bin/python3 -I -S /app/empiremuse/updater/proxy.py``. O
``-I`` ignora ambiente e diretório do script, e o ``-S`` impede o ``site`` (sem ``.pth`` nem site-packages do venv, onde
moram as dependências do bot). Na subida o proxy confere as duas flags e que nenhum módulo fora da biblioteca padrão foi
importado; senão sai com erro.

O bot é tratado como possivelmente comprometido. Ele só consegue pedir cinco operações, todas sobre um único container
auxiliar, de nome fixo e rótulo ``org.empiremuse.role=updater``: criar (o corpo do bot é descartado, só o alvo
``X.Y.Z@sha256:<64hex>`` do ``Cmd`` é aproveitado e entra num molde fixo), iniciar, inspecionar (só ``State``), ler o log
(só as linhas do próprio script) e remover. Cada requisição ao daemon é montada do zero: nenhum cabeçalho nem byte do
bot é repassado. O auxiliar (``HELPER_SCRIPT``) puxa a imagem por digest, confere a versão dela e troca uma única linha
do compose.
"""

from __future__ import annotations

import dataclasses
import http.client
import http.server
import json
import logging
import os
import posixpath
import re
import signal
import socket
import socketserver
import stat
import struct
import sys
import sysconfig
import threading
import urllib.parse
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

DOCKER_API_VERSION = "v1.44"
API_NAME = "empiremuse-updater"
ROLE_LABEL = "org.empiremuse.role"
ROLE_VALUE = "updater"
LOG_PREFIX = "empiremuse-updater: "

HELPER_IMAGE_NAME = "docker"
HELPER_IMAGE_TAG = "29.9.2-cli"
HELPER_IMAGE_DIGEST = "sha256:5e6baf67325a79f64ed3bed6863954d5e05d0f3d4e57cbdb14809346814bbfdb"
HELPER_IMAGE = f"{HELPER_IMAGE_NAME}:{HELPER_IMAGE_TAG}@{HELPER_IMAGE_DIGEST}"
HELPER_SOCKET_BIND = "/var/run/docker.sock:/var/run/docker.sock"

DEFAULT_DOCKER_SOCKET = "/var/run/docker.sock"
DEFAULT_LISTEN_SOCKET = "/run/empiremuse-updater/updater.sock"
DEFAULT_PROJECT_NAME = "empiremuse"
DEFAULT_SERVICE = "bot"
DEFAULT_IMAGE_REPO = "empirecommunity/empiremuse"

MAX_BODY = 64 * 1024
MAX_TAIL = 200
MAX_LOG_BYTES = 16 * 1024
MAX_LOG_LINE = 1024
MAX_DAEMON_BODY = 1024 * 1024
MAX_PULL_BODY = 4 * 1024 * 1024
MAX_CONNECTIONS = 8
READ_TIMEOUT = 10
DAEMON_TIMEOUT = 30
PULL_TIMEOUT = 300

EXIT_UNSAFE_RUNTIME = 70
EXIT_INVALID_CONFIG = 2
EXIT_SERVER_FAILED = 1

_VERSION_PART = r"(?:0|[1-9][0-9]{0,3})"
_TARGET = re.compile(rf"{_VERSION_PART}\.{_VERSION_PART}\.{_VERSION_PART}@sha256:[0-9a-f]{{64}}")
_PATH = re.compile(r"/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*")
_QUERY = re.compile(r"[A-Za-z0-9._=&:-]*")
_API_PREFIX = re.compile(r"v1\.[0-9]{1,2}")
_TAIL = re.compile(r"[1-9][0-9]{0,2}")
_CONTENT_LENGTH = re.compile(r"[0-9]{1,6}")
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_ABSOLUTE_PATH = re.compile(r"/[A-Za-z0-9._/-]+")
_IMAGE_REPO = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]{1,5})?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+")
_UID = re.compile(r"[0-9]{1,10}")
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_STATE_TEXT = re.compile(r"[a-z]{0,32}")
_STATE_TIME = re.compile(r"[0-9TZ:.+-]{0,40}")
_SOCKET_PATH_LIMIT = 100
_IMAGE_REPO_LIMIT = 200

LOG = logging.getLogger("empiremuse-updater")


class RequestDenied(Exception):
    """A requisição foge da lista permitida (403)."""


class BadRequest(Exception):
    """A requisição é permitida, mas o corpo é inválido (400)."""


class DaemonError(Exception):
    """O daemon do Docker não respondeu como esperado (502)."""


class ConfigError(Exception):
    """Configuração do proxy inválida; a mensagem cita só os nomes das variáveis."""


# --------------------------------------------------------------------------- script do auxiliar

HELPER_SCRIPT = r"""
say() { printf 'empiremuse-updater: %s %s\n' "$1" "$2"; }
die() { say "$1" "$2"; exit "$2"; }

valid_num() {
  case $1 in
    "" | *[!0-9]*) return 1 ;;
    0) return 0 ;;
    0*) return 1 ;;
  esac
  [ "${#1}" -le 4 ]
}

split_version() {
  case $1 in *.*.*) ;; *) return 1 ;; esac
  v_major=${1%%.*}
  v_rest=${1#*.}
  v_minor=${v_rest%%.*}
  v_patch=${v_rest#*.}
  valid_num "$v_major" && valid_num "$v_minor" && valid_num "$v_patch"
}

valid_digest() {
  case $1 in sha256:*) ;; *) return 1 ;; esac
  v_hex=${1#sha256:}
  [ "${#v_hex}" -eq 64 ] || return 1
  case $v_hex in *[!0-9a-f]*) return 1 ;; esac
  return 0
}

[ "$#" -eq 5 ] || die args 64
target=$1
project=$2
dir=$3
service=$4
repo=$5

case $target in *@*) ;; *) die args 64 ;; esac
ver=${target%%@*}
digest=${target#*@}
split_version "$ver" || die args 64
t_key=$((v_major * 100000000 + v_minor * 10000 + v_patch))
valid_digest "$digest" || die args 64
case $dir in /*) ;; *) die args 64 ;; esac
[ -n "$project" ] && [ -n "$service" ] && [ -n "$repo" ] || die args 64

F=$dir/docker-compose.yml
bak=
tmp=$F.new-$$

# repo e serviço nunca viram regex nem sed: o prefixo é comparado por igualdade exata (awk lê de ENVIRON).
image_lines() {
  IMAGE_PREFIX="    image: $repo:" awk '
    BEGIN { p = ENVIRON["IMAGE_PREFIX"]; n = length(p) }
    substr($0, 1, n) == p { print substr($0, n + 1) }
  ' "$F"
}

dc() { docker compose -p "$project" --project-directory "$dir" -f "$F" "$@"; }

# Quem grava o compose ou o .env decide o que este auxiliar (root, com o docker.sock) sobe: só aceita o que é do próprio
# usuário do auxiliar, sem link simbólico e sem escrita para grupo e outros.
trusted() {
  [ ! -L "$1" ] || return 1
  owner=$(stat -c %u "$1") || return 1
  [ "$owner" = "$(id -u)" ] || return 1
  mode=$(stat -c %a "$1") || return 1
  [ $((0$mode & 022)) -eq 0 ]
}
[ -f "$F" ] && [ -r "$F" ] || die compose 65
trusted "$dir" || die compose-perm 65
trusted "$F" || die compose-perm 65
if [ -e "$dir/.env" ] || [ -L "$dir/.env" ]; then
  trusted "$dir/.env" || die compose-perm 65
fi

count=$(image_lines | awk 'END { print NR }') || die compose 65
[ "$count" = "1" ] || die compose 65
current=$(image_lines) || die compose 65
case $current in
  *@*) valid_digest "${current#*@}" || die compose 65 ;;
esac
c_ver=${current%%@*}
c_rc=0
case $c_ver in
  *-rc.*)
    valid_num "${c_ver#*-rc.}" || die compose 65
    c_ver=${c_ver%%-rc.*}
    c_rc=1
    ;;
esac
split_version "$c_ver" || die compose 65
c_key=$((v_major * 100000000 + v_minor * 10000 + v_patch))
if [ "$c_rc" -eq 1 ]; then
  [ "$t_key" -ge "$c_key" ] || die downgrade 67
else
  [ "$t_key" -gt "$c_key" ] || die downgrade 67
fi

docker pull "$repo:$target" >/dev/null 2>&1 || die pull 66

env_out=$(docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$repo:$target" 2>/dev/null) || die version 68
only=$(printf '%s\n' "$env_out" | grep '^EMPIREMUSE_VERSION=' || true)
[ "$only" = "EMPIREMUSE_VERSION=$ver" ] || die version 68

bak=$F.bak-$(date +%Y%m%d-%H%M%S)
cp -p "$F" "$bak" || die backup 65

new_line="    image: $repo:$target"
cp -p "$F" "$tmp" || die backup 65
if ! IMAGE_PREFIX="    image: $repo:" NEW_LINE="$new_line" awk '
  BEGIN { p = ENVIRON["IMAGE_PREFIX"]; n = length(p) }
  substr($0, 1, n) == p { print ENVIRON["NEW_LINE"]; next }
  { print }
' "$F" >"$tmp"; then
  rm -f "$tmp"
  die backup 65
fi
mv "$tmp" "$F" || { rm -f "$tmp"; die backup 65; }

fail=0
dc config -q >/dev/null 2>&1 || fail=1
if [ "$fail" -eq 0 ]; then
  images=$(dc config --images "$service" 2>/dev/null) || fail=1
  [ "$images" = "$repo:$target" ] || fail=1
fi
if [ "$fail" -eq 0 ]; then
  dc up -d --no-deps --wait --wait-timeout 300 "$service" >/dev/null 2>&1 || fail=1
fi

if [ "$fail" -ne 0 ]; then
  cp -p "$bak" "$tmp" || die rollback 71
  mv "$tmp" "$F" || { rm -f "$tmp"; die rollback 71; }
  if dc up -d --no-deps --wait --wait-timeout 300 "$service" >/dev/null 2>&1; then
    die rollback 70
  fi
  die rollback 71
fi

say done 0
"""


# --------------------------------------------------------------------------- regras puras


@dataclasses.dataclass(frozen=True)
class Route:
    """Operação permitida sobre o auxiliar: ``create``, ``start``, ``inspect``, ``logs`` ou ``remove``."""

    action: str
    tail: int = 0


@dataclasses.dataclass(frozen=True)
class Config:
    docker_socket: str
    listen_socket: str
    project_name: str
    project_dir: str
    service: str
    image_repo: str
    peer_uid: int | None = None

    @property
    def container_name(self) -> str:
        """Nome real do container auxiliar; ``API_NAME`` é só o nome que o bot enxerga."""
        return f"{self.project_name}-updater"


def _deny(reason: str) -> RequestDenied:
    return RequestDenied(reason)


def _parse_query(query: str) -> dict[str, str]:
    if not query:
        return {}
    if not _QUERY.fullmatch(query):
        raise _deny("query")
    params: dict[str, str] = {}
    for pair in query.split("&"):
        key, separator, value = pair.partition("=")
        if not key or not separator or "=" in value or key in params:
            raise _deny("query")
        params[key] = value
    return params


def _require_params(params: Mapping[str, str], expected: Mapping[str, str]) -> None:
    if dict(params) != dict(expected):
        raise _deny("query")


def resolve_route(method: str, target: str) -> Route:
    """Operação permitida para ``method`` + alvo CRU da linha de requisição; qualquer outra coisa levanta ``RequestDenied``.

    O alvo é validado como veio (``//``, ``%``, ``.`` e ``..`` ficam de fora), com o prefixo opcional ``/v1.NN`` removido.
    O nome do container na URL tem de ser exatamente ``API_NAME``.
    """
    path, question, query = target.partition("?")
    if not _PATH.fullmatch(path) or (question and not query):
        raise _deny("caminho")
    segments = path.split("/")[1:]
    if any(segment in (".", "..") for segment in segments):
        raise _deny("caminho")
    if _API_PREFIX.fullmatch(segments[0]):
        segments = segments[1:]
    params = _parse_query(query)

    match method, segments:
        case "POST", ["containers", "create"]:
            _require_params(params, {"name": API_NAME})
            return Route("create")
        case "POST", ["containers", name, "start"] if name == API_NAME:
            _require_params(params, {})
            return Route("start")
        case "GET", ["containers", name, "json"] if name == API_NAME:
            _require_params(params, {})
            return Route("inspect")
        case "GET", ["containers", name, "logs"] if name == API_NAME:
            tail = params.get("tail", "")
            if set(params) != {"stdout", "stderr", "tail"} or params["stdout"] != "1" or params["stderr"] != "1":
                raise _deny("query")
            if not _TAIL.fullmatch(tail) or int(tail) > MAX_TAIL:
                raise _deny("query")
            return Route("logs", int(tail))
        case "DELETE", ["containers", name] if name == API_NAME:
            _require_params(params, {})
            return Route("remove")
    raise _deny("rota")


def check_headers(items: Iterable[tuple[str, str]]) -> int:
    """Confere os cabeçalhos do bot e devolve o ``Content-Length`` (0 se ausente); levanta ``RequestDenied`` se algo perigoso."""
    lengths: list[str] = []
    for name, value in items:
        lowered = name.lower()
        if lowered in ("upgrade", "transfer-encoding", "expect"):
            raise _deny("cabeçalho")
        if lowered == "connection" and "upgrade" in value.lower():
            raise _deny("cabeçalho")
        if lowered == "content-length":
            lengths.append(value)
    if len(lengths) > 1:
        raise _deny("content-length duplicado")
    if not lengths:
        return 0
    if not _CONTENT_LENGTH.fullmatch(lengths[0]):
        raise _deny("content-length")
    length = int(lengths[0])
    if length > MAX_BODY:
        raise _deny("corpo grande demais")
    return length


def parse_create_target(body: bytes) -> str:
    """Alvo ``X.Y.Z@sha256:<64hex>`` do ``Cmd`` do corpo do bot; o resto do corpo é ignorado. Inválido → ``BadRequest``."""
    try:
        data = json.loads(body.decode("utf-8"))
    except UnicodeDecodeError, ValueError, RecursionError:
        raise BadRequest("corpo") from None
    command = data.get("Cmd") if isinstance(data, dict) else None
    if not (isinstance(command, list) and len(command) == 1 and isinstance(command[0], str)):
        raise BadRequest("Cmd")
    if not _TARGET.fullmatch(command[0]):
        raise BadRequest("alvo")
    return command[0]


def create_template(config: Config, target: str) -> dict[str, Any]:
    """Corpo exato enviado ao daemon para criar o auxiliar: nada vem do bot além de ``target``."""
    return {
        "Image": HELPER_IMAGE,
        "Entrypoint": ["/bin/sh", "-euc", HELPER_SCRIPT, "updater"],
        "Cmd": [target, config.project_name, config.project_dir, config.service, config.image_repo],
        "User": "0:0",
        "Env": ["DOCKER_CONFIG=/tmp/docker", "HOME=/tmp"],
        "Labels": {ROLE_LABEL: ROLE_VALUE},
        "HostConfig": {
            "Binds": [HELPER_SOCKET_BIND, f"{config.project_dir}:{config.project_dir}"],
            "Privileged": False,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "ReadonlyRootfs": True,
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=16m"},
            "NetworkMode": "none",  # só fala com o daemon pelo docker.sock; quem baixa a imagem é o daemon
            "AutoRemove": False,
            "RestartPolicy": {"Name": "no"},
            "Memory": 268435456,
            "PidsLimit": 128,
            "LogConfig": {"Type": "json-file", "Config": {"max-size": "1m", "max-file": "1"}},
        },
    }


def _demultiplex(raw: bytes) -> bytes:
    """Junta os quadros (8 bytes de cabeçalho + carga) do stream de log do daemon; quadro malformado encerra a leitura."""
    chunks: list[bytes] = []
    position = 0
    while position + 8 <= len(raw):
        stream, padding, size = (
            raw[position],
            raw[position + 1 : position + 4],
            int.from_bytes(raw[position + 4 : position + 8]),
        )
        if stream > 2 or padding != b"\0\0\0":
            break
        chunks.append(raw[position + 8 : position + 8 + size])
        position += 8 + size
    return b"".join(chunks)


def filter_log_lines(raw: bytes, tail: int) -> list[str]:
    """Só as linhas do próprio script (``LOG_PREFIX``), imprimíveis, até ``tail`` linhas e ``MAX_LOG_BYTES`` no total."""
    text = _demultiplex(raw).decode("utf-8", errors="replace")
    candidates = [
        line
        for line in text.split("\n")
        if line.startswith(LOG_PREFIX) and len(line) <= MAX_LOG_LINE and line.isprintable()
    ]
    kept: list[str] = []
    size = 0
    for line in reversed(candidates[-tail:] if tail > 0 else []):
        size += len(line.encode("utf-8")) + 1
        if size > MAX_LOG_BYTES:
            break
        kept.append(line)
    kept.reverse()
    return kept


def inspect_view(data: Mapping[str, Any]) -> dict[str, Any]:
    """Só o subconjunto de ``State`` que o bot precisa; qualquer valor fora do formato esperado vira vazio ou -1."""
    state = data.get("State")
    if not isinstance(state, dict):
        raise DaemonError("State")

    def text(key: str, pattern: re.Pattern[str]) -> str:
        value = state.get(key)
        return value if isinstance(value, str) and pattern.fullmatch(value) else ""

    exit_code = state.get("ExitCode")
    return {
        "State": {
            "Status": text("Status", _STATE_TEXT),
            "Running": state.get("Running") is True,
            "ExitCode": exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else -1,
            "StartedAt": text("StartedAt", _STATE_TIME),
            "FinishedAt": text("FinishedAt", _STATE_TIME),
        }
    }


_PACKAGE_DIRECTORIES = frozenset({"site-packages", "dist-packages"})


def _stdlib_roots() -> tuple[str, ...]:
    """Pastas da biblioteca padrão do interpretador (puras e de plataforma, com ``lib-dynload`` dentro), já resolvidas."""
    paths = {sysconfig.get_path("stdlib"), sysconfig.get_path("platstdlib")}
    return tuple(sorted({os.path.realpath(path) for path in paths if path}))


def _from_stdlib(name: str, module: Any, roots: Sequence[str]) -> bool:
    """O módulo é do próprio interpretador: embutido, congelado, ou arquivo (resolvido) dentro da pasta da stdlib.

    Decide pela ORIGEM, não pelo nome: o CPython 3.15 carrega ``_missing_stdlib_info`` (que não está em
    ``sys.stdlib_module_names``) quando um import opcional falha, e um módulo de terceiros pode ter nome de stdlib.
    ``site-packages`` e ``dist-packages`` ficam de fora mesmo quando moram dentro da pasta da stdlib.
    """
    if name in sys.builtin_module_names:
        return True
    if getattr(getattr(module, "__spec__", None), "origin", None) in ("built-in", "frozen"):
        return True
    filename = getattr(module, "__file__", None)
    if not isinstance(filename, str):
        return False
    real = os.path.realpath(filename)
    for root in roots:
        if real.startswith(root + os.sep):
            return not _PACKAGE_DIRECTORIES & set(real[len(root) + 1 :].split(os.sep))
    return False


def isolation_problems(flags: Any, modules: Mapping[str, Any]) -> list[str]:
    """Problemas que impedem a subida: falta ``-I`` ou ``-S``, ou há módulo carregado que não é do interpretador.

    ``modules`` é o ``sys.modules`` (nome → módulo); o ``__main__`` (o próprio script) é ignorado.
    """
    problems: list[str] = []
    if not flags.isolated:
        problems.append("falta o modo isolado (-I)")
    if not flags.no_site:
        problems.append("falta -S: o site do venv não pode rodar")
    roots = _stdlib_roots()
    foreign = sorted(
        {
            name.partition(".")[0]
            for name, module in modules.items()
            if module is not None and name != "__main__" and not _from_stdlib(name, module, roots)
        }
    )
    if foreign:
        problems.append("módulos fora da biblioteca padrão: " + ", ".join(foreign))
    return problems


def _valid_path(value: object) -> bool:
    return (
        isinstance(value, str)
        and _ABSOLUTE_PATH.fullmatch(value) is not None
        and not value.startswith("//")  # o normpath POSIX preserva o "//" inicial: "//" montaria a raiz do host
        and posixpath.normpath(value) == value
        and len(value.encode()) <= _SOCKET_PATH_LIMIT
    )


def load_config(environ: Mapping[str, str]) -> Config:
    """Lê e valida o ambiente (``UPDATER_*``); qualquer valor inválido levanta ``ConfigError`` citando só os nomes."""
    docker_socket = environ.get("UPDATER_DOCKER_SOCKET", DEFAULT_DOCKER_SOCKET)
    listen_socket = environ.get("UPDATER_LISTEN_SOCKET", DEFAULT_LISTEN_SOCKET)
    project_name = environ.get("UPDATER_PROJECT_NAME", DEFAULT_PROJECT_NAME)
    project_dir = environ.get("UPDATER_PROJECT_DIR", "")
    service = environ.get("UPDATER_SERVICE", DEFAULT_SERVICE)
    image_repo = environ.get("UPDATER_IMAGE_REPO", DEFAULT_IMAGE_REPO)
    peer_uid = environ.get("UPDATER_PEER_UID")

    invalid = [
        name
        for name, ok in (
            ("UPDATER_DOCKER_SOCKET", _valid_path(docker_socket)),
            ("UPDATER_LISTEN_SOCKET", _valid_path(listen_socket)),
            ("UPDATER_PROJECT_NAME", _NAME.fullmatch(project_name) is not None),
            ("UPDATER_PROJECT_DIR", _valid_path(project_dir)),
            ("UPDATER_SERVICE", _NAME.fullmatch(service) is not None),
            (
                "UPDATER_IMAGE_REPO",
                len(image_repo) <= _IMAGE_REPO_LIMIT and _IMAGE_REPO.fullmatch(image_repo) is not None,
            ),
            ("UPDATER_PEER_UID", peer_uid is None or _UID.fullmatch(peer_uid) is not None),
        )
        if not ok
    ]
    if invalid:
        raise ConfigError(", ".join(invalid))
    return Config(
        docker_socket=docker_socket,
        listen_socket=listen_socket,
        project_name=project_name,
        project_dir=project_dir,
        service=service,
        image_repo=image_repo,
        peer_uid=None if peer_uid is None else int(peer_uid),
    )


# --------------------------------------------------------------------------- daemon do Docker


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("docker", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._socket_path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


class DockerDaemon:
    """Cliente mínimo do daemon por socket Unix; cada chamada abre a própria conexão e monta os cabeçalhos do zero."""

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path

    def call(
        self,
        method: str,
        path: str,
        query: Mapping[str, str] | None = None,
        body: bytes | None = None,
        *,
        timeout: float = DAEMON_TIMEOUT,
        limit: int = MAX_DAEMON_BODY,
    ) -> tuple[int, bytes]:
        url = f"/{DOCKER_API_VERSION}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query, safe=":")
        connection = _UnixConnection(self._socket_path, timeout)
        try:
            connection.putrequest(method, url, skip_accept_encoding=True)
            connection.putheader("Connection", "close")
            if body is not None:
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Content-Length", str(len(body)))
            connection.endheaders(body)
            response = connection.getresponse()
            data = response.read(limit + 1)
        except OSError, http.client.HTTPException:
            raise DaemonError("daemon inacessível") from None
        finally:
            connection.close()
        if len(data) > limit:
            raise DaemonError("resposta grande demais")
        return response.status, data


def _json_object(data: bytes) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except UnicodeDecodeError, ValueError, RecursionError:
        raise DaemonError("resposta inválida") from None
    if not isinstance(value, dict):
        raise DaemonError("resposta inválida")
    return value


def _pull_failed(stream: bytes) -> bool:
    for line in stream.decode("utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError, RecursionError:
            continue
        if isinstance(event, dict) and ("error" in event or "errorDetail" in event):
            return True
    return False


class Updater:
    """Executa a operação já validada de uma ``Route`` contra o daemon e devolve ``(status, corpo JSON ou None)``."""

    def __init__(self, config: Config, daemon: DockerDaemon) -> None:
        self._config = config
        self._daemon = daemon

    def handle(self, route: Route, body: bytes) -> tuple[int, dict[str, Any] | None]:
        if route.action == "create":
            return self._create(body)
        info = self._inspect_helper()
        if info is None:
            return 404, {"message": "auxiliar não encontrado"}
        name = self._config.container_name
        match route.action:
            case "inspect":
                return 200, inspect_view(info)
            case "start":
                status, _ = self._daemon.call("POST", f"/containers/{name}/start")
                if status == 204:
                    return 204, None
                if status == 304:
                    return 409, {"message": "auxiliar já iniciado"}
            case "logs":
                query = {"stdout": "1", "stderr": "1", "tail": str(route.tail)}
                status, data = self._daemon.call("GET", f"/containers/{name}/logs", query)
                if status == 200:
                    return 200, {"lines": filter_log_lines(data, route.tail)}
            case "remove":
                status, _ = self._daemon.call("DELETE", f"/containers/{name}")
                if status == 204:
                    return 204, None
                if status == 409:
                    return 409, {"message": "auxiliar em execução"}
        raise DaemonError(f"{route.action}: {status}")

    def _inspect_helper(self) -> dict[str, Any] | None:
        status, data = self._daemon.call("GET", f"/containers/{self._config.container_name}/json")
        if status == 404:
            return None
        if status != 200:
            raise DaemonError(f"inspect: {status}")
        info = _json_object(data)
        container_config = info.get("Config")
        labels = container_config.get("Labels") if isinstance(container_config, dict) else None
        if not isinstance(labels, dict) or labels.get(ROLE_LABEL) != ROLE_VALUE:
            raise _deny("container sem o rótulo do auxiliar")
        return info

    def _ensure_helper_image(self) -> None:
        status, _ = self._daemon.call("GET", f"/images/{HELPER_IMAGE}/json")
        if status == 200:
            return
        if status != 404:
            raise DaemonError(f"imagem: {status}")
        query = {"fromImage": HELPER_IMAGE_NAME, "tag": HELPER_IMAGE_DIGEST}
        status, data = self._daemon.call("POST", "/images/create", query, timeout=PULL_TIMEOUT, limit=MAX_PULL_BODY)
        if status != 200 or _pull_failed(data):
            raise DaemonError("pull do auxiliar")

    def _create(self, body: bytes) -> tuple[int, dict[str, Any] | None]:
        target = parse_create_target(body)
        self._ensure_helper_image()
        payload = json.dumps(create_template(self._config, target), separators=(",", ":")).encode()
        status, data = self._daemon.call("POST", "/containers/create", {"name": self._config.container_name}, payload)
        if status == 201:
            container_id = _json_object(data).get("Id")
            if not isinstance(container_id, str) or not _CONTAINER_ID.fullmatch(container_id):
                raise DaemonError("create: Id")
            return 201, {"Id": container_id}
        if status == 409:
            return 409, {"message": "auxiliar já existe"}
        raise DaemonError(f"create: {status}")


# --------------------------------------------------------------------------- servidor


def _loggable_method(method: str | None) -> str:
    """Método HTTP como o bot mandou só se for letra maiúscula curta; senão ``?`` (o bot não injeta escapes no log)."""
    return method if method is not None and re.fullmatch(r"[A-Z]{1,16}", method) else "?"


def _loggable_path(target: str) -> str:
    path = target.partition("?")[0]
    return path if _PATH.fullmatch(path) else "-"


class Handler(http.server.BaseHTTPRequestHandler):
    """Uma requisição por conexão (HTTP/1.0, sem keep-alive); toda decisão sai numa linha de log sem corpo nem cabeçalhos."""

    server_version = "empiremuse-updater"
    sys_version = ""
    protocol_version = "HTTP/1.0"
    timeout = READ_TIMEOUT
    server: UnixHTTPServer

    def __getattr__(self, name: str) -> Any:
        # Todo método HTTP (inclusive CONNECT, PUT, HEAD…) cai no mesmo caminho, que o nega se não estiver na tabela.
        if name.startswith("do_"):
            return self._serve
        raise AttributeError(name)

    def handle_expect_100(self) -> bool:
        self.send_error(403)
        return False

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        LOG.info("%s - parser %d", _loggable_method(self.command), code)
        super().send_error(code, "negado" if code == 403 else None)

    def log_message(self, format: str, *args: Any) -> None:
        """O log da stdlib ecoa a linha de requisição do bot; só as linhas próprias de ``_log`` saem."""

    def _log(self, method: str, target: str, decision: str, status: int) -> None:
        LOG.info("%s %s %s %d", _loggable_method(method), _loggable_path(target), decision, status)

    def _reply(self, status: int, payload: dict[str, Any] | None) -> None:
        data = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _read_body(self, length: int) -> bytes:
        if not length:
            return b""
        data = self.rfile.read(length)
        if len(data) != length:
            raise BadRequest("corpo incompleto")
        return data

    def _serve(self) -> None:
        self.close_connection = True
        words = self.requestline.split()
        target = words[1] if len(words) == 3 else ""
        decision = "permitido"
        try:
            if len(words) != 3 or self.request_version not in ("HTTP/1.0", "HTTP/1.1"):
                raise _deny("requisição")
            length = check_headers(self.headers.items())
            route = resolve_route(self.command, target)
            if length and route.action != "create":
                raise _deny("corpo em rota sem corpo")
            status, payload = self.server.updater.handle(route, self._read_body(length))
        except RequestDenied:
            decision, status, payload = "negado", 403, {"message": "negado"}
        except BadRequest:
            decision, status, payload = "inválido", 400, {"message": "requisição inválida"}
        except DaemonError as error:
            LOG.warning("falha no daemon: %s", error)
            decision, status, payload = "erro", 502, {"message": "falha ao falar com o Docker"}
        self._log(self.command, target, decision, status)
        self._reply(status, payload)


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Servidor HTTP em socket Unix (nunca TCP), criado sob ``umask(0o117)``: o socket nasce 0660, sem ``chmod`` depois."""

    daemon_threads = True
    request_queue_size = MAX_CONNECTIONS

    def __init__(self, config: Config, daemon: DockerDaemon | None = None) -> None:
        self.updater = Updater(config, daemon or DockerDaemon(config.docker_socket))
        self._peer_uid = config.peer_uid
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        previous = os.umask(0o117)
        try:
            super().__init__(config.listen_socket, Handler)
        finally:
            os.umask(previous)

    def verify_request(self, request: Any, client_address: Any) -> bool:
        if self._peer_uid is None:
            return True
        credentials = request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", credentials)
        return uid == self._peer_uid

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def _remove_stale_socket(path: str) -> None:
    """Apaga o socket que um proxy anterior deixou no volume; qualquer outro tipo de arquivo no caminho é recusado."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise ConfigError("UPDATER_LISTEN_SOCKET (o caminho existe e não é um socket)")
    os.unlink(path)


def serve(config: Config) -> int:
    try:
        _remove_stale_socket(config.listen_socket)
        server = UnixHTTPServer(config)
    except (OSError, ConfigError) as error:
        print(f"updater-proxy: não foi possível abrir o socket: {error}", file=sys.stderr)
        return EXIT_SERVER_FAILED

    def stop(signum: int, frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    LOG.info("ouvindo em %s", config.listen_socket)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            os.unlink(config.listen_socket)
        except FileNotFoundError:
            pass
    return 0


def main(environ: Mapping[str, str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)
    problems = isolation_problems(sys.flags, dict(sys.modules))
    if problems:
        print("updater-proxy: ambiente inseguro: " + "; ".join(problems), file=sys.stderr)
        return EXIT_UNSAFE_RUNTIME
    try:
        config = load_config(os.environ if environ is None else environ)
    except ConfigError as error:
        print(f"updater-proxy: configuração inválida: {error}", file=sys.stderr)
        return EXIT_INVALID_CONFIG
    return serve(config)


if __name__ == "__main__":
    sys.exit(main())
