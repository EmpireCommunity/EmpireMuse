"""Confiança e autoridade HTTP dos nós Lavalink, sem resolução de nomes."""

import ipaddress


def is_loopback_host(host: object) -> bool:
    """Só localhost (sem ponto final) ou um literal IP de loopback; colchetes valem apenas para IPv6."""
    if not isinstance(host, str):
        return False
    if host.lower() == "localhost":
        return True
    bracketed = host.startswith("[") and host.endswith("]")
    literal = host[1:-1] if bracketed else host
    try:
        address = ipaddress.ip_address(literal)
    except ValueError:
        return False
    return address.is_loopback and (not bracketed or address.version == 6)


def node_authority(host: str, port: int | str) -> str:
    """Autoridade host:porta de REST e WebSocket, com um único par de colchetes para IPv6."""
    try:
        if ipaddress.ip_address(host).version == 6:
            host = f"[{host}]"
    except ValueError:
        pass  # nome de host ou IPv6 que já veio com colchetes
    return f"{host}:{port}"


def node_rest_uri(host: str, port: int | str, secure: bool = False) -> str:
    return f"{'https' if secure else 'http'}://{node_authority(host, port)}"
