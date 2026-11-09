"""``ClientIpRouter``: um só ponto define ``request.remote_ip`` para o painel, a página de status e o relé ``/ws``."""

from types import SimpleNamespace
from typing import Any

import pytest
import tornado.httputil
import tornado.routing

from empiremuse.rules.client_ip import UNKNOWN_PEER, parse_trusted_proxy_cidrs
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


PADRAO = parse_trusted_proxy_cidrs("")[0]


def conexao_com_par(par: Any) -> SimpleNamespace:
    """Conexão cujo socket diz o par TCP (``getpeername``, como o do Tornado), antes de qualquer cabeçalho."""

    def getpeername() -> Any:
        if isinstance(par, Exception):
            raise par
        return par

    return SimpleNamespace(stream=SimpleNamespace(socket=SimpleNamespace(getpeername=getpeername)))


def passar_pelo_roteador(
    trust_proxy: bool, cabecalhos: dict[str, str], par: Any = (PROXY, 40000), redes: Any = PADRAO
) -> tuple[str | None, Any]:
    interno = Interno()
    requisicao = tornado.httputil.HTTPServerRequest(
        method="GET",
        uri="/painel/login",
        headers=tornado.httputil.HTTPHeaders(cabecalhos),
        connection=conexao_com_par(par),
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

    assert passar_pelo_roteador(True, forjados, par=("192.0.2.50", 1))[0] == "192.0.2.50"


def test_par_ipv6_com_escopo_perde_o_escopo() -> None:
    assert passar_pelo_roteador(True, {"CF-Connecting-IP": VISITANTE}, par=("fe80::1%eth0", 1, 0, 2))[0] == "fe80::1"


def test_lista_configurada_vale_no_roteador() -> None:
    redes = parse_trusted_proxy_cidrs("192.0.2.0/24")[0]

    assert (
        passar_pelo_roteador(True, {"CF-Connecting-IP": VISITANTE}, par=("192.0.2.50", 1), redes=redes)[0] == VISITANTE
    )
    assert passar_pelo_roteador(True, {"CF-Connecting-IP": VISITANTE}, par=(PROXY, 1), redes=redes)[0] == PROXY


def test_lista_vazia_significa_ninguem_confiavel() -> None:
    assert passar_pelo_roteador(True, {"CF-Connecting-IP": VISITANTE}, redes=())[0] == PROXY


@pytest.mark.parametrize(
    "par", [OSError(107, "fechado"), AttributeError("sem socket"), "/run/sock", ("lixo", 1)], ids=str
)
def test_par_ilegivel_usa_o_valor_fixo_e_avisa_uma_vez(par: Any, caplog: pytest.LogCaptureFixture) -> None:
    interno = Interno()
    roteador = ClientIpRouter(interno, True, PADRAO)
    for _ in range(2):
        requisicao = tornado.httputil.HTTPServerRequest(
            method="GET",
            uri="/",
            headers=tornado.httputil.HTTPHeaders({"CF-Connecting-IP": VISITANTE, "X-Real-IP": VISITANTE}),
            connection=conexao_com_par(par),
        )
        requisicao.remote_ip = VISITANTE  # o que o xheaders teria posto vindo de um cabeçalho forjado
        with caplog.at_level("WARNING", logger="empiremuse.web_app"):
            roteador.find_handler(requisicao)

    assert interno.ips == [UNKNOWN_PEER, UNKNOWN_PEER]
    assert caplog.text.count("IP do par TCP") == 1


def test_conexao_sem_stream_nao_e_confiavel() -> None:
    interno = Interno()
    requisicao = tornado.httputil.HTTPServerRequest(
        method="GET", uri="/", headers=tornado.httputil.HTTPHeaders({"CF-Connecting-IP": VISITANTE})
    )
    requisicao.remote_ip = BORDA
    ClientIpRouter(interno, True, PADRAO).find_handler(requisicao)

    assert interno.ips == [UNKNOWN_PEER]


def test_o_par_tcp_vem_do_socket_real() -> None:
    """Socket TCP de verdade: ``getpeername`` do servidor é o IP do cliente, mesmo que o ``xheaders`` reescreva ``remote_ip``."""
    import socket

    from empiremuse.web_app import _tcp_peer_ip

    with socket.socket() as servidor:
        servidor.bind(("127.0.0.1", 0))
        servidor.listen(1)
        with socket.create_connection(servidor.getsockname()) as cliente:
            aceito, _ = servidor.accept()
            with aceito:
                conexao = SimpleNamespace(stream=SimpleNamespace(socket=aceito))
                requisicao = tornado.httputil.HTTPServerRequest(method="GET", uri="/", connection=conexao)
                requisicao.remote_ip = VISITANTE  # reescrito pelo xheaders

                assert _tcp_peer_ip(requisicao) == "127.0.0.1" == cliente.getsockname()[0]


@pytest.mark.asyncio
async def test_servidor_tornado_local_entrega_o_par_real_pela_cadeia_connection_stream_socket() -> None:
    """HTTPServer de verdade: ``request.connection.stream.socket`` existe e dá o par TCP, com ``xheaders`` reescrevendo ``remote_ip``."""
    import tornado.httpserver
    import tornado.httpclient
    import tornado.netutil

    visto: dict[str, str | None] = {}

    class Espiao(tornado.routing.Router):
        def find_handler(self, request: tornado.httputil.HTTPServerRequest, **kwargs: Any) -> Any:
            from empiremuse.web_app import _tcp_peer_ip

            visto["par"], visto["remote_ip"] = _tcp_peer_ip(request), request.remote_ip

            class Resposta(tornado.httputil.HTTPMessageDelegate):
                def finish(self) -> None:
                    request.connection.write_headers(
                        tornado.httputil.ResponseStartLine("HTTP/1.1", 200, "OK"),
                        tornado.httputil.HTTPHeaders({"Content-Length": "0"}),
                    )
                    request.connection.finish()

                def on_connection_close(self) -> None:
                    pass

            return Resposta()

    sockets = tornado.netutil.bind_sockets(0, "127.0.0.1")
    servidor = tornado.httpserver.HTTPServer(Espiao(), xheaders=True)
    servidor.add_sockets(sockets)
    porta = sockets[0].getsockname()[1]
    try:
        resposta = await tornado.httpclient.AsyncHTTPClient().fetch(
            f"http://127.0.0.1:{porta}/", headers={"X-Real-IP": VISITANTE}
        )
    finally:
        servidor.stop()

    assert resposta.code == 200
    assert visto == {"par": "127.0.0.1", "remote_ip": VISITANTE}  # o par é o real; o xheaders reescreveu só o remote_ip
