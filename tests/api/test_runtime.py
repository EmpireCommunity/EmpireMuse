"""Modos do painel (contrato T-456 §6 e §14 [M4, n3]): config inválida = nenhum painel e nenhuma API, e o bot sobe e toca."""

import json
import socket
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from tornado.httpclient import AsyncHTTPClient
from tornado.testing import AsyncTestCase

from empiremuse import web_app
from empiremuse.api.idempotency import Reply
from empiremuse.api.runtime import start_external_api
from empiremuse.panel.state import ensure_panel_state
from tests.api.helpers import NOW, SERVICE_KEY, external_config
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


def test_destruir_o_usuario_no_store_unico_limpa_as_respostas_guardadas_da_api(pool):
    status, _ = start(pool)
    try:
        idempotency = status.server.request_callback.settings["default_handler_args"]["ctx"].idempotency
        idempotency.settle(idempotency.claim("1001", "chave-0001", "f").ticket, Reply(200, {"ok": True}))

        pool.panel_sessions.destroy_user("1001")  # o mesmo gancho do apagamento de dados

        assert idempotency.claim("1001", "chave-0001", "f").replay is None
    finally:
        status.server.stop()


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


def test_listener_so_escuta_no_endereco_configurado(pool):
    status, _ = start(pool)
    try:
        porta = [s.getsockname() for s in status.server._sockets.values()]
        assert porta and all(nome[0] == "127.0.0.1" for nome in porta)  # nunca 0.0.0.0 nem ::
        numero = porta[0][1]
        with socket.create_connection(("127.0.0.1", numero), timeout=2):
            pass
        with pytest.raises(OSError):  # outro endereço da mesma máquina não alcança a API
            socket.create_connection(("127.0.0.2", numero), timeout=2)
    finally:
        status.server.stop()


def test_aviso_alto_quando_o_modo_externo_esta_pedido_mas_o_rele_esta_desligado(caplog):
    from empiremuse.api.runtime import warn_if_relay_is_off

    with caplog.at_level("ERROR"):
        warn_if_relay_is_off(external_config())
        warn_if_relay_is_off({})
        warn_if_relay_is_off({"EXTERNAL_PANEL_URL": "   "})

    assert [r.levelname for r in caplog.records] == ["ERROR"]
    assert "RUN_RPC_SERVER" in caplog.text


def test_modo_bloqueado_mantem_a_confianca_no_proxy_vinda_da_configuracao(monkeypatch):
    capturado = {}

    class ServidorFalso:
        def __init__(self, roteador, **kwargs):
            capturado["roteador"], capturado["kwargs"] = roteador, kwargs

        def listen(self, porta, address=None, **kwargs):
            pass

    monkeypatch.setattr(web_app.tornado.httpserver, "HTTPServer", ServidorFalso)
    config = {
        "PORT": "6000",
        "EXTERNAL_PANEL_URL": "https://painel.example.com",  # incompleto: modo bloqueado
        "PANEL_TRUST_PROXY": True,
        "PANEL_TRUSTED_PROXY_CIDRS": "192.0.2.0/24",
    }

    web_app.run_app(MagicMock(), config=config)

    assert capturado["kwargs"]["xheaders"] is True  # o /ws e a página de status seguem vendo o IP do visitante
    assert [str(rede) for rede in capturado["roteador"].trusted_networks] == ["192.0.2.0/24"]
    assert capturado["roteador"].trust_proxy is True


def test_modo_bloqueado_registra_os_avisos_da_lista_de_proxies(monkeypatch, caplog):
    monkeypatch.setattr(web_app.tornado.httpserver, "HTTPServer", lambda *a, **k: MagicMock())
    config = {
        "PORT": "6000",
        "EXTERNAL_PANEL_URL": "https://painel.example.com",
        "PANEL_TRUST_PROXY": True,
        "PANEL_TRUSTED_PROXY_CIDRS": "192.0.2.0/24, valor-invalido-xyz",
    }

    with caplog.at_level("WARNING", logger="empiremuse.web_app"):
        web_app.run_app(MagicMock(), config=config)

    assert [r for r in caplog.records if r.name == "empiremuse.web_app" and r.levelname == "WARNING"]


class ListenerPublicoTest(AsyncTestCase):
    def test_o_listener_publico_nunca_responde_api(self):
        pool = SimpleNamespace(bots=[], database=None)
        publica, interna = free_port(), free_port()
        config = {
            **local_config(PANEL_INSECURE_COOKIES=False),  # o genérico estaria ligado: a API válida deve substituí-lo
            "PORT": str(publica),
            "WEB_BIND_ADDRESS": "127.0.0.1",
            **external_config(EXTERNAL_PANEL_API_PORT=str(interna)),
        }
        web_app.run_app(pool, config=config)
        cabecalhos = {"Authorization": f"Bearer {SERVICE_KEY}"}

        async def pedir(porta, caminho, method="GET"):
            return await AsyncHTTPClient().fetch(
                f"http://127.0.0.1:{porta}{caminho}",
                method=method,
                body="" if method == "POST" else None,
                headers=cabecalhos,
                raise_error=False,
                follow_redirects=False,
            )

        async def tudo():
            publicas = [await pedir(publica, c) for c in ("/api/v1/health", "/api/v1/meta", "/api/v1/sessions")]
            redirects = [
                await pedir(publica, "/painel/callback?code=segredo&state=outro", method)
                for method in ("GET", "HEAD", "POST")
            ]
            return publicas, await pedir(interna, "/api/v1/health"), redirects

        try:
            publicas, interna_ok, redirects = self.io_loop.run_sync(tudo)
        finally:
            pool.panel_api_server.stop()

        assert interna_ok.code == 200 and json.loads(interna_ok.body) == {"status": "ok"}
        for resposta in publicas:
            assert resposta.code == 404 and b"api_versions" not in resposta.body and b'"status"' not in resposta.body

        get, head, post = redirects
        for response in (get, head):
            assert response.code == 302 and response.headers["Location"] == config["EXTERNAL_PANEL_URL"]
            assert "Set-Cookie" not in response.headers and b"segredo" not in response.body
        assert post.code == 404 and "Location" not in post.headers


def test_b6_modo_externo_valido_substitui_o_generico_por_redirect_mesmo_com_panel_enabled(monkeypatch):
    from empiremuse.api.runtime import ExternalApiStatus

    captured = {}

    class Server:
        def __init__(self, router, **kwargs):
            captured["router"] = router

        def listen(self, port, **kwargs):
            pass

    monkeypatch.setattr(
        "empiremuse.api.runtime.start_external_api",
        lambda *args: ExternalApiStatus(server=object(), panel_url="https://muse.empirecommunity.org/panel"),
    )
    monkeypatch.setattr(web_app, "_build_panel", lambda *args: pytest.fail("o genérico não pode subir junto da API"))
    monkeypatch.setattr(web_app.tornado.httpserver, "HTTPServer", Server)
    config = {
        "PORT": "6000",
        "PANEL_ENABLED": True,
        "PANEL_TRUST_PROXY": True,
        "PANEL_TRUSTED_PROXY_CIDRS": "192.0.2.0/24",
    }
    assert web_app.run_app(SimpleNamespace(database=None), config=config) is None
    assert captured["router"].trust_proxy is True
    assert captured["router"].trusted_networks


def test_b6_config_generica_invalida_nao_desliga_a_confianca_no_ip_configurada(monkeypatch):
    captured = {}

    class Server:
        def __init__(self, router, **kwargs):
            captured["router"], captured["kwargs"] = router, kwargs

        def listen(self, port, **kwargs):
            pass

    monkeypatch.setattr(web_app.tornado.httpserver, "HTTPServer", Server)
    config = {
        "PORT": "6000",
        "PANEL_ENABLED": True,
        "PANEL_BASE_URL": "invalid",
        "PANEL_TRUST_PROXY": True,
        "PANEL_TRUSTED_PROXY_CIDRS": "192.0.2.0/24",
    }
    assert web_app.run_app(SimpleNamespace(database=None), config=config) is None
    assert captured["router"].trust_proxy is True
    assert captured["kwargs"]["xheaders"] is True
