# -*- coding: utf-8 -*-
"""Canal de pedidos único (item 11, P4): a seção ``request`` do documento único e a eleição do bot do painel.

Um canal por servidor para todo o pool. O bot do painel (o principal se está no servidor, senão a 1ª reserva presente) é o ÚNICO que envia,
edita e trata as mensagens do canal. A eleição é decidida DENTRO da trava do servidor, conferindo de novo quem está no servidor (S-V11). Tudo
passa pelo store (``utils/servers``): trava por servidor, ``rev`` +1 e compare-and-set; o painel web e os comandos gravam o mesmo documento.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from typing import TYPE_CHECKING, NamedTuple, Optional

from empiremuse.rules.request_panel import PanelPeer, elect_panel_bot
from empiremuse.rules.servers import SECTION_REQUEST
from empiremuse.utils.servers import ServerConfigConflict

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore
    from empiremuse.utils.servers import ServerStore

logger = logging.getLogger(__name__)

SECTIONS = (SECTION_REQUEST,)


class RequestConfig(NamedTuple):
    """O canal de pedidos do servidor. ``panel_bot_id`` é o último bot eleito (``None`` = nunca houve canal)."""

    channel: Optional[int]
    message_id: Optional[int]
    panel_bot_id: Optional[int]
    rev: int

    @property
    def current(self) -> tuple[Optional[int], Optional[int]]:
        """``(canal, mensagem)``: o que o escritor leu, para o ``expected`` do ``set``/``clear`` (conflito = mudou no meio)."""
        return self.channel, self.message_id


class Election(NamedTuple):
    """Resultado da eleição: o bot do painel agora e se ele MUDOU em relação ao último gravado (quem muda reenvia o painel)."""

    bot: Optional["BotCore"]
    changed: bool


def _as_id(value: object) -> Optional[int]:
    try:
        number = int(value)  # type: ignore[call-overload]
    except TypeError, ValueError:
        return None
    return number if number > 0 else None


def _config(doc: dict) -> RequestConfig:
    request = doc.get(SECTION_REQUEST) or {}
    return RequestConfig(
        _as_id(request.get("channel")),
        _as_id(request.get("message_id")),
        _as_id(request.get("panel_bot_id")),
        int(doc.get("rev", 0)),
    )


class ServerRequest:
    def __init__(self, store: ServerStore) -> None:
        self.store = store
        self._operation_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()

    def operation(self, gid: int) -> asyncio.Lock:
        """A trava de OPERAÇÃO do canal do servidor: ``/setup`` e ``/reset`` (Discord e painel web) correm um de cada vez.

        É diferente da trava do store: ela cobre os efeitos no Discord (criar, apagar, arquivar), que o compare-and-set não desfaz.
        """
        lock = self._operation_locks.get(gid)
        if lock is None:
            lock = self._operation_locks[gid] = asyncio.Lock()
        return lock

    # ------------------------------------------------------------------ leitura

    async def get(self, gid: int) -> RequestConfig:
        return _config(await self.store.get_sections(gid, *SECTIONS))

    # ------------------------------------------------------------------ escrita

    async def set(
        self,
        gid: int,
        channel_id: Optional[int],
        message_id: Optional[int],
        *,
        expected_rev: Optional[int] = None,
        expected: Optional[tuple[Optional[int], Optional[int]]] = None,
    ) -> RequestConfig:
        """Grava canal e mensagem juntos (``None`` limpa) sob a trava; sem canal, sem mensagem.

        ``expected`` = ``(canal, mensagem)`` que o escritor leu: se o gravado já é outro, ``ServerConfigConflict`` (conferido DENTRO da
        trava). Vale mais que ``expected_rev``, que também sobe com qualquer outra configuração do servidor.
        """
        channel = str(channel_id) if channel_id else None
        message = str(message_id) if channel and message_id else None

        def apply(doc: dict) -> bool:
            if expected is not None and (_config(doc).channel, _config(doc).message_id) != tuple(expected):
                raise ServerConfigConflict
            before = dict(doc[SECTION_REQUEST])
            doc[SECTION_REQUEST].update({"channel": channel, "message_id": message})
            return doc[SECTION_REQUEST] != before

        return _config(await self.store.mutate_doc(gid, SECTIONS, apply, expected_rev=expected_rev))

    async def clear(
        self,
        gid: int,
        *,
        expected_rev: Optional[int] = None,
        expected: Optional[tuple[Optional[int], Optional[int]]] = None,
    ) -> RequestConfig:
        return await self.set(gid, None, None, expected_rev=expected_rev, expected=expected)

    # ------------------------------------------------------------------ o bot do painel

    def peers(self, gid: int) -> list[PanelPeer]:
        """Os bots do pool que estão no servidor agora (bot exclusivo de OUTRO servidor não conta)."""
        found = []
        for bot in self.store.pool.get_all_bots():
            exclusive = getattr(bot, "exclusive_guild_id", None)
            if bot.user is None or bot.get_guild(gid) is None or (exclusive and exclusive != gid):
                continue
            if self._radio_only(bot):
                continue  # item 8: o bot só-rádio nunca vira bot do painel da Jukebox
            found.append(PanelPeer(bot.identifier, bot.user.id, bool(exclusive)))
        return found

    def _radio_only(self, bot: BotCore) -> bool:
        roles = getattr(getattr(self.store.pool, "radio", None), "roles", None)
        return bool(roles is not None and roles.is_radio_only(getattr(bot, "identifier", None)))

    def _bot_by_id(self, bot_id: Optional[int]) -> Optional[BotCore]:
        return next((b for b in self.store.pool.get_all_bots() if b.user is not None and b.user.id == bot_id), None)

    def elected_id(self, gid: int, stored_id: Optional[int] = None) -> Optional[int]:
        """Quem seria o bot do painel olhando só quem está no servidor agora (sem banco).

        Enquanto o último bot do painel gravado ainda está subindo (não ficou pronto), ele continua valendo: no boot uma reserva fica pronta
        antes do principal ter o servidor no cache e não pode tomar o painel, nem ficar gravada como bot do painel por isso.
        """
        stored = self._bot_by_id(stored_id)
        if stored is not None and not getattr(stored, "bot_ready", True) and stored.get_guild(gid) is None:
            return stored_id
        return elect_panel_bot(self.peers(gid))

    async def elect(self, gid: int) -> Election:
        """Elege o bot do painel DENTRO da trava (conferindo de novo quem está no servidor) e grava a mudança."""
        outcome: dict = {}

        def apply(doc: dict) -> bool:
            stored = doc[SECTION_REQUEST].get("panel_bot_id")
            # conferido aqui dentro: o que valeu fora da trava pode ter mudado
            elected = self.elected_id(gid, _as_id(stored))
            new = None if elected is None else str(elected)
            outcome["elected"] = elected
            outcome["changed"] = stored != new
            if stored == new:
                return False
            doc[SECTION_REQUEST]["panel_bot_id"] = new
            return True

        await self.store.mutate_doc(gid, SECTIONS, apply)
        election = Election(self._bot_by_id(outcome["elected"]), bool(outcome["changed"]))
        if election.changed and election.bot is not None:
            # toda troca gravada passa o painel ao novo bot, qualquer que seja o caminho que a gravou (panel_bot, is_panel_bot, entrada/saída)
            panel = getattr(self.store.pool, "request_panel", None)
            if panel is not None:
                panel.schedule_handoff(gid)
        return election

    async def panel_bot(self, gid: int) -> Optional[BotCore]:
        """O bot do painel do servidor (grava a eleição se mudou). ``None`` se nenhum bot do pool está no servidor."""
        if not self.peers(gid):
            return None  # nenhum bot do pool vê o servidor: nada a eleger, e o banco nem é tocado
        config = await self.get(gid)
        elected = self.elected_id(gid, config.panel_bot_id)
        if elected is None:
            return None
        if config.panel_bot_id != elected:
            return (await self.elect(gid)).bot
        self._retry_handoff(gid)
        return self._bot_by_id(elected)

    async def is_panel_bot(self, gid: int, bot: BotCore) -> bool:
        """``bot`` é o bot do painel? Quem não foi eleito pela presença responde sem tocar no banco."""
        if bot.user is None or all(p.bot_id != bot.user.id for p in self.peers(gid)):
            return False
        config = await self.get(gid)
        if self.elected_id(gid, config.panel_bot_id) != bot.user.id:
            return False
        if config.panel_bot_id != bot.user.id:
            await self.elect(gid)
        else:
            self._retry_handoff(gid)
        return True

    def _retry_handoff(self, gid: int) -> None:
        """A eleição já está gravada: se a passagem do painel ficou pendente (falhou antes), tenta de novo."""
        panel = getattr(self.store.pool, "request_panel", None)
        if panel is not None:
            panel.retry_pending_handoff(gid)
