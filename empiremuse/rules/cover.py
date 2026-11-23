"""Regras puras da capa de terceiros nas skins de imagem: a lista de hosts (``COVER_HOSTS``) e os tetos da busca e do cache.

A URL da capa vem do metadado da faixa (texto de terceiro). A lista de hosts é a PRIMEIRA barreira (host fora dela: logo, sem
resolver nem conectar); ela não substitui o filtro de IP, que vale nos dois modos (lista e ``*``).

Formato de ``COVER_HOSTS``: hosts separados por espaço, cada um exato (``i.scdn.co``) ou ``*.sufixo`` (curinga só no rótulo da
esquerda, casando exatamente UM rótulo: ``*.dzcdn.net`` casa ``e-cdns-images.dzcdn.net``, não ``dzcdn.net`` nem
``a.b.dzcdn.net``). O valor ``*`` sozinho = qualquer host público. Qualquer outra coisa é inválida e o bot não sobe.
"""

from __future__ import annotations

import re
from typing import NamedTuple

ANY_HOST = "*"
DEFAULT_COVER_HOSTS = (
    "i.ytimg.com img.youtube.com *.sndcdn.com i.scdn.co *.dzcdn.net *.mzstatic.com f4.bcbits.com "
    "static-cdn.jtvnw.net i.vimeocdn.com"
)

MIB = 1024 * 1024

# Busca (no molde do ``LogoFetcher``).
COVER_URL_MAX = 2048
COVER_MAX_BYTES = 2 * MIB
COVER_TOTAL_TIMEOUT_S = 5.0
COVER_CONNECT_TIMEOUT_S = 3.0
COVER_RESOLVE_TIMEOUT_S = 3.0
COVER_READ_CHUNK = 8192
COVER_EGRESS_PARALLEL = 2
COVER_EGRESS_PER_SECOND = 2.0

# Decodificação isolada: só estes formatos, e o filho devolve RGB cru de tamanho fixo.
COVER_FORMATS = ("JPEG", "PNG", "WEBP")
COVER_SIDE = 240
COVER_RAW_BYTES = COVER_SIDE * COVER_SIDE * 3  # 172.800
COVER_MAX_PIXELS = 2048 * 2048
COVER_WORKER_TIMEOUT_S = 4.0

# Cache (por bytes) e espera do render.
COVER_CACHE_BYTES = 8 * MIB  # cerca de 48 capas de 240 × 240 RGB
COVER_FAILURE_TTL_S = 3600.0
COVER_FAILURE_ENTRIES = 4096
COVER_WAIT_S = 1.5  # o render espera a capa no máximo isto (fora da vaga); depois desenha com o logo

# Os únicos códigos que vão ao log (nunca a URL, a query nem o texto de uma exceção).
REASONS = frozenset(
    {
        "url",
        "host",
        "address",
        "redirect",
        "status",
        "size",
        "encoding",
        "format",
        "decode",
        "deadline",
        "network",
        "internal",
        "sandbox",
    }
)

_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_HOST_MAX = 253


class HostRule(NamedTuple):
    any_host: bool
    exact: frozenset[str]
    suffixes: frozenset[str]  # sem o ``*.``


def _valid_name(name: str, *, min_labels: int) -> bool:
    labels = name.split(".")
    return (
        len(name) <= _HOST_MAX
        and len(labels) >= min_labels
        and all(_LABEL.fullmatch(label) for label in labels)
        and not labels[-1].isdigit()  # um IP literal (ou um TLD numérico) nunca é um host da lista
    )


def parse_cover_hosts(value: object) -> HostRule:
    """A lista já validada. ``ValueError`` (sem ecoar o valor inteiro) para qualquer entrada fora do formato."""
    if not isinstance(value, str):
        raise ValueError("COVER_HOSTS deve ser texto")
    tokens = value.lower().split()
    if not tokens:
        raise ValueError("COVER_HOSTS vazio")
    if tokens == [ANY_HOST]:
        return HostRule(True, frozenset(), frozenset())
    exact: set[str] = set()
    suffixes: set[str] = set()
    for token in tokens:
        if token.startswith("*."):
            suffix = token[2:]
            if not _valid_name(suffix, min_labels=2):
                raise ValueError("COVER_HOSTS: curinga inválido")
            suffixes.add(suffix)
        elif _valid_name(token, min_labels=2):
            exact.add(token)
        else:
            raise ValueError("COVER_HOSTS: host inválido (use host exato, *.sufixo ou só *)")
    return HostRule(False, frozenset(exact), frozenset(suffixes))


def normalize_cover_hosts(value: object) -> str:
    """O valor da configuração em forma canônica (vazio = o padrão). Levanta ``ValueError`` se inválido."""
    if value is None or (isinstance(value, str) and not value.strip()):
        value = DEFAULT_COVER_HOSTS
    rule = parse_cover_hosts(value)
    if rule.any_host:
        return ANY_HOST
    return " ".join(sorted(rule.exact) + sorted(f"*.{s}" for s in rule.suffixes))


def host_allowed(rule: HostRule, host: str) -> bool:
    """O host (já em minúsculas, como o ``yarl`` devolve em ``raw_host``) está na lista?"""
    name = host.lower().rstrip(".")
    if not _valid_name(name, min_labels=1):
        return rule.any_host and bool(name)  # um IP literal só passa no modo ``*`` (e o filtro de IP decide)
    if rule.any_host or name in rule.exact:
        return True
    first, _, rest = name.partition(".")
    return bool(first) and rest in rule.suffixes
