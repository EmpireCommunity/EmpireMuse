"""A17-A21 (item 18, P3) — o proxy de atualização, caixa-preta: o ``proxy.py`` roda como processo (``python -I -S``) contra um daemon falso.

Só se usam os nomes que o contrato fixa: o arquivo ``empiremuse/updater/proxy.py``, as variáveis ``UPDATER_*``, as rotas e o
template. Uma requisição negada responde 403 e NÃO chega ao daemon (``daemon.requisicoes`` fica vazio); as permitidas chegam
reescritas do zero. O nome na API é ``empiremuse-updater``; o contêiner real é ``<UPDATER_PROJECT_NAME>-updater`` (aqui ``e2e-updater``).
Sem ``sleep``: a espera do socket é uma tentativa de conexão enquanto o processo vive.
"""

import json
import os
import re
import socket
import stat
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from tests.support.daemon_falso import DaemonFalso, requisicao_crua
from tests.support.raiz import RAIZ

pytestmark = pytest.mark.critical

PROXY = RAIZ / "empiremuse" / "updater" / "proxy.py"
DIGEST = "sha256:" + "ab" * 32
ALVO = f"2.1.1@{DIGEST}"
CONTAINER_API = "empiremuse-updater"
CONTAINER_REAL = "e2e-updater"
PREFIXO_LOG = "empiremuse-updater: "
CORPO_VALIDO = json.dumps({"Cmd": [ALVO]}).encode()
TIPO_JSON = ("Content-Type", "application/json")

CONFIG = {
    "UPDATER_PROJECT_NAME": "e2e",
    "UPDATER_PROJECT_DIR": "/srv/e2e",
    "UPDATER_SERVICE": "bot",
    "UPDATER_IMAGE_REPO": "empirecommunity/empiremuse",
}


def ambiente(tmp_path: Path, **extras) -> dict:
    return {
        "PATH": "/usr/bin:/bin",
        "UPDATER_DOCKER_SOCKET": str(tmp_path / "docker.sock"),
        "UPDATER_LISTEN_SOCKET": str(tmp_path / "updater.sock"),
        **CONFIG,
        **extras,
    }


def subir(env: dict, *flags: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, *(flags or ("-I", "-S")), str(PROXY)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def esperar_socket(processo: subprocess.Popen, caminho: str) -> None:
    """Tenta conectar enquanto o processo vive: sem sleep nem prazo em tempo real."""
    while processo.poll() is None:
        with socket.socket(socket.AF_UNIX) as teste:
            try:
                teste.connect(caminho)
                return
            except OSError:
                continue
    pytest.fail(f"o proxy saiu antes de abrir o socket: {processo.stderr.read()}", pytrace=False)


@pytest.fixture(scope="module")
def mundo(tmp_path_factory):
    pasta = tmp_path_factory.mktemp("proxy")
    daemon = DaemonFalso(str(pasta / "docker.sock"))
    processo = subir(ambiente(pasta))
    esperar_socket(processo, str(pasta / "updater.sock"))
    yield daemon, str(pasta / "updater.sock")
    processo.terminate()
    processo.wait(timeout=20)
    daemon.parar()


@pytest.fixture
def daemon(mundo):
    mundo[0].reiniciar()
    return mundo[0]


@pytest.fixture
def pedir(mundo):
    return lambda *args, **kwargs: requisicao_crua(mundo[1], *args, **kwargs)


def semear(daemon, **kwargs):
    daemon.adicionar_container(CONTAINER_REAL, **kwargs)


def chegou(daemon, metodo, caminho):
    return [r for r in daemon.requisicoes if r.metodo == metodo and r.caminho == caminho]


# ============================================================================ A17 — rotas permitidas chegam exatas


def test_create_reaches_the_daemon_with_the_real_name_and_nothing_else_in_the_query(daemon, pedir):
    status, _ = pedir("POST", f"/containers/create?name={CONTAINER_API}", [TIPO_JSON], CORPO_VALIDO)

    assert status == 201
    (criacao,) = chegou(daemon, "POST", "/containers/create")
    assert parse_qs(criacao.query) == {"name": [CONTAINER_REAL]}


def test_no_header_or_byte_of_the_bot_reaches_the_daemon(daemon, pedir):
    cabecalhos = [
        TIPO_JSON,
        ("Authorization", "Bearer segredo-do-bot"),
        ("X-Registry-Auth", "segredo-do-bot"),
        ("Cookie", "sessao=segredo-do-bot"),
        ("X-Forwarded-For", "6.6.6.6"),
        ("X-Segredo", "segredo-do-bot"),
    ]

    status, _ = pedir("POST", f"/containers/create?name={CONTAINER_API}", cabecalhos, CORPO_VALIDO)

    assert status == 201
    for requisicao in daemon.requisicoes:
        assert "segredo-do-bot" not in requisicao.bruto
        assert b"segredo-do-bot" not in requisicao.corpo
        assert not {"authorization", "x-registry-auth", "cookie", "x-forwarded-for", "x-segredo"} & set(
            requisicao.cabecalhos
        )


# ============================================================================ A17 — tudo o mais é negado e não chega ao daemon

NEGADAS = [
    ("GET", "/_ping", [], b""),
    ("GET", "/version", [], b""),
    ("GET", "/v1.47/version", [], b""),
    ("POST", "/images/create?fromImage=alpine&tag=latest", [], b""),
    ("POST", "/build", [], b""),
    ("POST", f"/containers/{CONTAINER_API}/exec", [TIPO_JSON], b"{}"),
    ("POST", "/containers/outro/start", [], b""),
    ("GET", "/containers/outro/json", [], b""),
    ("GET", "/containers/outro/logs?stdout=1&stderr=1&tail=5", [], b""),
    ("DELETE", "/containers/outro", [], b""),
    ("POST", "/containers/create?name=outro", [TIPO_JSON], CORPO_VALIDO),
    ("POST", "/containers/create", [TIPO_JSON], CORPO_VALIDO),
    ("DELETE", f"/containers/{CONTAINER_API}?force=1", [], b""),
    ("DELETE", f"/containers/{CONTAINER_API}?v=1", [], b""),
    ("CONNECT", f"/containers/{CONTAINER_API}/json", [], b""),
    ("PUT", f"/containers/{CONTAINER_API}/start", [], b""),
    ("POST", f"/containers/{CONTAINER_API}/json", [], b""),
    ("GET", f"/containers/{CONTAINER_API}/start", [], b""),
    ("POST", f"/containers/{CONTAINER_API}/stop", [], b""),
    ("POST", f"/containers/{CONTAINER_API}/start", [("Upgrade", "tcp")], b""),
    ("POST", f"/containers/{CONTAINER_API}/start", [("Connection", "Upgrade")], b""),
    ("POST", f"/containers/create?name={CONTAINER_API}", [TIPO_JSON, ("Transfer-Encoding", "chunked")], b"0\r\n\r\n"),
    ("POST", f"/containers/create?name={CONTAINER_API}", [TIPO_JSON, ("Expect", "100-continue")], CORPO_VALIDO),
    (
        "POST",
        f"/containers/create?name={CONTAINER_API}",
        [TIPO_JSON, ("Content-Length", "5"), ("Content-Length", "5")],
        b"{...}",
    ),
    ("GET", f"/containers/{CONTAINER_API}/%2e%2e/json", [], b""),
    ("GET", f"/containers/{CONTAINER_API}%2fjson", [], b""),
    ("GET", f"/containers//{CONTAINER_API}/json", [], b""),
    ("GET", f"/containers/{CONTAINER_API}/../outro/json", [], b""),
    ("GET", f"/containers/./{CONTAINER_API}/json", [], b""),
    ("GET", f"/containers/{CONTAINER_API}/logs?stdout=1&stderr=1&tail=1&tail=2", [], b""),
    ("GET", f"/containers/{CONTAINER_API}/logs?stdout=1&stderr=1&tail=5&follow=1", [], b""),
    ("GET", f"/containers/{CONTAINER_API}/logs?stdout=1&tail=5", [], b""),
    ("GET", f"/containers/{CONTAINER_API}/json?size=1", [], b""),
    ("POST", f"/containers/create?name={CONTAINER_API}", [TIPO_JSON], b" " * (64 * 1024 + 1)),
    ("GET", f"/containers/{CONTAINER_API}/json", [TIPO_JSON], b"{}"),
    ("POST", f"/containers/{CONTAINER_API}/start", [TIPO_JSON], b"{}"),
    ("DELETE", f"/containers/{CONTAINER_API}", [TIPO_JSON], b"{}"),
]


@pytest.mark.parametrize(
    ("metodo", "alvo", "cabecalhos", "corpo"), NEGADAS, ids=[f"{m} {a[:60]}" for m, a, _, _ in NEGADAS]
)
def test_everything_else_is_forbidden_and_never_reaches_the_daemon(daemon, pedir, metodo, alvo, cabecalhos, corpo):
    semear(daemon)
    daemon.adicionar_container("outro")

    status, _ = pedir(metodo, alvo, cabecalhos, corpo)

    assert status == 403
    assert daemon.requisicoes == []


@pytest.mark.parametrize("tail", ["0", "201", "-1", "abc", ""])
def test_the_log_tail_is_limited_to_1_through_200(daemon, pedir, tail):
    semear(daemon)

    status, _ = pedir("GET", f"/containers/{CONTAINER_API}/logs?stdout=1&stderr=1&tail={tail}")

    assert status in (400, 403)
    assert chegou(daemon, "GET", f"/containers/{CONTAINER_REAL}/logs") == []


# ============================================================================ A18 — create: o corpo do bot é descartado


def criado_no_daemon(daemon) -> dict:
    (criacao,) = chegou(daemon, "POST", "/containers/create")
    return json.loads(criacao.corpo)


def conferir_template(recebido: dict, alvo: str) -> None:
    """O que o daemon recebe é o template fixo; só a imagem (digest fixo no código) e o script (constante) não são conhecidos aqui."""
    assert re.fullmatch(r".+@sha256:[0-9a-f]{64}", recebido["Image"])
    assert recebido["Entrypoint"][:2] == ["/bin/sh", "-euc"] and len(recebido["Entrypoint"]) == 4
    assert recebido["Entrypoint"][3] == "updater" and "docker" in recebido["Entrypoint"][2]
    assert recebido == {
        "Image": recebido["Image"],
        "Entrypoint": recebido["Entrypoint"],
        "Cmd": [alvo, "e2e", "/srv/e2e", "bot", "empirecommunity/empiremuse"],
        "User": "0:0",
        "Env": ["DOCKER_CONFIG=/tmp/docker", "HOME=/tmp"],
        "Labels": {"org.empiremuse.role": "updater"},
        "HostConfig": {
            "Binds": ["/var/run/docker.sock:/var/run/docker.sock", "/srv/e2e:/srv/e2e"],
            "Privileged": False,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "ReadonlyRootfs": True,
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=16m"},
            "NetworkMode": "none",
            "AutoRemove": False,
            "RestartPolicy": {"Name": "no"},
            "Memory": 268435456,
            "PidsLimit": 128,
            "LogConfig": {"Type": "json-file", "Config": {"max-size": "1m", "max-file": "1"}},
        },
    }


HOSTIS = {
    "Image": "evil/image",
    "Cmd": [ALVO],
    "Entrypoint": ["/bin/sh"],
    "User": "0",
    "Env": ["TOKEN=x"],
    "Privileged": True,
    "HostConfig": {"Privileged": True, "Binds": ["/:/host"], "NetworkMode": "host", "CapAdd": ["ALL"]},
    "hostconfig": {"Privileged": True, "Binds": ["/:/host"]},
    "HOSTCONFIG": {"PidMode": "host"},
    "Labels": {"org.empiremuse.role": "updater", "extra": "1"},
}


def test_any_body_from_the_bot_becomes_exactly_the_fixed_template(daemon, pedir):
    status, _ = pedir("POST", f"/containers/create?name={CONTAINER_API}", [TIPO_JSON], json.dumps(HOSTIS).encode())

    assert status == 201
    conferir_template(criado_no_daemon(daemon), ALVO)


def test_duplicate_keys_never_smuggle_anything_into_the_template(daemon, pedir):
    corpo = f'{{"Cmd": ["lixo"], "HostConfig": {{"Privileged": true}}, "Cmd": ["{ALVO}"], "HostConfig": {{"Binds": ["/:/h"]}}}}'.encode()

    status, _ = pedir("POST", f"/containers/create?name={CONTAINER_API}", [TIPO_JSON], corpo)

    assert status == 201
    conferir_template(criado_no_daemon(daemon), ALVO)


INVALIDOS = [
    b"",
    b"nao e json",
    b"[]",
    b'"texto"',
    b"{}",
    json.dumps({"Cmd": []}).encode(),
    json.dumps({"Cmd": "2.1.1"}).encode(),
    json.dumps({"Cmd": [ALVO, ALVO]}).encode(),
    json.dumps({"Cmd": [7]}).encode(),
    json.dumps({"Cmd": ["latest"]}).encode(),
    json.dumps({"Cmd": ["2.1.1"]}).encode(),
    json.dumps({"Cmd": ["2.1.1@sha256:abc"]}).encode(),
    json.dumps({"Cmd": [f"v2.1.1@{DIGEST}"]}).encode(),
    json.dumps({"Cmd": [f"02.1.1@{DIGEST}"]}).encode(),
    json.dumps({"Cmd": [f"2.1.1@sha256:{'AB' * 32}"]}).encode(),
    json.dumps({"Cmd": [f"2.1.1@{DIGEST}; rm -rf /"]}).encode(),
    json.dumps({"Cmd": [f"2.1.1@{DIGEST}\n"]}).encode(),
]


@pytest.mark.parametrize("corpo", INVALIDOS, ids=[repr(c[:40]) for c in INVALIDOS])
def test_an_invalid_target_is_a_400_and_nothing_is_sent_to_the_daemon(daemon, pedir, corpo):
    status, _ = pedir("POST", f"/containers/create?name={CONTAINER_API}", [TIPO_JSON], corpo)

    assert status == 400
    assert daemon.requisicoes == []


def test_an_existing_helper_container_is_a_409(daemon, pedir):
    semear(daemon)

    status, _ = pedir("POST", f"/containers/create?name={CONTAINER_API}", [TIPO_JSON], CORPO_VALIDO)

    assert status == 409


@pytest.mark.parametrize(
    ("metodo", "rota"),
    [
        ("POST", "/start"),
        ("GET", "/json"),
        ("GET", "/logs?stdout=1&stderr=1&tail=5"),
        ("DELETE", ""),
    ],
)
def test_a_container_without_the_updater_label_is_forbidden_for_every_action(daemon, pedir, metodo, rota):
    semear(daemon, rotulos={"org.empiremuse.role": "outro"})

    status, _ = pedir(metodo, f"/containers/{CONTAINER_API}{rota}")

    assert status == 403
    assert [r for r in daemon.requisicoes if r.metodo != "GET" or not r.caminho.endswith("/json")] == []


# ============================================================================ A19 — imagem do auxiliar


def test_a_missing_helper_image_is_pulled_by_the_fixed_digest_before_the_create(daemon, pedir):
    daemon.imagem_presente = False

    status, _ = pedir("POST", f"/containers/create?name={CONTAINER_API}", [TIPO_JSON], CORPO_VALIDO)

    assert status == 201
    ordem = [(r.metodo, r.caminho) for r in daemon.requisicoes]
    assert ordem.index(("POST", "/images/create")) < ordem.index(("POST", "/containers/create"))
    imagem = criado_no_daemon(daemon)["Image"]
    (puxada,) = chegou(daemon, "POST", "/images/create")
    consulta = parse_qs(puxada.query)
    assert digest_da(imagem) in " ".join(v for valores in consulta.values() for v in valores)


def digest_da(imagem: str) -> str:
    return imagem.split("@", 1)[1]


# ============================================================================ A20 — inspect e logs


def test_inspect_returns_only_the_state_subset(daemon, pedir):
    semear(daemon, estado={"Status": "exited", "Running": False, "ExitCode": 70})

    status, corpo = pedir("GET", f"/containers/{CONTAINER_API}/json")

    assert status == 200
    resposta = json.loads(corpo)
    assert set(resposta) == {"State"}
    assert resposta["State"] == {
        "Status": "exited",
        "Running": False,
        "ExitCode": 70,
        "StartedAt": "2026-10-27T10:00:00Z",
        "FinishedAt": "2026-10-27T10:01:00Z",
    }


def test_logs_keep_only_prefixed_clean_lines_up_to_200_and_16_kib(daemon, pedir):
    ruido = ["docker: ruído do daemon", "pull: sem prefixo", PREFIXO_LOG.strip() + "sem espaço"]
    sujas = [PREFIXO_LOG + "cor \x1b[31mvermelha\x1b[0m", PREFIXO_LOG + "sino\x07", PREFIXO_LOG + "nulo\x00"]
    boas = [PREFIXO_LOG + f"etapa {n} 0" for n in range(300)]
    longas = [PREFIXO_LOG + "x" * 5000 for _ in range(10)]
    semear(daemon, linhas_de_log=ruido + sujas + boas + longas)

    status, corpo = pedir("GET", f"/containers/{CONTAINER_API}/logs?stdout=1&stderr=1&tail=200")

    assert status == 200
    linhas = json.loads(corpo)["lines"]
    assert 0 < len(linhas) <= 200
    assert all(linha.startswith(PREFIXO_LOG) for linha in linhas)
    assert all(ord(c) >= 32 and ord(c) != 127 for linha in linhas for c in linha)
    assert sum(len(linha.encode()) for linha in linhas) <= 16 * 1024
    assert not any("ruído" in linha for linha in linhas)


# ============================================================================ A21 — configuração, socket e interpretador


INVALIDAS = [
    ("UPDATER_PROJECT_DIR", None),
    ("UPDATER_PROJECT_DIR", "relativo/dir"),
    ("UPDATER_PROJECT_DIR", "/srv/../etc"),
    ("UPDATER_PROJECT_DIR", "/srv/com espaço"),
    ("UPDATER_PROJECT_DIR", "/srv/e2e;x"),
    ("UPDATER_PROJECT_NAME", "Maiúscula"),
    ("UPDATER_PROJECT_NAME", "a.b"),
    ("UPDATER_PROJECT_NAME", "-comeca"),
    ("UPDATER_SERVICE", "a.b"),
    ("UPDATER_SERVICE", "a#b"),
    ("UPDATER_SERVICE", "a&b"),
    ("UPDATER_SERVICE", "a\\b"),
    ("UPDATER_SERVICE", "a b"),
    ("UPDATER_IMAGE_REPO", "semBarra"),
    ("UPDATER_IMAGE_REPO", "empirecommunity/empiremuse#x"),
    ("UPDATER_IMAGE_REPO", "or4cu1o/empire&muse"),
    ("UPDATER_IMAGE_REPO", "or4cu1o/empire muse"),
    ("UPDATER_IMAGE_REPO", "or4cu1o/empire\\muse"),
    ("UPDATER_IMAGE_REPO", "OR4CU1O/empiremuse"),
    ("UPDATER_IMAGE_REPO", "or4cu1o//empiremuse"),
]


@pytest.mark.parametrize(("variavel", "valor"), INVALIDAS, ids=[f"{v}={x}" for v, x in INVALIDAS])
def test_an_invalid_configuration_exits_nonzero_without_creating_the_socket(tmp_path, variavel, valor):
    env = ambiente(tmp_path)
    if valor is None:
        del env[variavel]
    else:
        env[variavel] = valor

    processo = subir(env)
    processo.communicate(timeout=30)

    assert processo.returncode not in (0, None)
    assert not (tmp_path / "updater.sock").exists()


def test_the_listen_socket_is_created_with_mode_0660(mundo):
    modo = stat.S_IMODE(os.stat(mundo[1]).st_mode)

    assert modo == 0o660


def test_without_the_no_site_flag_the_proxy_refuses_to_start(tmp_path):
    processo = subir(ambiente(tmp_path), "-I")
    processo.communicate(timeout=30)

    assert processo.returncode not in (0, None)
    assert not (tmp_path / "updater.sock").exists()


def test_with_a_third_party_module_already_imported_the_proxy_refuses_to_start(tmp_path):
    site_packages = Path(pytest.__file__).resolve().parent.parent
    codigo = (
        "import sys, runpy\n"
        f"sys.path.append({str(site_packages)!r})\n"
        "import pytest\n"
        f"runpy.run_path({str(PROXY)!r}, run_name='__main__')\n"
    )
    processo = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", codigo],
        env=ambiente(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    processo.communicate(timeout=30)

    assert processo.returncode not in (0, None)
    assert not (tmp_path / "updater.sock").exists()


# ============================================================================ log: nada do bot vira sequência de controle (B1)


@pytest.mark.parametrize("metodo", ["\x1b[2J\x1b[31mFORJADO", "\x1b]0;x\x07FORJADO"], ids=["CSI", "OSC"])
def test_a_forged_request_line_never_puts_control_sequences_in_the_proxy_log(tmp_path, metodo):
    """Cabeçalhos demais viram 431 no parser da stdlib; a linha de log traz o método do bot e não pode repassar o ESC."""
    env = ambiente(tmp_path)
    processo = subir(env)
    esperar_socket(processo, env["UPDATER_LISTEN_SOCKET"])

    status, _ = requisicao_crua(env["UPDATER_LISTEN_SOCKET"], metodo, "/x", [(f"X-{n}", "v") for n in range(120)])
    processo.terminate()
    _, erro = processo.communicate(timeout=30)

    assert status == 431
    assert "\x1b" not in erro and "\x07" not in erro and "FORJADO" not in erro
    assert "? - parser 431" in erro
