"""Item 18, P3 — o ``proxy.py`` em processo: funções puras, cliente do daemon e servidor contra o daemon falso numa thread.

O ``test_proxy.py`` roda o proxy como processo (``python -I -S``) e prova o contrato de fora, mas um subprocesso não entra na cobertura.
Aqui as mesmas peças são exercitadas direto, sem repetir os casos de lá: bordas das funções puras, as falhas do daemon (respostas
forçadas no daemon falso), o handler/servidor real (``UnixHTTPServer``) e a subida (``serve``/``main``). Nada dorme: o servidor roda
em thread e os pedidos são síncronos.
"""

import json
import logging
import os
import signal
import socket
import struct
import threading
from types import SimpleNamespace

import pytest

from empiremuse.updater import proxy
from empiremuse.updater.proxy import (
    BadRequest,
    Config,
    ConfigError,
    DaemonError,
    DockerDaemon,
    RequestDenied,
    Route,
    UnixHTTPServer,
    check_headers,
    create_template,
    filter_log_lines,
    inspect_view,
    isolation_problems,
    load_config,
    parse_create_target,
    resolve_route,
)
from tests.support.daemon_falso import DaemonFalso, requisicao_crua

pytestmark = pytest.mark.critical

DIGEST = "sha256:" + "ab" * 32
ALVO = f"2.1.1@{DIGEST}"
NOME = "e2e-updater"
CORPO = json.dumps({"Cmd": [ALVO]}).encode()
ID = "a" * 64


def quadro(texto: str, fluxo: int = 1) -> bytes:
    carga = texto.encode()
    return struct.pack(">BxxxI", fluxo, len(carga)) + carga


# ============================================================================ funções puras


def test_resolve_route_maps_each_allowed_operation():
    assert resolve_route("POST", "/containers/create?name=empiremuse-updater") == Route("create")
    assert resolve_route("POST", "/v1.47/containers/empiremuse-updater/start") == Route("start")
    assert resolve_route("GET", "/containers/empiremuse-updater/json") == Route("inspect")
    assert resolve_route("GET", "/containers/empiremuse-updater/logs?stdout=1&stderr=1&tail=200") == Route("logs", 200)
    assert resolve_route("DELETE", "/containers/empiremuse-updater") == Route("remove")


@pytest.mark.parametrize(
    ("metodo", "alvo"),
    [
        ("GET", "/containers/empiremuse-updater/logs?stdout=1&stderr=0&tail=5"),
        ("GET", "/containers/empiremuse-updater/logs?stdout=1&stderr=1&tail=201"),
        ("GET", "/containers/empiremuse-updater/logs?stdout=1&stderr=1&tail=05"),
        ("GET", "/containers/empiremuse-updater/json?"),
        ("GET", "/containers/empiremuse-updater/json?a"),
        ("GET", "/containers/empiremuse-updater/json?a=b=c"),
        ("GET", "/containers/empiremuse-updater/json?a=b&a=c"),
        ("GET", "/containers/empiremuse-updater/json?a=%41"),
        ("GET", ""),
        ("GET", "containers/empiremuse-updater/json"),
        ("POST", "/containers/create?name=outro"),
    ],
)
def test_resolve_route_denies_malformed_targets(metodo, alvo):
    with pytest.raises(RequestDenied):
        resolve_route(metodo, alvo)


@pytest.mark.parametrize(
    ("itens", "tamanho"),
    [
        ([], 0),
        ([("Content-Type", "application/json")], 0),
        ([("content-length", "12")], 12),
        ([("Content-Length", "65536")], 65536),
    ],
)
def test_check_headers_returns_the_validated_content_length(itens, tamanho):
    assert check_headers(itens) == tamanho


@pytest.mark.parametrize(
    "itens",
    [
        [("Content-Length", "abc")],
        [("Content-Length", "-1")],
        [("Content-Length", "1234567")],
        [("Content-Length", "65537")],
        [("Connection", "keep-alive, Upgrade")],
        [("Expect", "100-continue")],
    ],
)
def test_check_headers_denies_dangerous_or_malformed_headers(itens):
    with pytest.raises(RequestDenied):
        check_headers(itens)


@pytest.mark.parametrize(
    "corpo", [b"\xff\xfe", b"{", b"[" * 100000, b'{"Cmd": null}', b'{"Cmd": ["' + b"x" * 10 + b'"]}']
)
def test_parse_create_target_raises_bad_request_for_hostile_bodies(corpo):
    with pytest.raises(BadRequest):
        parse_create_target(corpo)


def test_the_template_carries_the_configuration_and_nothing_else():
    config = Config("/d.sock", "/l.sock", "e2e", "/srv/e2e", "bot", "empirecommunity/empiremuse")

    modelo = create_template(config, ALVO)

    assert modelo["Cmd"] == [ALVO, "e2e", "/srv/e2e", "bot", "empirecommunity/empiremuse"]
    assert modelo["HostConfig"]["Binds"][1] == "/srv/e2e:/srv/e2e" and modelo["HostConfig"]["Privileged"] is False
    assert config.container_name == NOME


def test_filter_log_lines_keeps_the_last_prefixed_lines_within_tail_and_size():
    prefixo = proxy.LOG_PREFIX
    bruto = quadro(f"{prefixo}a\nruido\n{prefixo}b\n") + quadro(f"{prefixo}c\n{prefixo}tab\tsim\n", 2)

    assert filter_log_lines(bruto, 2) == [f"{prefixo}b", f"{prefixo}c"]
    assert filter_log_lines(bruto, 0) == []
    longas = quadro("".join(f"{prefixo}{'x' * 1000}\n" for _ in range(40)))
    assert sum(len(linha) + 1 for linha in filter_log_lines(longas, 200)) <= proxy.MAX_LOG_BYTES


@pytest.mark.parametrize(
    "bruto",
    [b"", b"curto", struct.pack(">BxxxI", 9, 1) + b"x", b"\x01\x01\x00\x00" + b"\x00\x00\x00\x01x"],
    ids=["vazio", "menor-que-o-cabecalho", "fluxo-invalido", "preenchimento-invalido"],
)
def test_filter_log_lines_stops_at_a_malformed_frame(bruto):
    assert filter_log_lines(bruto, 10) == []


def test_filter_log_lines_survives_truncated_payloads_and_bad_utf8():
    cortado = struct.pack(">BxxxI", 1, 500) + (proxy.LOG_PREFIX + "ok\n").encode() + b"\xff"

    assert filter_log_lines(cortado, 5) == [proxy.LOG_PREFIX + "ok"]


def test_inspect_view_sanitizes_every_field():
    visao = inspect_view(
        {
            "State": {
                "Status": "exited",
                "Running": 1,
                "ExitCode": True,
                "StartedAt": "ruim\n",
                "FinishedAt": "2026-10-27T10:01:00Z",
                "Pid": 1,
            }
        }
    )

    assert visao == {
        "State": {
            "Status": "exited",
            "Running": False,
            "ExitCode": -1,
            "StartedAt": "",
            "FinishedAt": "2026-10-27T10:01:00Z",
        }
    }


@pytest.mark.parametrize("dados", [{}, {"State": None}, {"State": "texto"}])
def test_inspect_view_without_a_state_object_is_a_daemon_error(dados):
    with pytest.raises(DaemonError):
        inspect_view(dados)


def modulo(arquivo=None, origem=None):
    return SimpleNamespace(__file__=arquivo, __spec__=SimpleNamespace(origin=origem) if origem else None)


def test_isolation_problems_decides_by_origin_not_by_name(tmp_path):
    ok = SimpleNamespace(isolated=1, no_site=1)
    stdlib = os.path.dirname(os.__file__)
    modulos = {
        "os": os,
        "json.decoder": json.decoder,
        "sys": modulo(),
        "_frozen_qualquer": modulo(origem="frozen"),
        "__main__": modulo(str(tmp_path / "main.py")),
        "preso": None,
        "requests": modulo(str(tmp_path / "requests" / "__init__.py")),
        "json": modulo(str(tmp_path / "json.py")),
        "yaml.loader": modulo(os.path.join(stdlib, "site-packages", "yaml", "loader.py")),
        "sem_arquivo": modulo(),
    }

    (problema,) = isolation_problems(ok, modulos)

    assert set(problema.split(": ")[1].split(", ")) == {"requests", "json", "yaml", "sem_arquivo"}


def test_load_config_applies_defaults_and_reads_the_peer_uid():
    config = load_config({"UPDATER_PROJECT_DIR": "/srv/app", "UPDATER_PEER_UID": "10001"})

    assert (config.docker_socket, config.project_name, config.service, config.image_repo) == (
        "/var/run/docker.sock",
        "empiremuse",
        "bot",
        "empirecommunity/empiremuse",
    )
    assert config.listen_socket == "/run/empiremuse-updater/updater.sock" and config.peer_uid == 10001


@pytest.mark.parametrize(
    ("variavel", "valor"),
    [
        ("UPDATER_PEER_UID", "root"),
        ("UPDATER_DOCKER_SOCKET", "relativo.sock"),
        ("UPDATER_LISTEN_SOCKET", "/" + "a" * 120),
    ],
)
def test_load_config_names_only_the_invalid_variables(variavel, valor):
    with pytest.raises(ConfigError) as erro:
        load_config({"UPDATER_PROJECT_DIR": "/srv/app", variavel: valor})

    assert variavel in str(erro.value) and valor not in str(erro.value)


@pytest.mark.parametrize(
    ("variavel", "valor"),
    [
        ("UPDATER_PROJECT_DIR", "//"),
        ("UPDATER_PROJECT_DIR", "//docker"),
        ("UPDATER_LISTEN_SOCKET", "//x"),
        ("UPDATER_DOCKER_SOCKET", "//x"),
    ],
)
def test_a_path_with_a_double_leading_slash_is_refused(variavel, valor):
    """``//x`` é equivalente a ``/x`` no Linux, mas no POSIX o início ``//`` é especial: o caminho tem de vir normalizado (I1)."""
    with pytest.raises(ConfigError) as erro:
        load_config({"UPDATER_PROJECT_DIR": "/srv/app", variavel: valor})

    assert variavel in str(erro.value)


# ============================================================================ cliente do daemon e Updater


@pytest.fixture
def daemon(tmp_path):
    falso = DaemonFalso(str(tmp_path / "docker.sock"))
    yield falso
    falso.parar()


def cliente(daemon) -> DockerDaemon:
    return DockerDaemon(daemon.caminho_do_socket)


def test_the_daemon_client_builds_the_request_from_scratch(daemon):
    daemon.adicionar_container(NOME)

    status, dados = cliente(daemon).call("GET", f"/containers/{NOME}/logs", {"stdout": "1", "tail": "5"})

    assert status == 200
    (requisicao,) = daemon.requisicoes
    assert requisicao.bruto.startswith(f"/{proxy.DOCKER_API_VERSION}/containers/{NOME}/logs?")
    assert requisicao.cabecalhos["connection"] == "close" and requisicao.cabecalhos["host"] == "docker"
    assert "content-type" not in requisicao.cabecalhos


def test_the_daemon_client_sends_the_json_headers_only_with_a_body(daemon):
    cliente(daemon).call("POST", "/containers/create", {"name": NOME}, b"{}")

    (requisicao,) = daemon.requisicoes
    assert (
        requisicao.cabecalhos["content-type"] == "application/json" and requisicao.cabecalhos["content-length"] == "2"
    )


def test_an_unreachable_daemon_is_a_daemon_error(tmp_path):
    with pytest.raises(DaemonError, match="inacessível"):
        DockerDaemon(str(tmp_path / "nao-existe.sock")).call("GET", "/x")


def test_an_oversized_daemon_response_is_a_daemon_error(daemon):
    daemon.forcadas[("GET", "/x")] = (200, b"x" * 50)

    with pytest.raises(DaemonError, match="grande demais"):
        cliente(daemon).call("GET", "/x", limit=10)


def servidor_com(daemon, tmp_path, *, peer_uid=None):
    config = Config(
        daemon.caminho_do_socket,
        str(tmp_path / "updater.sock"),
        "e2e",
        "/srv/e2e",
        "bot",
        "empirecommunity/empiremuse",
        peer_uid,
    )
    servidor = UnixHTTPServer(config, DockerDaemon(daemon.caminho_do_socket))
    fio = threading.Thread(target=servidor.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    fio.start()
    return servidor, fio, config.listen_socket


@pytest.fixture
def proxy_em_processo(daemon, tmp_path):
    servidor, fio, caminho = servidor_com(daemon, tmp_path)
    yield lambda *args, **kwargs: requisicao_crua(caminho, *args, **kwargs), servidor
    servidor.shutdown()
    fio.join()
    servidor.server_close()


@pytest.mark.parametrize(
    ("metodo", "caminho", "status_forcado", "esperado"),
    [
        ("POST", f"/containers/{NOME}/start", 304, 409),
        ("POST", f"/containers/{NOME}/start", 500, 502),
        ("GET", f"/containers/{NOME}/logs", 500, 502),
        ("DELETE", f"/containers/{NOME}", 409, 409),
        ("DELETE", f"/containers/{NOME}", 500, 502),
    ],
)
def test_daemon_statuses_are_translated_for_the_bot(
    daemon, proxy_em_processo, metodo, caminho, status_forcado, esperado
):
    pedir, _ = proxy_em_processo
    daemon.adicionar_container(NOME)
    daemon.forcadas[(metodo, caminho)] = (status_forcado, b"{}")
    alvo = "/containers/empiremuse-updater" + caminho.removeprefix(f"/containers/{NOME}")
    if caminho.endswith("/logs"):
        alvo += "?stdout=1&stderr=1&tail=5"

    status, corpo = pedir(metodo, alvo)

    assert status == esperado
    if esperado == 502:
        assert json.loads(corpo) == {"message": "falha ao falar com o Docker"}


def test_a_helper_that_does_not_exist_is_a_404_for_every_action(proxy_em_processo):
    pedir, _ = proxy_em_processo

    assert pedir("GET", "/containers/empiremuse-updater/json")[0] == 404
    assert pedir("POST", "/containers/empiremuse-updater/start")[0] == 404


@pytest.mark.parametrize(
    ("forcada", "esperado"),
    [
        (("GET", f"/containers/{NOME}/json"), (500, b"{}")),
        (("GET", f"/containers/{NOME}/json"), (200, b"nao e json")),
        (("GET", f"/containers/{NOME}/json"), (200, b"[1]")),
        (("GET", f"/containers/{NOME}/json"), (200, b'{"Config": {"Labels": []}}')),
    ],
)
def test_a_bad_inspect_answer_never_reaches_the_action(daemon, proxy_em_processo, forcada, esperado):
    pedir, _ = proxy_em_processo
    daemon.adicionar_container(NOME)
    daemon.forcadas[forcada] = esperado

    status, _ = pedir("POST", "/containers/empiremuse-updater/start")

    assert status in (403, 502) and chegou_start(daemon) == []


def chegou_start(daemon):
    return [r for r in daemon.requisicoes if r.metodo == "POST" and r.caminho.endswith("/start")]


@pytest.mark.parametrize(
    ("forcadas", "esperado"),
    [
        ({("GET", f"/images/{proxy.HELPER_IMAGE}/json"): (500, b"{}")}, 502),
        ({("POST", "/images/create"): (500, b"{}")}, 502),
        ({("POST", "/images/create"): (200, b'{"error": "denied"}\n')}, 502),
        ({("POST", "/images/create"): (200, b'{"errorDetail": {"message": "x"}}\n')}, 502),
        ({("POST", "/containers/create"): (500, b"{}")}, 502),
        ({("POST", "/containers/create"): (201, b'{"Id": "curto"}')}, 502),
        ({("POST", "/containers/create"): (201, b"nada")}, 502),
        ({("POST", "/containers/create"): (409, b"{}")}, 409),
    ],
    ids=[
        "imagem-500",
        "pull-500",
        "pull-error",
        "pull-errorDetail",
        "create-500",
        "id-invalido",
        "create-sem-json",
        "create-409",
    ],
)
def test_create_failures_are_translated(daemon, proxy_em_processo, forcadas, esperado):
    pedir, _ = proxy_em_processo
    daemon.forcadas.update(forcadas)
    if ("POST", "/images/create") in forcadas:
        daemon.imagem_presente = False

    status, _ = pedir(
        "POST", "/containers/create?name=empiremuse-updater", [("Content-Type", "application/json")], CORPO
    )

    assert status == esperado


def test_a_pull_stream_with_noise_but_no_error_is_a_success(daemon, proxy_em_processo):
    pedir, _ = proxy_em_processo
    daemon.forcadas[("POST", "/images/create")] = (200, b'{"status": "Pulling"}\nlixo\n[1]\n{"status": "done"}\n')

    status, corpo = pedir("POST", "/containers/create?name=empiremuse-updater", [], CORPO)

    assert status == 201 and json.loads(corpo) == {"Id": ID}


def test_a_create_answer_returns_only_the_container_id(proxy_em_processo):
    pedir, _ = proxy_em_processo

    status, corpo = pedir("POST", "/containers/create?name=empiremuse-updater", [], CORPO)

    assert status == 201 and set(json.loads(corpo)) == {"Id"}


# ============================================================================ handler e servidor


def test_a_daemon_that_is_down_is_a_502_with_a_generic_message(tmp_path):
    fora = SimpleNamespace(caminho_do_socket=str(tmp_path / "fora.sock"))
    servidor, fio, caminho = servidor_com(fora, tmp_path)
    try:
        status, corpo = requisicao_crua(caminho, "GET", "/containers/empiremuse-updater/json")
    finally:
        servidor.shutdown()
        fio.join()
        servidor.server_close()

    assert status == 502 and json.loads(corpo) == {"message": "falha ao falar com o Docker"}


def test_an_incomplete_body_is_a_400(proxy_em_processo, tmp_path):
    _, servidor = proxy_em_processo
    caminho = servidor.server_address
    with socket.socket(socket.AF_UNIX) as cliente_cru:
        cliente_cru.connect(caminho)
        cliente_cru.sendall(b"POST /containers/create?name=empiremuse-updater HTTP/1.1\r\nContent-Length: 50\r\n\r\n{}")
        cliente_cru.shutdown(socket.SHUT_WR)
        resposta = cliente_cru.recv(65536)

    assert resposta.startswith(b"HTTP/1.0 400")


@pytest.mark.parametrize(
    ("linha", "status"),
    [
        (b"GET /containers/empiremuse-updater/json HTTP/2.0\r\n\r\n", b"HTTP/1.0 505"),
        (b"GET / HTTP/1.1 extra\r\n\r\n", b"HTTP/1.0 400"),
    ],
    ids=["versao-http-2", "linha-com-quatro-campos"],
)
def test_unsupported_request_lines_are_denied(proxy_em_processo, linha, status):
    _, servidor = proxy_em_processo
    with socket.socket(socket.AF_UNIX) as cliente_cru:
        cliente_cru.connect(servidor.server_address)
        cliente_cru.sendall(linha)
        resposta = cliente_cru.recv(65536)

    assert resposta.startswith(status)


def test_the_log_has_one_line_per_decision_and_never_the_body_or_headers(proxy_em_processo, daemon, caplog):
    pedir, _ = proxy_em_processo
    with caplog.at_level(logging.INFO, logger="empiremuse-updater"):
        pedir("POST", "/containers/create?name=empiremuse-updater", [("Authorization", "Bearer segredo")], CORPO)
        pedir("GET", "/_ping")
        pedir("GET", "/containers/%2e%2e/segredo-no-caminho")

    texto = caplog.text
    assert "permitido" in texto and "negado" in texto
    assert "segredo" not in texto and DIGEST not in texto and "Bearer" not in texto


def test_only_the_configured_peer_uid_is_served(daemon, tmp_path):
    servidor, fio, caminho = servidor_com(daemon, tmp_path, peer_uid=os.getuid() + 1)
    try:
        status, _ = requisicao_crua(caminho, "GET", "/containers/empiremuse-updater/json")
    finally:
        servidor.shutdown()
        fio.join()
        servidor.server_close()

    assert status == 0 and daemon.requisicoes == []


def test_when_all_connection_slots_are_taken_a_new_connection_is_dropped(proxy_em_processo, daemon):
    pedir, servidor = proxy_em_processo
    daemon.adicionar_container(NOME)
    ocupados = [servidor._slots.acquire() for _ in range(proxy.MAX_CONNECTIONS)]
    try:
        assert pedir("GET", "/containers/empiremuse-updater/json")[0] == 0
    finally:
        for _ in ocupados:
            servidor._slots.release()

    assert pedir("GET", "/containers/empiremuse-updater/json")[0] == 200


# ============================================================================ subida


def test_a_stale_socket_is_removed_but_a_regular_file_is_refused(tmp_path):
    antigo = tmp_path / "velho.sock"
    with socket.socket(socket.AF_UNIX) as s:
        s.bind(str(antigo))
    arquivo = tmp_path / "arquivo"
    arquivo.write_text("x", encoding="utf-8")

    proxy._remove_stale_socket(str(antigo))
    proxy._remove_stale_socket(str(tmp_path / "ausente"))

    assert not antigo.exists()
    with pytest.raises(ConfigError):
        proxy._remove_stale_socket(str(arquivo))
    assert arquivo.exists()


def config_em(tmp_path, **extras) -> Config:
    base = {
        "docker_socket": str(tmp_path / "docker.sock"),
        "listen_socket": str(tmp_path / "updater.sock"),
        "project_name": "e2e",
        "project_dir": "/srv/e2e",
        "service": "bot",
        "image_repo": "empirecommunity/empiremuse",
    }
    return Config(**{**base, **extras})


def test_serve_fails_with_exit_1_when_the_socket_cannot_be_opened(tmp_path, capsys):
    config = config_em(tmp_path, listen_socket=str(tmp_path / "sem" / "pasta" / "updater.sock"))

    assert proxy.serve(config) == proxy.EXIT_SERVER_FAILED
    assert "não foi possível abrir o socket" in capsys.readouterr().err


def test_serve_runs_until_stopped_then_removes_the_socket_and_wires_the_signals(tmp_path, monkeypatch):
    config = config_em(tmp_path)
    instalados = {}
    monkeypatch.setattr(proxy.signal, "signal", lambda numero, tratador: instalados.setdefault(numero, tratador))

    def serve_forever(self):
        assert os.path.exists(config.listen_socket)
        instalados[signal.SIGTERM](signal.SIGTERM, None)  # o tratador só dispara o shutdown em outra thread

    monkeypatch.setattr(UnixHTTPServer, "serve_forever", serve_forever)

    assert proxy.serve(config) == 0
    assert not os.path.exists(config.listen_socket) and set(instalados) == {signal.SIGTERM, signal.SIGINT}


def test_main_refuses_to_start_outside_the_isolated_interpreter(capsys):
    assert proxy.main({}) == proxy.EXIT_UNSAFE_RUNTIME
    assert "ambiente inseguro" in capsys.readouterr().err


def test_main_reports_an_invalid_configuration_by_variable_name(monkeypatch, capsys):
    monkeypatch.setattr(proxy, "isolation_problems", lambda flags, modulos: [])

    assert proxy.main({"UPDATER_SERVICE": "a.b", "UPDATER_PROJECT_DIR": "/srv/e2e"}) == proxy.EXIT_INVALID_CONFIG
    erro = capsys.readouterr().err
    assert "UPDATER_SERVICE" in erro and "a.b" not in erro


def test_main_serves_with_the_validated_configuration(monkeypatch):
    recebidas = []
    monkeypatch.setattr(proxy, "isolation_problems", lambda flags, modulos: [])
    monkeypatch.setattr(proxy, "serve", lambda config: recebidas.append(config) or 0)

    assert proxy.main({"UPDATER_PROJECT_DIR": "/srv/e2e", "UPDATER_PROJECT_NAME": "e2e"}) == 0
    assert recebidas[0].container_name == NOME
