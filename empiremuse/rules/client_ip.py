"""IP do visitante atrás da Cloudflare: o cabeçalho ``CF-Connecting-IP``, só quando o proxy é confiável.

A Cloudflare sempre envia ``CF-Connecting-IP`` com o IP do visitante. O proxy da frente (ex.: Traefik) costuma preencher
``X-Real-IP``/``X-Forwarded-For`` com o IP da BORDA da Cloudflare, e aí todos os visitantes pareceriam o mesmo IP (limite de
tentativas do login e logs quebrados). Sem proxy confiável o cabeçalho é ignorado: um cliente que chega direto poderia forjá-lo.

"Proxy confiável" tem duas camadas: ``PANEL_TRUST_PROXY`` liga o recurso e ``PANEL_TRUSTED_PROXY_CIDRS`` diz QUEM pode mandar o
cabeçalho, medido pelo par TCP (o IP que abriu a conexão, antes de qualquer cabeçalho). Par fora da lista usa o IP do par.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping

CLOUDFLARE_IP_HEADER = "CF-Connecting-IP"
# IP no lugar de um par que não deu para ler: um valor só, que não é endereço de ninguém.
UNKNOWN_PEER = "unknown"
# Padrão de PANEL_TRUSTED_PROXY_CIDRS: loopback e redes privadas (cobre o Traefik na rede interna do compose).
DEFAULT_TRUSTED_PROXY_CIDRS = (
    "127.0.0.0/8",
    "::1/128",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "fc00::/7",
)

type IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """O valor do cabeçalho sem diferenciar maiúsculas de minúsculas (``HTTPHeaders`` já é; um ``dict`` comum não)."""
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def _valid_ip(text: str) -> str | None:
    """O IP no formato canônico, ou ``None`` se não for um endereço IPv4/IPv6 simples (sem lista, porta nem escopo)."""
    try:
        address = ipaddress.ip_address(text.strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.scope_id:
        return None
    return str(address)


def client_ip_from_headers(headers: Mapping[str, str], trust_proxy: bool, fallback: str | None) -> str | None:
    """O IP do visitante: ``CF-Connecting-IP`` válido com proxy confiável; senão ``fallback`` (o IP já resolvido).

    ``fallback`` é o que o servidor já tem (``request.remote_ip``, com ``xheaders`` quando o proxy é confiável). Cabeçalho
    ausente, repetido (valores juntos viram uma lista) ou inválido cai no ``fallback``.
    """
    if not trust_proxy:
        return fallback
    value = _header(headers, CLOUDFLARE_IP_HEADER)
    if value is None:
        return fallback
    return _valid_ip(value) or fallback


def parse_trusted_proxy_cidrs(raw: object) -> tuple[tuple[IPNetwork, ...], tuple[str, ...]]:
    """``PANEL_TRUSTED_PROXY_CIDRS`` (lista separada por vírgula) em redes e avisos (texto fixo, sem os valores digitados).

    Vazio ou ausente usa o padrão (loopback e redes privadas). Com entradas, só elas valem; entrada inválida é ignorada e, se TODAS
    forem inválidas, a lista fica VAZIA (nenhum proxy confiável: o IP do visitante cai para o IP do par), nunca o padrão. Avisa também de
    bits de host (``10.0.0.5/8`` vira ``10.0.0.0/8``) e de rede que cobre todo o endereçamento (``0.0.0.0/0``, ``::/0``).
    Uma tupla vazia significa o mesmo em todo o código: ninguém é confiável.
    """
    items = [item.strip() for item in str(raw or "").split(",") if item.strip()]
    if not items:
        return tuple(ipaddress.ip_network(cidr) for cidr in DEFAULT_TRUSTED_PROXY_CIDRS), ()
    networks: list[IPNetwork] = []
    invalid = host_bits = 0
    for item in items:
        try:
            networks.append(ipaddress.ip_network(item, strict=True))
        except ValueError:
            try:
                networks.append(ipaddress.ip_network(item, strict=False))
                host_bits += 1
            except ValueError:
                invalid += 1
    warnings: list[str] = []
    if invalid:
        warnings.append(f"PANEL_TRUSTED_PROXY_CIDRS: {invalid} entrada(s) inválida(s) foram ignoradas.")
    if host_bits:
        warnings.append(
            f"PANEL_TRUSTED_PROXY_CIDRS: {host_bits} entrada(s) com bits de host; vale a rede correspondente."
        )
    if not networks:
        warnings.append("PANEL_TRUSTED_PROXY_CIDRS: nenhuma entrada válida; nenhum proxy será confiável.")
    if any(network.prefixlen == 0 for network in networks):
        warnings.append(
            "PANEL_TRUSTED_PROXY_CIDRS cobre todo o endereçamento: qualquer cliente pode forjar o IP do visitante."
        )
    return tuple(networks), tuple(warnings)


def peer_address(peer: str | None) -> str | None:
    """O IP do par TCP em forma canônica, sem escopo IPv6 (``fe80::1%eth0``) e com IPv4 mapeado como IPv4; ``None`` se não for IP."""
    if not peer:
        return None
    try:
        address = ipaddress.ip_address(peer.split("%", 1)[0].strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return str(address)


def peer_is_trusted(peer: str | None, networks: tuple[IPNetwork, ...]) -> bool:
    """Se o IP do par TCP está numa das redes confiáveis. Par ausente ou malformado nunca é confiável."""
    canonical = peer_address(peer)
    if canonical is None:
        return False
    address = ipaddress.ip_address(canonical)
    return any(address.version == network.version and address in network for network in networks)


def resolve_client_ip(
    headers: Mapping[str, str], peer: str | None, fallback: str | None, networks: tuple[IPNetwork, ...]
) -> str:
    """O IP do visitante com ``PANEL_TRUST_PROXY`` ligado.

    Par TCP confiável: ``CF-Connecting-IP`` válido, senão ``fallback`` (o IP que o servidor já resolveu a partir de um proxy confiável).
    Par fora das redes confiáveis: SEMPRE o IP do par (sem escopo), nunca o ``fallback``, que já pode carregar o valor de um
    ``X-Forwarded-For``/``X-Real-IP`` forjado. Sem par legível devolve ``UNKNOWN_PEER`` (valor fixo, para o limite por IP não confiar em nada).
    """
    canonical = peer_address(peer)
    if canonical is None:
        return UNKNOWN_PEER
    if not peer_is_trusted(canonical, networks):
        return canonical
    return client_ip_from_headers(headers, True, fallback) or canonical
