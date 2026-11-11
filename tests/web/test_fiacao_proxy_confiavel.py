"""Fiação de ``PANEL_TRUSTED_PROXY_CIDRS``: ``_build_panel`` guarda a lista e ``run_app`` a entrega ao ``ClientIpRouter``."""

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

    monkeypatch.setattr(panel, "build_panel_app", lambda pool, settings: object())
    config = local_config(PANEL_TRUST_PROXY=True, PANEL_TRUSTED_PROXY_CIDRS="192.0.2.0/24")

    app, trust_proxy, redes = web_app._build_panel(MagicMock(), config)

    assert app is not None and trust_proxy is True and redes == REDES


def test_build_panel_com_configuracao_invalida_nao_confia_em_ninguem(monkeypatch) -> None:
    app, trust_proxy, redes = web_app._build_panel(MagicMock(), {"PANEL_ENABLED": True, "PANEL_BASE_URL": "lixo"})

    assert (app, trust_proxy, redes) == (None, False, ())


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
    monkeypatch.setattr(
        web_app, "_build_panel", lambda pool, config: (object() if painel_ativo else None, confia, REDES)
    )
    monkeypatch.setattr(web_app, "make_router", lambda app, panel: app)
    monkeypatch.setenv("PORT", "18080")

    config = {"PORT": "18080", "PANEL_TRUST_PROXY": confia, "PANEL_TRUSTED_PROXY_CIDRS": "192.0.2.0/24"}
    web_app.run_app(SimpleNamespace(config=config), config=config)

    roteador = capturado["roteador"]
    assert isinstance(roteador, web_app.ClientIpRouter)
    assert roteador.trusted_networks == REDES and roteador.trust_proxy is esperado
    assert capturado["kwargs"]["xheaders"] is esperado
