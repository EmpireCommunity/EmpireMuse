"""Como pedir as funções Limitadas a Apoiadores (rádios): concedidas à mão por quem cuida do bot, nunca uma compra.

O pedido é por ticket no servidor de suporte CONFIGURADO (``SUPPORT_SERVER``; quem hospeda uma cópia põe o seu). Sem link válido, a frase
genérica. Regra pura: recebe o tradutor e o texto da configuração.
"""

from __future__ import annotations

from collections.abc import Callable

from empiremuse.rules.urls import is_clean_url

NO_PREMIUM = "no_premium"  # o código de recusa de servidor sem acesso às rádios (``radio.refused.no_premium``)
ASK_SUPPORT = "radio.premium.ask.support"
ASK_GENERIC = "radio.premium.ask.generic"
_HTTPS = "https://"


def support_link(value: object) -> str:
    """O link do servidor de suporte se é um ``https://`` limpo (``is_clean_url``: sem markdown, menção nem credencial); senão vazio."""
    if not isinstance(value, str):
        return ""
    url = value.strip()
    return url if url.startswith(_HTTPS) and is_clean_url(url) else ""


def ask_line(tr: Callable[..., str], support_url: object) -> str:
    """A frase de como pedir: o ticket no servidor de suporte (link entre ``<>``, sem prévia no Discord) ou a genérica."""
    link = support_link(support_url)
    return tr(ASK_SUPPORT, link=f"<{link}>") if link else tr(ASK_GENERIC)


def with_ask(tr: Callable[..., str], text: str, support_url: object) -> str:
    """``text`` (o aviso de servidor sem acesso) seguido de como pedir."""
    return f"{text} {ask_line(tr, support_url)}"


def refused_text(tr: Callable[..., str], code: str, support_url: object) -> str:
    """``radio.refused.<code>``; o de servidor sem acesso (``no_premium``) ganha como pedir."""
    text = tr(f"radio.refused.{code}")
    return with_ask(tr, text, support_url) if code == NO_PREMIUM else text


# ---------------------------------------------------------------- comandos Limitados a Apoiadores (fonte do site-data)

SUPPORTER_LIMITED_ATTR = "__supporter_limited__"


def limited_to_supporters[F: Callable[..., object]](func: F) -> F:
    """Marca o callback de um comando que RECUSA sem o acesso de Apoiador do servidor (a recusa ``no_premium``). Fica ao lado da definição do
    comando; o exportador do site-data lê a marca (``supporter_limited``) e um teste confere que todo comando marcado recusa sem o acesso e
    que nenhum não marcado recusa por isso. Ponha a marca ABAIXO do decorador do comando (ela vai na função)."""
    setattr(func, SUPPORTER_LIMITED_ATTR, True)
    return func


def is_limited_to_supporters(callback: object) -> bool:
    return getattr(callback, SUPPORTER_LIMITED_ATTR, False) is True
