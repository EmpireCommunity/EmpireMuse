"""Fiação de ``PANEL_TRUSTED_PROXY_CIDRS``: ``run_app`` entrega a mesma lista ao painel e ao ``ClientIpRouter``."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from empiremuse import web_app
from empiremuse.rules.client_ip import parse_trusted_proxy_cidrs

pytestmark = pytest.mark.critical

REDES = parse_trusted_proxy_cidrs("192.0.2.0/24")[0]


def test_build_panel_devolve_a_lista_configurada(monkeypatch) -> None:
    import empiremuse.panel as panel
    from tests.panel.helpers import local_config

    capturado = []
    monkeypatch.setattr(panel, "build_panel_app", lambda pool, settings: capturado.append(settings) or object())
    config = local_config(PANEL_TRUST_PROXY=True, PANEL_TRUSTED_PROXY_CIDRS="192.0.2.0/24")

    app = web_app._build_panel(MagicMock(), config, REDES)

    assert app is not None and capturado[0].trusted_proxy_networks is REDES


def test_build_panel_com_configuracao_invalida_nao_confia_em_ninguem(monkeypatch) -> None:
    app = web_app._build_panel(MagicMock(), {"PANEL_ENABLED": True, "PANEL_BASE_URL": "lixo"}, REDES)

    assert app is None


@pytest.mark.parametrize(
    ("painel_ativo", "confia", "esperado"), [(True, True, True), (True, False, False), (False, True, True)]
)
def test_run_app_entrega_a_lista_e_o_xheaders_ao_servidor(monkeypatch, painel_ativo, confia, esperado) -> None:
    capturado: dict = {}

    class ServidorFalso:
        def __init__(self, roteador, **kwargs):
            capturado["roteador"], capturado["kwargs"] = roteador, kwargs

        def listen(self, porta, address=None, **kwargs):
            capturado["porta"] = porta

    monkeypatch.setattr(web_app.tornado.httpserver, "HTTPServer", ServidorFalso)
    monkeypatch.setattr(web_app, "_build_panel", lambda pool, config, networks: object() if painel_ativo else None)
    monkeypatch.setattr(web_app, "make_router", lambda app, panel: app)
    monkeypatch.setenv("PORT", "18080")

    config = {"PORT": "18080", "PANEL_TRUST_PROXY": confia, "PANEL_TRUSTED_PROXY_CIDRS": "192.0.2.0/24"}
    web_app.run_app(SimpleNamespace(config=config), config=config)

    roteador = capturado["roteador"]
    assert isinstance(roteador, web_app.ClientIpRouter)
    assert roteador.trusted_networks == REDES and roteador.trust_proxy is esperado
    assert capturado["kwargs"]["xheaders"] is esperado


def test_cidrs_invalidos_avisam_uma_vez_com_generico_ativo(monkeypatch, caplog):
    from tests.panel.helpers import local_config

    monkeypatch.setattr("empiremuse.panel.build_panel_app", lambda *args: object())
    monkeypatch.setattr(web_app, "make_router", lambda app, panel: app)
    monkeypatch.setattr(web_app.tornado.httpserver, "HTTPServer", lambda *args, **kwargs: MagicMock())
    config = local_config(PORT="6000", PANEL_TRUSTED_PROXY_CIDRS="192.0.2.0/24,invalid")
    web_app.run_app(SimpleNamespace(database=None), config=config)
    warnings = [record for record in caplog.records if "CIDR" in record.message]
    assert len(warnings) == 1
