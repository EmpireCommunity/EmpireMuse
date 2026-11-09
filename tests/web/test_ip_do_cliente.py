"""``ClientIpRouter``: um só ponto define ``request.remote_ip`` para o painel, a página de status e o relé ``/ws``."""

from types import SimpleNamespace
from typing import Any

import pytest
import tornado.httputil
import tornado.routing

from empiremuse.rules.client_ip import parse_trusted_proxy_cidrs
from empiremuse.web_app import ClientIpRouter

pytestmark = pytest.mark.regression

BORDA = "172.70.1.1"  # o IP que o Tornado enxerga atrás do proxy: a borda da Cloudflare, igual para todos os visitantes
VISITANTE = "203.0.113.7"
PROXY = "172.18.0.2"  # o par TCP: o Traefik na rede privada do compose


class Interno(tornado.routing.Router):
    """O roteador de baixo: guarda o IP que recebeu e devolve um marcador no lugar do handler."""

    def __init__(self) -> None:
        self.ips: list[str | None] = []

    def find_handler(self, request: tornado.httputil.HTTPServerRequest, **kwargs: Any) -> Any:
        self.ips.append(request.remote_ip)
        return "handler"


def passar_pelo_roteador(
    trust_proxy: bool, cabecalhos: dict[str, str], par: str | None = PROXY, redes: Any = None
) -> tuple[str | None, Any]:
    interno = Interno()
    conexao = SimpleNamespace(
        context=SimpleNamespace(_orig_remote_ip=par)
    )  # o contexto real do Tornado guarda o par antes do xheaders
    requisicao = tornado.httputil.HTTPServerRequest(
        method="GET", uri="/painel/login", headers=tornado.httputil.HTTPHeaders(cabecalhos), connection=conexao
    )
    requisicao.remote_ip = BORDA  # já resolvido pelo servidor (xheaders), antes do roteamento
    delegate = ClientIpRouter(interno, trust_proxy, redes).find_handler(requisicao)
    return interno.ips[0], delegate


def test_com_proxy_confiavel_o_ip_do_visitante_chega_ao_painel_e_ao_rele() -> None:
    ip, delegate = passar_pelo_roteador(True, {"CF-Connecting-IP": VISITANTE, "X-Real-IP": BORDA})
    assert ip == VISITANTE and delegate == "handler"


@pytest.mark.parametrize(
    "cabecalhos",
    [{}, {"CF-Connecting-IP": "não é um ip"}, {"CF-Connecting-IP": f"{VISITANTE}, 198.51.100.1"}],
    ids=["ausente", "invalido", "lista"],
)
def test_sem_cabecalho_valido_vale_o_ip_ja_resolvido_pelo_servidor(cabecalhos: dict[str, str]) -> None:
    assert passar_pelo_roteador(True, cabecalhos)[0] == BORDA


def test_sem_proxy_confiavel_o_cabecalho_forjado_e_ignorado() -> None:
    assert passar_pelo_roteador(False, {"CF-Connecting-IP": VISITANTE})[0] == BORDA


def test_par_fora_das_redes_confiaveis_fica_com_o_proprio_ip_mesmo_com_cabecalhos_forjados() -> None:
    forjados = {"CF-Connecting-IP": VISITANTE, "X-Forwarded-For": "198.51.100.1", "X-Real-IP": "198.51.100.1"}

    assert passar_pelo_roteador(True, forjados, par="192.0.2.50")[0] == "192.0.2.50"


def test_lista_configurada_vale_no_roteador() -> None:
    redes = parse_trusted_proxy_cidrs("192.0.2.0/24")[0]

    assert passar_pelo_roteador(True, {"CF-Connecting-IP": VISITANTE}, par="192.0.2.50", redes=redes)[0] == VISITANTE
    assert passar_pelo_roteador(True, {"CF-Connecting-IP": VISITANTE}, par=PROXY, redes=redes)[0] == PROXY


def test_conexao_sem_contexto_do_tornado_nao_e_confiavel() -> None:
    interno = Interno()
    requisicao = tornado.httputil.HTTPServerRequest(
        method="GET", uri="/", headers=tornado.httputil.HTTPHeaders({"CF-Connecting-IP": VISITANTE})
    )
    requisicao.remote_ip = BORDA
    ClientIpRouter(interno, True).find_handler(requisicao)

    assert interno.ips == [BORDA]


def test_o_par_tcp_vem_do_contexto_real_do_tornado() -> None:
    """Se uma versão do Tornado renomear ``_orig_remote_ip``, este teste falha em vez de o painel deixar de confiar no proxy."""
    import socket

    import tornado.httpserver

    from empiremuse.web_app import _tcp_peer_ip

    with socket.socket(socket.AF_INET) as sock:
        contexto = tornado.httpserver._HTTPRequestContext(SimpleNamespace(socket=sock), (PROXY, 40000), "http")  # type: ignore[arg-type]
        contexto._apply_xheaders(tornado.httputil.HTTPHeaders({"X-Real-IP": VISITANTE}))
        requisicao = tornado.httputil.HTTPServerRequest(
            method="GET", uri="/", connection=SimpleNamespace(context=contexto)
        )

        assert contexto.remote_ip == VISITANTE and _tcp_peer_ip(requisicao) == PROXY
