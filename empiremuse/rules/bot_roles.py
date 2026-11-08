"""Regras puras dos papéis dos bots do pool (item 11, P2): ordem dos convites, quem avisa e os textos fixos (a janela do aviso é ``NOTICE_WINDOW_SECONDS``).

O principal é o bot do ``TOKEN``; os demais bots do pool são reservas. Aqui só há
decisões e texto; quem sabe quais bots estão em qual servidor é o ``utils/client``.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import NamedTuple

from empiremuse.rules.bot_tokens import Role, role_of, identifier_sort_key as identifier_sort_key
from empiremuse.rules.usage import clean_text, render_text

NOTICE_WINDOW_SECONDS = 24 * 60 * 60  # um aviso de "servidor sem o principal" por servidor a cada 24 h
NAME_LIMIT = 40
PREFIX_LIMIT = 5


class Peer(NamedTuple):
    """Um bot do pool visto pelas regras: ``bot_id`` é o id do usuário do bot."""

    identifier: str
    bot_id: int
    is_main: bool


def invite_order(peers: Iterable[Peer], in_guild: Collection[int]) -> list[Peer]:
    """Os bots que faltam no servidor, na ordem de convite: o principal primeiro (se falta), depois as reservas por ``identifier``."""
    missing = [peer for peer in peers if peer.bot_id not in in_guild]
    return sorted(missing, key=lambda peer: (not peer.is_main, identifier_sort_key(peer.identifier)))


def notifier(reserve_identifiers: Iterable[str]) -> str | None:
    """Quem avisa num servidor sem o principal: a reserva de menor ``identifier`` entre as que estão lá (um aviso só por servidor)."""
    return min(reserve_identifiers, key=identifier_sort_key, default=None)


def main_missing_hint(main_name: object, invite_url: str) -> str:
    """Uma linha para as respostas de uma reserva num servidor sem o principal (o nome é texto de terceiro: limpo e escapado)."""
    name = render_text(main_name, NAME_LIMIT) or "bot principal"
    return f"ℹ️ Os comandos de barra (/) ficam no {name}: <{invite_url}>"


def main_missing_notice(main_name: object, invite_url: str, prefix: object) -> str:
    """Texto fixo do aviso de entrada de uma reserva num servidor sem o principal. O convite é montado pelo código, nunca vem de fora."""
    name = render_text(main_name, NAME_LIMIT) or "bot principal"
    shown = clean_text(prefix, PREFIX_LIMIT).replace("`", "")  # vai entre crases: sem escape de markdown, sem crase
    use = (
        f"Aqui você pode usar `{shown}play` e `{shown}help`." if shown else "Aqui você pode usar os comandos de texto."
    )
    return (
        f"🎵 Olá! Eu sou um bot reserva: os comandos de barra (/) ficam no {name}. "
        f"Adicione o {name} para usá-los: <{invite_url}>\n{use}"
    )


def radio_order(identifiers: Iterable[str]) -> list[str]:
    """Só-rádio primeiro, depois reservas, em ordem natural; principal fica fora."""
    return sorted(
        (name for name in identifiers if role_of(name) in (Role.RADIO, Role.RESERVE)),
        key=lambda name: (role_of(name) is not Role.RADIO, identifier_sort_key(name)),
    )
