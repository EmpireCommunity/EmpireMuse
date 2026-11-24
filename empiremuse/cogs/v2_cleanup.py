# -*- coding: utf-8 -*-
"""LIMPEZA TEMPORÁRIA (T-C-BUILD): cada bot apaga as PRÓPRIAS mensagens do spike V2 no #jukebox. Sai antes do PR.

Só apaga uma mensagem se as três condições valem: o autor é este bot, ela foi postada na janela do spike e o texto (conteúdo, embed
e componentes) tem uma marca do spike (texto sintético que nenhum player do bot gera). Rodar de novo não acha nada.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import re
from typing import TYPE_CHECKING, Any, Iterator

import disnake
from disnake.ext import commands

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore

logger = logging.getLogger(__name__)

CHANNEL_ID = 1567393608426848407
WINDOW_START = datetime.time(14, 50)
WINDOW_END = datetime.time(15, 5)
START_DELAY_S = 8
MARKS = re.compile(
    r"spike|Passo \d|Mensagem V1|vai virar V2|Interação|TOCANDO · Lounge|Disco do EmpireMuse|^fila x+|^## b$",
    re.I | re.M,
)


def texts_of(node: Any) -> Iterator[str]:
    """Os textos de um componente V2 (``TextDisplay``) e dos filhos dele."""
    content = getattr(node, "content", None)
    if isinstance(content, str):
        yield content
    for name in ("children", "components"):
        for child in getattr(node, name, None) or ():
            yield from texts_of(child)
    accessory = getattr(node, "accessory", None)
    if accessory is not None:
        description = getattr(accessory, "description", None)
        if isinstance(description, str):
            yield description


def message_text(message: disnake.Message) -> str:
    parts = [message.content or ""]
    for embed in message.embeds:
        parts += [embed.title or "", embed.description or ""]
    for component in message.components:
        parts.extend(texts_of(component))
    return "\n".join(parts)


def is_spike(message: disnake.Message, bot_id: int, window: tuple[datetime.datetime, datetime.datetime]) -> bool:
    return (
        message.author.id == bot_id
        and window[0] <= message.created_at <= window[1]
        and MARKS.search(message_text(message)) is not None
    )


def todays_window() -> tuple[datetime.datetime, datetime.datetime]:
    today = datetime.datetime.now().astimezone().date()
    zone = datetime.datetime.now().astimezone().tzinfo
    return (
        datetime.datetime.combine(today, WINDOW_START, tzinfo=zone),
        datetime.datetime.combine(today, WINDOW_END, tzinfo=zone),
    )


class V2Cleanup(commands.Cog):
    def __init__(self, bot: BotCore):
        self.bot = bot
        self._task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        self._task = self.bot.loop.create_task(self.run())

    def cog_unload(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def run(self) -> None:
        try:
            await self.bot.wait_until_ready()
            await asyncio.sleep(START_DELAY_S)
            channel = self.bot.get_channel(CHANNEL_ID)
            if channel is None:
                logger.info("v2cleanup: skip(canal não visível a este bot)")
                return
            window = todays_window()
            deleted = 0
            async for message in channel.history(limit=300, after=window[0], before=window[1], oldest_first=True):
                if not is_spike(message, self.bot.user.id, window):
                    continue
                try:
                    await message.delete()
                    deleted += 1
                except disnake.HTTPException as error:
                    logger.warning("v2cleanup: não apagou uma mensagem (HTTP %s)", error.status)
                await asyncio.sleep(0.4)
            logger.info("v2cleanup: done (%d apagadas)", deleted)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - limpeza temporária: só relata o tipo
            logger.warning("v2cleanup: falhou (%s)", type(error).__name__)


def setup(bot: BotCore) -> None:
    bot.add_cog(V2Cleanup(bot))
