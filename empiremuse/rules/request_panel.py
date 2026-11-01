"""Regras puras do canal de pedidos único e do painel multi-player (item 11, P4).

O canal de pedidos é um por servidor, para todo o pool. Quem envia, edita e trata as mensagens dele é UM bot, o do painel: o principal se
está no servidor, senão a primeira reserva presente. O painel mostra um bloco por player ativo; a call que ``@everyone`` não enxerga nunca
mostra faixa nem quem pediu (S-V7). Aqui só há decisões e dados; os bots, o banco e o Discord são do ``utils``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from enum import StrEnum
from typing import NamedTuple

from empiremuse.rules.bot_roles import identifier_sort_key
from empiremuse.rules.servers import is_main_identifier
from empiremuse.rules.usage import clean_text

PANEL_REFRESH_MS = 3000  # no máximo uma edição do painel por servidor a cada 3 s
PANEL_TICK_MS = 15000  # com música tocando, o painel em lista se redesenha sozinho (progresso) a cada 15 s
HEAL_MIN_INTERVAL_MS = 5000  # painel apagado: no máximo um painel novo a cada 5 s por servidor (quem apaga em laço não vira laço de envios)
PROGRESS_WIDTH = 10
QUEUE_PAGE_SIZE = 25  # o teto de opções de um select
MAX_GENRE_OPTIONS = 25
QUEUE_BUTTON_ID = "request_panel_queue"
GENRE_SELECT_ID = "request_panel_genre"
NAME_LIMIT = 40
TITLE_LIMIT = 100
URL_LIMIT = 250

PLAYING = "playing"
PAUSED = "paused"


class PanelPeer(NamedTuple):
    """Um bot que está no servidor, visto pela eleição: ``exclusive`` = bot exclusivo de servidor (só entra se não há mais ninguém)."""

    identifier: str
    bot_id: int
    exclusive: bool = False


class PanelEntry(NamedTuple):
    """Um player no painel. Com ``private`` não há faixa nem link (S-V7); os textos já vêm limpos e curtos."""

    bot_name: str
    bot_id: int
    voice_name: str
    private: bool
    title: str | None
    url: str | None
    state: str
    queue: int
    requester: str | None = None
    volume: int | None = None
    position_ms: int | None = None
    duration_ms: int | None = None  # ``None`` = sem duração (transmissão ao vivo) ou call privada


def elect_panel_bot(peers: Iterable[PanelPeer]) -> int | None:
    """Id do bot do painel: o principal se está no servidor; senão a 1ª reserva (ordem natural do ``identifier``); por último um exclusivo."""
    ordered = sorted(
        peers, key=lambda p: (p.exclusive, not is_main_identifier(p.identifier), identifier_sort_key(p.identifier))
    )
    return ordered[0].bot_id if ordered else None


def make_entry(
    *,
    bot_name: object,
    bot_id: int,
    voice_name: object,
    private: bool,
    title: object,
    url: object,
    paused: bool,
    queue: int,
    requester: object = None,
    volume: object = None,
    position_ms: object = None,
    duration_ms: object = None,
) -> PanelEntry:
    """A entrada do painel. Call privada: sem faixa, sem link e sem quem pediu, aconteça o que acontecer com o que veio de fora."""
    return PanelEntry(
        bot_name=clean_text(bot_name, NAME_LIMIT) or "bot",
        bot_id=bot_id,
        voice_name=clean_text(voice_name, NAME_LIMIT) or "call",
        private=bool(private),
        title=None if private else (clean_text(title, TITLE_LIMIT) or None),
        url=None if private else (clean_text(url, URL_LIMIT) or None),
        state=PAUSED if paused else PLAYING,
        queue=max(0, int(queue)),
        requester=None if private else (clean_text(requester, NAME_LIMIT) or None),
        volume=None if private else _bounded(volume, 0, 1000),
        position_ms=None if private else _non_negative(position_ms),
        duration_ms=None if private else _positive(duration_ms),
    )


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _bounded(value: object, low: int, high: int) -> int | None:
    number = _int_or_none(value)
    return None if number is None else max(low, min(high, number))


def _non_negative(value: object) -> int | None:
    number = _int_or_none(value)
    return None if number is None else max(0, number)


def _positive(value: object) -> int | None:
    number = _int_or_none(value)
    return number if number is not None and number > 0 else None


def clock(ms: int) -> str:
    """``m:ss`` (ou ``h:mm:ss`` a partir de uma hora) de uma posição em milissegundos."""
    seconds = max(0, ms) // 1000
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


def progress_text(position_ms: int | None, duration_ms: int | None, *, width: int = PROGRESS_WIDTH) -> str | None:
    """``▰▰▰▱▱▱▱▱▱▱ 1:23 / 3:45``; ``None`` sem duração (ao vivo) ou sem posição. A posição passa do fim? Barra cheia."""
    if position_ms is None or duration_ms is None or duration_ms <= 0:
        return None
    position = max(0, min(position_ms, duration_ms))
    filled = position * width // duration_ms
    return f"{'▰' * filled}{'▱' * (width - filled)} {clock(position)} / {clock(duration_ms)}"


def heal_wait_ms(last_ms: int | None, now_ms: int) -> int:
    """Quanto falta (em ms) para o painel apagado poder ser reenviado; 0 = já pode."""
    if last_ms is None or now_ms < last_ms:
        return 0
    return max(0, HEAL_MIN_INTERVAL_MS - (now_ms - last_ms))


class StrictAction(StrEnum):
    """O que a Jukebox estrita faz com uma mensagem escrita no canal de pedidos."""

    IGNORE = "ignore"  # não é da conta do canal (bot, comando): fica como está
    PROCESS = "process"  # o fluxo de sempre (pedido; o próprio fluxo apaga)
    DELETE = "delete"  # texto que não é pedido: apagado na hora, sem resposta
    DELETE_AND_PROCESS = "delete_and_process"  # pedido: apagado na hora e atendido (o nome digitado vale)


def strict_action(
    strict: bool, *, author_is_bot: bool, is_command: bool, has_mentions: bool, is_system: bool
) -> StrictAction:
    """Decisão pura da Jukebox estrita. Bot e comando nunca são tocados; sem o modo estrito nada muda (menção segue ignorada)."""
    if author_is_bot or is_command:
        return StrictAction.IGNORE
    if not strict:
        return StrictAction.IGNORE if has_mentions else StrictAction.PROCESS
    if is_system or has_mentions:
        return StrictAction.DELETE
    return StrictAction.DELETE_AND_PROCESS


_MENTION = re.compile(r"<(@!?|@&|#)(\d{1,20})>")


def plain_mentions(text: str, name_of: Callable[[int], str | None]) -> str:
    """Troca ``<@id>``, ``<@&id>`` e ``<#id>`` por texto simples (o rodapé do Discord não renderiza menção): o nome de quem o ``name_of``
    conhece, senão ``…``. Nunca devolve uma menção viva."""

    def replace(match: re.Match[str]) -> str:
        if match.group(1) not in ("@", "@!"):
            return "…"
        name = name_of(int(match.group(2)))
        return f"@{name}" if name else "…"

    return _MENTION.sub(replace, text)


def last_action_of(candidates: Sequence[tuple[bool, str | None]]) -> str:
    """O texto da última ação: o 1º de ``(call privada, texto)`` que não é de call privada e não está vazio (S-V7); vazio se nenhum."""
    for private, text in candidates:
        if not private and text:
            return text
    return ""


def choose_player(candidates: Sequence[tuple[int, int | None]], member_voice_id: int | None) -> int | None:
    """Id do bot cujo player está na call de quem apertou, ou ``None``.

    ``candidates``: ``(id do bot, id da call onde o player está)`` na ordem de prioridade. Quem não está em call, ou está numa call sem
    player, não tem player (nunca cai no de outra call).
    """
    if member_voice_id is None:
        return None
    return next((bot_id for bot_id, voice_id in candidates if voice_id == member_voice_id), None)


def refresh_wait_ms(last_ms: int | None, now_ms: int) -> int:
    """Quanto falta (em ms) para a próxima edição do painel poder sair; 0 = já pode."""
    if last_ms is None or now_ms < last_ms:
        return 0
    return max(0, PANEL_REFRESH_MS - (now_ms - last_ms))
