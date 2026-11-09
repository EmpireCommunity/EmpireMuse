"""``client_ip_from_headers``: o IP do visitante vem de ``CF-Connecting-IP`` só com proxy confiável e só se for um IP válido."""

import pytest

from empiremuse.rules.client_ip import client_ip_from_headers

pytestmark = pytest.mark.regression

BORDA = "172.70.1.1"  # o IP que o proxy da frente enxerga: a borda da Cloudflare, igual para todos os visitantes


@pytest.mark.parametrize(
    ("enviado", "esperado"),
    [
        ("203.0.113.7", "203.0.113.7"),
        ("  203.0.113.7  ", "203.0.113.7"),
        ("2001:db8::1", "2001:db8::1"),
        ("2001:0DB8:0:0:0:0:0:1", "2001:db8::1"),
    ],
    ids=["ipv4", "ipv4-com-espacos", "ipv6", "ipv6-canonico"],
)
def test_ip_valido_do_cabecalho_e_usado_com_proxy_confiavel(enviado: str, esperado: str) -> None:
    assert client_ip_from_headers({"CF-Connecting-IP": enviado}, True, BORDA) == esperado


@pytest.mark.parametrize(
    "enviado",
    ["", "abc", "999.1.1.1", "203.0.113.7, 198.51.100.1", "203.0.113.7:443", "fe80::1%eth0", "<script>"],
    ids=["vazio", "texto", "fora-da-faixa", "lista", "com-porta", "com-escopo", "marcacao"],
)
def test_valor_invalido_e_ignorado_e_vale_o_ip_ja_resolvido(enviado: str) -> None:
    assert client_ip_from_headers({"CF-Connecting-IP": enviado}, True, BORDA) == BORDA


def test_sem_proxy_confiavel_o_cabecalho_e_ignorado_mesmo_valido() -> None:
    """Um cliente que chega direto poderia forjar o cabeçalho."""
    assert client_ip_from_headers({"CF-Connecting-IP": "203.0.113.7"}, False, "198.51.100.9") == "198.51.100.9"


def test_cabecalho_ausente_mantem_o_ip_ja_resolvido() -> None:
    assert client_ip_from_headers({"X-Real-IP": "10.0.0.1"}, True, BORDA) == BORDA
    assert client_ip_from_headers({}, True, None) is None


def test_nome_do_cabecalho_nao_diferencia_maiusculas() -> None:
    assert client_ip_from_headers({"cf-connecting-ip": "203.0.113.7"}, True, BORDA) == "203.0.113.7"


# ----- T21: o cabeçalho só vale com o par TCP em rede confiável (PANEL_TRUSTED_PROXY_CIDRS) -----

from empiremuse.rules.client_ip import (  # noqa: E402
    UNKNOWN_PEER,
    parse_trusted_proxy_cidrs,
    peer_is_trusted,
    resolve_client_ip,
)

PADRAO = parse_trusted_proxy_cidrs("")[0]
SO_A_REDE_X = parse_trusted_proxy_cidrs("198.51.100.0/24, 2001:db8:abcd::/48")[0]


@pytest.mark.critical
@pytest.mark.parametrize(
    ("par", "cabecalho", "esperado"),
    [
        ("172.18.0.2", "203.0.113.7", "203.0.113.7"),  # Traefik na rede privada do compose
        ("10.0.0.5", "2001:db8::7", "2001:db8::7"),
        ("127.0.0.1", "203.0.113.7", "203.0.113.7"),
        ("::1", "203.0.113.7", "203.0.113.7"),
        ("fd00::2", "203.0.113.7", "203.0.113.7"),
        ("::ffff:10.0.0.2", "203.0.113.7", "203.0.113.7"),  # IPv4 mapeado em IPv6 é o mesmo par
        ("172.18.0.2", "lixo", BORDA),  # par confiável, cabeçalho malformado: vale o IP já resolvido
        ("172.18.0.2", None, BORDA),
        ("203.0.113.9", "198.51.100.1", "203.0.113.9"),  # par público forjando o cabeçalho: vale o par
        ("2001:db8::9", "198.51.100.1", "2001:db8::9"),
        ("172.32.0.1", "198.51.100.1", "172.32.0.1"),  # logo fora de 172.16.0.0/12
        ("::ffff:203.0.113.9", "198.51.100.1", "203.0.113.9"),  # mapeado vira IPv4
        ("fe80::1%eth0", "198.51.100.1", "fe80::1"),  # sem escopo
        (None, "198.51.100.1", UNKNOWN_PEER),  # sem par legível: nunca o fallback, que já carrega o cabeçalho
        ("não é ip", "198.51.100.1", UNKNOWN_PEER),
        ("", "198.51.100.1", UNKNOWN_PEER),
    ],
)
def test_cf_connecting_ip_so_vale_com_o_par_na_lista_padrao(par, cabecalho, esperado) -> None:
    cabecalhos = {} if cabecalho is None else {"CF-Connecting-IP": cabecalho}

    assert resolve_client_ip(cabecalhos, par, BORDA, PADRAO) == esperado


def test_fallback_com_o_valor_do_cabecalho_nunca_vaza_para_par_nao_confiavel() -> None:
    """O ``fallback`` do servidor pode ser o ``X-Forwarded-For`` forjado: com par fora da lista ele nunca é usado."""
    forjado = "198.51.100.66"

    assert resolve_client_ip({"CF-Connecting-IP": forjado}, "203.0.113.9", forjado, PADRAO) == "203.0.113.9"
    assert resolve_client_ip({"CF-Connecting-IP": forjado}, None, forjado, PADRAO) == UNKNOWN_PEER


@pytest.mark.critical
@pytest.mark.parametrize(
    ("par", "confiavel"),
    [
        ("198.51.100.7", True),
        ("2001:db8:abcd::1", True),
        ("10.0.0.1", False),
        ("198.51.101.7", False),
        ("127.0.0.1", False),
    ],
)
def test_lista_configurada_substitui_o_padrao(par, confiavel) -> None:
    assert peer_is_trusted(par, SO_A_REDE_X) is confiavel


@pytest.mark.parametrize(
    ("bruto", "redes", "avisos"),
    [
        (None, 6, 0),
        ("", 6, 0),
        (" , ,", 6, 0),
        ("198.51.100.0/24, 10.0.0.0/8", 2, 0),
        ("198.51.100.0/24, lixo, 300.1.1.1/8", 1, 1),  # inválida ignorada, com aviso
        ("10.0.0.5/8", 1, 1),  # bits de host: vale a rede, com aviso
        ("lixo, 999.0.0.0/8", 0, 2),  # tudo inválido: FALHA FECHADA (ninguém confiável), nunca o padrão
        ("0.0.0.0/0", 1, 1),  # tudo confiável: aviso
        ("::/0, 10.0.0.0/8", 2, 1),
    ],
)
def test_parse_trusted_proxy_cidrs(bruto, redes, avisos) -> None:
    resultado, mensagens = parse_trusted_proxy_cidrs(bruto)

    assert len(resultado) == redes and len(mensagens) == avisos
    assert all("lixo" not in m and "999" not in m for m in mensagens)  # o aviso nunca repete o valor digitado


def test_tudo_invalido_deixa_a_lista_vazia_e_ninguem_e_confiavel() -> None:
    redes, _ = parse_trusted_proxy_cidrs("lixo")

    assert (
        redes == () and resolve_client_ip({"CF-Connecting-IP": "203.0.113.7"}, "10.0.0.2", BORDA, redes) == "10.0.0.2"
    )


def test_painel_le_as_redes_confiaveis_e_repassa_os_avisos_sem_citar_o_valor(caplog) -> None:
    from empiremuse.panel.settings import PanelSettings, load_panel_settings
    from tests.panel.helpers import local_config

    with caplog.at_level("WARNING", logger="empiremuse.panel.settings"):
        settings = load_panel_settings(local_config(PANEL_TRUSTED_PROXY_CIDRS="198.51.100.0/24, valor-invalido-xyz"))

    assert [str(rede) for rede in settings.trusted_proxy_networks] == ["198.51.100.0/24"]
    assert "1 entrada" in caplog.text and "valor-invalido-xyz" not in caplog.text
    assert load_panel_settings(local_config()).trusted_proxy_networks == PADRAO
    assert (
        PanelSettings(enabled=False).trusted_proxy_networks == PADRAO
    )  # vazio = padrão em todo lugar; () só se configurado
