"""Modos do painel (contrato T-456 §6 e §14 [M4, n3]): config inválida = nenhum painel e nenhuma API, e o bot sobe e toca."""

import socket
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from empiremuse import web_app
from empiremuse.api.runtime import start_external_api
from empiremuse.panel.state import ensure_panel_state
from tests.api.helpers import NOW, external_config
from tests.panel.helpers import load_settings, local_config

pytestmark = pytest.mark.critical


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def pool():
    return SimpleNamespace(bots=[], database=None)


def start(pool, **overrides):
    config = {"PORT": "6000", **external_config(EXTERNAL_PANEL_API_PORT=str(free_port())), **overrides}
    return start_external_api(pool, config, wall_clock=lambda: NOW), config


def test_modo_externo_desligado_nao_cria_estado_nem_listener(pool):
    status = start_external_api(pool, {"PORT": "6000"}, wall_clock=lambda: NOW)

    assert (status.blocked, status.server) == (False, None)
    assert not hasattr(pool, "panel_sessions") and not hasattr(pool, "panel_api_server")


def test_modo_externo_valido_sobe_o_listener_e_cria_o_estado_unico_no_pool(pool):
    status, _ = start(pool)
    try:
        assert status.blocked is False and status.server is pool.panel_api_server
        assert pool.panel_sessions is not None and pool.panel_tasks is not None
        assert ensure_panel_state(pool, load_settings(local_config())).session_store is pool.panel_sessions
    finally:
        status.server.stop()


@pytest.mark.parametrize(
    "overrides",
    [
        {"EXTERNAL_PANEL_URL": ""},  # segredo sem URL
        {"EXTERNAL_PANEL_API_SECRET": ""},  # URL sem segredo
        {"EXTERNAL_PANEL_URL": "https://u:p@painel.example.com"},
        {"EXTERNAL_PANEL_API_PORT": "6000"},  # igual a PORT
        {"EXTERNAL_PANEL_API_BIND": ""},
        {"PANEL_SESSION_TTL_HOURS": 0},
        {"PORT": "lixo"},
    ],
)
def test_n3_configuracao_invalida_bloqueia_tudo_sem_abrir_porta_nem_criar_estado(pool, overrides):
    status, _ = start(pool, **overrides)

    assert (status.blocked, status.server) == (True, None)
    assert getattr(pool, "panel_api_server", None) is None
    assert getattr(pool, "panel_sessions", None) is None


def test_porta_ocupada_bloqueia_e_o_bot_segue(pool):
    with socket.socket() as ocupado:
        ocupado.bind(("127.0.0.1", 0))
        ocupado.listen()
        porta = ocupado.getsockname()[1]

        status, _ = start(pool, EXTERNAL_PANEL_API_PORT=str(porta))

    assert (status.blocked, status.server) == (True, None)


def test_n3_bot_sobe_e_toca_run_app_com_configuracao_externa_invalida_nao_sobe_o_painel_generico(monkeypatch, pool):
    escutando = []

    class ServidorFalso:
        def __init__(self, roteador, **kwargs):
            self.roteador = roteador

        def listen(self, porta, address=None, **kwargs):
            escutando.append(porta)

    monkeypatch.setattr(web_app.tornado.httpserver, "HTTPServer", ServidorFalso)
    monkeypatch.setattr(web_app, "_build_panel", lambda *a: pytest.fail("o painel genérico não pode subir"))
    config = {
        **local_config(PANEL_BASE_URL="http://localhost:8080"),
        "PORT": "6000",
        "EXTERNAL_PANEL_URL": "https://painel.example.com",  # sem os outros segredos
    }

    panel_app = web_app.run_app(MagicMock(), config=config)

    assert panel_app is None
    assert escutando == [6000]  # o servidor do bot (status e /ws) subiu


def test_painel_generico_segue_como_antes_sem_nenhuma_variavel_externa(monkeypatch):
    chamado = []
    monkeypatch.setattr(web_app, "_build_panel", lambda pool, config: chamado.append(1) or (None, False, ()))
    monkeypatch.setattr(web_app.tornado.httpserver, "HTTPServer", lambda *a, **k: MagicMock())

    web_app.run_app(MagicMock(), config={"PORT": "6000"})

    assert chamado == [1]
