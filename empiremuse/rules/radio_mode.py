"""Modo rádio e estação da Jukebox (T-434 J2a e J2b): a porta DJ/voto/recusa, o quórum da votação e o recuo da queda, sem I/O.

O controlador (``utils/radio/jukebox_radio.py``) junta o que só ele sabe (membro, player, relógio) e pergunta aqui. Nada daqui lê o
Discord, o banco ou o relógio: tudo chega por argumento, para a tabela de testes cobrir cada caso.
"""

from __future__ import annotations

import math
from enum import StrEnum
from typing import NamedTuple

VOTE_TTL_S = 60
VOTE_COOLDOWN_S = 120  # mesmo autor, mesmo player, depois de uma votação que expirou [m5]
SWITCH_COOLDOWN_S = 5  # entre trocas de rádio no mesmo player, DJ incluído [s1]
LIVE_RETRY_S = (2, 4, 8)  # 3 tentativas depois de uma queda (P11)
RETRY_RESET_S = 120  # no ar por tanto tempo, a próxima queda recomeça em 2 s (o ``STABLE_AFTER_S`` da 24 h)
VOTE_CAP_DEFAULT = 3


class Decision(StrEnum):
    DIRECT = "direct"
    VOTE = "vote"
    DENY = "deny"


class Target(StrEnum):
    LIVE = "live"  # entrar no modo rádio ou trocar a estação ao vivo (A4)
    LEAVE = "leave"  # sair do modo rádio por um pedido de música (A4, plano B)
    STATION = "station"  # ligar ou trocar a estação (pronta ou do servidor) no lugar do autoplay (A5, J2b)


class Actor(NamedTuple):
    """Quem pediu. ``is_dj`` = permissão REAL (``can_act_as_dj``), nunca a autopromoção de ``player.dj`` [m2/Q2]."""

    is_dj: bool
    restricted: bool  # o player está em modo restrito (``restrict_mode``)


class PlayerState(NamedTuple):
    """O que o player tem agora, lido dentro do ``radio_lock``."""

    playing: bool  # há faixa atual
    queued: bool  # há música na fila
    radio: bool  # já está no modo rádio
    station: bool = False  # estação ativa (J2b)
    foreign_queue: bool = False  # há na fila música pedida por OUTRA pessoa (A5: só a do próprio autor não pede voto)

    @property
    def busy(self) -> bool:
        return self.playing or self.queued or self.radio or self.station


class Gate(NamedTuple):
    decision: Decision
    reason: str = ""  # chave de texto da recusa; vazia nos outros casos


def gate(actor: Actor, state: PlayerState, target: Target) -> Gate:
    """DIRECT, VOTE ou DENY para o pedido. A tela avisa antes, mas a decisão é sempre esta, refeita no clique e de novo dentro do lock."""
    if target is Target.LIVE and not state.busy:
        return Gate(Decision.DIRECT)  # player ocioso e fila vazia: sem aviso nem voto
    if target is Target.STATION and not (state.radio or state.station or state.foreign_queue):
        return Gate(
            Decision.DIRECT
        )  # fila vazia ou só com músicas do autor, sem R nem S: a música atual termina e a estação segue
    if actor.is_dj:
        return Gate(Decision.DIRECT)
    if actor.restricted:
        return Gate(Decision.DENY, "jukebox.radio.restricted")
    return Gate(Decision.VOTE)


def quorum(humans: int, cap: int) -> int:
    """Votos para passar: metade dos humanos elegíveis, para cima, entre 1 e ``cap`` (Q1). Sozinho (ou ninguém elegível) = 1."""
    ceiling = max(1, int(cap))
    return max(1, min(math.ceil(max(0, humans) / 2), ceiling))


def vote_passes(votes: int, humans: int, cap: int) -> bool:
    return votes >= quorum(humans, cap)


def switch_wait(last_switch: float | None, now: float) -> float:
    """Segundos que faltam para outra troca de rádio no mesmo player (0 = pode)."""
    if last_switch is None:
        return 0.0
    return max(0.0, SWITCH_COOLDOWN_S - (now - last_switch))


def cooldown_wait(expired_at: float | None, now: float) -> float:
    """Segundos que faltam para o mesmo autor abrir outra votação no mesmo player, depois de uma que expirou (0 = pode)."""
    if expired_at is None:
        return 0.0
    return max(0.0, VOTE_COOLDOWN_S - (now - expired_at))


class Retry(NamedTuple):
    delay: float | None  # ``None`` = esgotou: a Jukebox para
    attempt: int  # quantas tentativas já foram gastas depois desta decisão


def next_retry(attempt: int, on_air_since: float | None, now: float) -> Retry:
    """Depois de uma queda: espera 2, 4 e 8 s; a 4ª queda seguida esgota. Ficar no ar ``RETRY_RESET_S`` zera a contagem."""
    if on_air_since is not None and now - on_air_since >= RETRY_RESET_S:
        attempt = 0
    if attempt >= len(LIVE_RETRY_S):
        return Retry(None, attempt)
    return Retry(float(LIVE_RETRY_S[attempt]), attempt + 1)
