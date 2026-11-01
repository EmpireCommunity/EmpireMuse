"""Regras de URL: a URL pura (``check_url``/``clean_url``) vale para modais, importadores, painel e botões de favoritar."""

from __future__ import annotations

import ipaddress
import re
from typing import Any, NamedTuple, Optional
from urllib.parse import urlsplit

from empiremuse.rules.errors import URL_INVALID, URL_TOO_LONG, RuleError
from empiremuse.rules.limits import ITG_AVATAR_MAX

URL_REG = re.compile("http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*(),]|%[0-9a-fA-F][0-9a-fA-F])+")
_MASS_MENTION_REG = re.compile(r"@(?:everyone|here)", re.IGNORECASE)
_UNSAFE_CHARS = frozenset("[]<>)\\")


def _has_userinfo(url: str) -> bool:
    """Tem ``usuário[:senha]@`` na autoridade (entre ``://`` e a 1ª ``/`` ou ``?``)? O ``@`` do caminho e da consulta vale."""
    authority = re.split(r"[/?]", url.split("://", 1)[-1], maxsplit=1)[0]
    return "@" in authority


def is_clean_url(url: str) -> bool:
    """A URL INTEIRA casa com ``URL_REG`` e não tem colchetes (markdown), ``<``, ``>``, ``)`` nem ``\\`` (que fecham ou
    abrem link e menção na mensagem), menções em massa nem credencial (``usuário:senha@host``)."""
    if not URL_REG.fullmatch(url) or any(char in url for char in _UNSAFE_CHARS):
        return False
    return not (_MASS_MENTION_REG.search(url) or _has_userinfo(url))


def _unwrap(raw: str) -> str:
    """Apara as pontas e tira o ``<`` e o ``>`` que envolvem o link (usados no Discord para não gerar prévia)."""
    text = raw.strip()
    if len(text) >= 2 and text.startswith("<") and text.endswith(">"):
        text = text[1:-1].strip()
    return text


def check_url(raw: Any, max_chars: Optional[int] = None) -> str | RuleError:
    """URL pura: apara as pontas, desembrulha ``<...>`` e exige a URL INTEIRA limpa (sem texto ao redor, colchetes nem
    menções em massa). ``URL_TOO_LONG`` se passar de ``max_chars``; ``URL_INVALID`` nos demais casos."""
    if not isinstance(raw, str):
        return RuleError(URL_INVALID, value=raw)
    text = _unwrap(raw)
    if max_chars is not None and len(text) > max_chars:
        return RuleError(URL_TOO_LONG, value=text, limit=max_chars)
    return text if is_clean_url(text) else RuleError(URL_INVALID, value=text)


def clean_url(raw: Any, max_chars: Optional[int] = None) -> Optional[str]:
    """A URL pura de ``check_url``, ou ``None`` se não vale (qualquer motivo)."""
    result = check_url(raw, max_chars)
    return None if isinstance(result, RuleError) else result


def clean_avatar(avatar) -> Optional[str]:
    """Avatar opcional da integração: ``""`` se ausente (``None`` ou vazio), a URL pura, ou ``None`` se inválido."""
    if avatar is None or avatar == "":
        return ""
    return clean_url(avatar, ITG_AVATAR_MAX)


# ---------------------------------------------------------------------- link que vai para o Lavalink (filtro de IP)

_AMBIGUOUS_NUMERIC_HOST = re.compile(r"(?:0x[0-9a-f]*|\d+)(?:\.(?:0x[0-9a-f]*|\d+))*", re.IGNORECASE)
_BAD_URL_CHARS = re.compile(
    r"[\x00-\x1f\x7f\\]"
)  # controle (tab, quebra de linha: alguns parsers os apagam no meio do link) e barra invertida


class UrlTarget(NamedTuple):
    """O destino de um link http(s) que vai ao Lavalink. ``refused``: a sintaxe é ambígua ou perigosa e o link nem chega a ser resolvido."""

    host: str  # em minúsculas, sem colchetes nem ponto final; vazio se a sintaxe foi recusada
    refused: bool = False


def lavalink_target(query: object) -> Optional[UrlTarget]:
    """O host de um link ``http(s)://`` que o Lavalink buscaria, ou ``None`` se o texto não é link (busca por nome, ``ytsearch:...``).

    Recusa por sintaxe, antes de qualquer DNS, o que cada parser lê de um jeito: ``usuário@host``, barra invertida, espaço/controle no
    link, porta inválida, host vazio e host numérico fora da forma canônica (``2130706433``, ``0x7f.1``, ``010.0.0.1``, ``127.1``), porque
    a biblioteca do Lavalink e o sistema podem resolvê-lo para endereços diferentes do que aqui se conferiu."""
    if not isinstance(query, str):
        return None
    text = query.strip().strip("<>").strip()
    if not text[:8].lower().startswith(("http://", "https://")):
        return None
    if _BAD_URL_CHARS.search(text):
        return UrlTarget("", True)
    token = text.split(" ", 1)[0]  # o que vem depois do 1º espaço não é o link (um pedido pode ter texto junto)
    try:
        parts = urlsplit(token)
        parts.port  # noqa: B018 - levanta ValueError numa porta inválida
        host = parts.hostname
    except ValueError:
        return UrlTarget("", True)
    if "@" in parts.netloc or not host:
        return UrlTarget("", True)
    host = host.rstrip(".").lower()
    if not host:
        return UrlTarget("", True)
    if _AMBIGUOUS_NUMERIC_HOST.fullmatch(host):
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return UrlTarget(host, True)
    return UrlTarget(host)
