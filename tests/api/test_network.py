"""Rede (contrato T-456 §2 [M3, N1]): a conexão de par TCP fora de ``EXTERNAL_PANEL_API_CIDRS`` é fechada sem resposta."""

import ipaddress

import pytest
from tornado.httpclient import AsyncHTTPClient
from tornado.simple_httpclient import HTTPStreamClosedError
from tornado.testing import AsyncHTTPTestCase

from empiremuse.api.server import ApiHTTPServer, build_api_app, peer_allowed
from tests.api.support import ApiTestCase

pytestmark = pytest.mark.critical

NETS = (ipaddress.ip_network("172.20.0.0/24"), ipaddress.ip_network("2001:db8::/64"))


@pytest.mark.parametrize(
    ("peer", "esperado"),
    [
        ("172.20.0.7", True),
        ("172.20.1.7", False),
        ("127.0.0.1", False),
        ("2001:db8::5", True),
        ("2001:db9::5", False),
        ("::ffff:172.20.0.9", True),  # IPv4 mapeado em IPv6 vale como o IPv4
        ("::ffff:10.0.0.9", False),
        ("nao-e-ip", False),
    ],
)
def test_peer_allowed_confere_o_par_tcp_nas_sub_redes(peer, esperado):
    assert peer_allowed((peer, 5000), NETS) is esperado


@pytest.mark.parametrize("address", [None, "/run/socket", (), ("", 1)])
def test_endereco_ilegivel_nunca_e_permitido(address):
    assert peer_allowed(address, NETS) is False


class PeerOutsideCidrsTest(ApiTestCase):
    networks = (ipaddress.ip_network("172.20.0.0/24"),)  # o cliente do teste conecta de 127.0.0.1

    def test_conexao_de_fora_da_sub_rede_e_fechada_sem_resposta(self):
        with pytest.raises(HTTPStreamClosedError):  # a conexão cai antes de qualquer resposta HTTP
            self.api("GET", "/health", key=None)


class PeerInsideCidrsTest(ApiTestCase):
    networks = (ipaddress.ip_network("127.0.0.0/8"),)

    def test_conexao_de_dentro_da_sub_rede_e_atendida(self):
        assert self.api("GET", "/health", key=None).code == 200


class DeniedLogTest(AsyncHTTPTestCase):
    def get_app(self):
        return build_api_app(None)  # nunca atende: toda conexão é negada

    def get_http_server(self):
        self.now = [0.0]
        return ApiHTTPServer(self._app, NETS, clock=lambda: self.now[0])

    def hit(self):
        async def call():
            with pytest.raises(HTTPStreamClosedError):
                await AsyncHTTPClient().fetch(self.get_url("/"))

        self.io_loop.run_sync(call)

    def test_conexoes_negadas_geram_um_aviso_por_minuto_com_a_contagem(self):
        with self.assertLogs("empiremuse.api.server", level="WARNING") as logs:
            for _ in range(3):
                self.hit()
        assert len(logs.output) == 1

        self.now[0] = 61.0
        with self.assertLogs("empiremuse.api.server", level="WARNING") as logs:
            self.hit()
        assert "3 conexão" in logs.output[0]  # as 2 sem aviso mais a nova
