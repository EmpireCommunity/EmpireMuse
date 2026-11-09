# -*- coding: utf-8 -*-
"""Sincroniza os emojis de aplicação de cada bot, uma vez por processo, no ``on_ready``.

Todos os bots do pool sincronizam, incluindo os só-rádio. Falha de sync usa a reserva Unicode.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from disnake.ext import commands

from empiremuse.utils.music.app_emojis import sync_app_emojis

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore

logger = logging.getLogger(__name__)
boot_logger = logging.getLogger("empiremuse.boot")

EMOJIS_DIR = Path(__file__).resolve().parents[2] / "assets" / "emojis"


class AppEmojis(commands.Cog):
    def __init__(self, bot: BotCore):
        self.bot = bot
        if not hasattr(bot, "app_emojis"):
            bot.app_emojis = {}
        self._task: asyncio.Task | None = None
        self._unloaded = False

    def _start(self):
        # a flag fica no bot: recarregar a cog e reconectar não refaz o sync
        if getattr(self.bot, "_app_emojis_started", False):
            return

        self.bot._app_emojis_started = True

        self._task = self.bot.loop.create_task(self._sync())

    def cog_load(self):
        # as cogs carregam depois do primeiro on_ready; com o bot pronto, o sync sobe aqui
        if self.bot.is_ready():
            self._start()

    def cog_unload(self):
        self._unloaded = True

        if self._task is not None and not self._task.done():
            self._task.cancel()
            # sync interrompido não conta como feito: a cog recarregada tenta de novo
            self.bot._app_emojis_started = False

    @commands.Cog.listener("on_ready")
    async def on_ready(self):
        self._start()

    async def _sync(self):
        try:
            mapping = await sync_app_emojis(self.bot, emojis_dir=EMOJIS_DIR)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("Falha ao sincronizar os emojis de aplicação (%s)", type(error).__name__)
            return

        if self._unloaded:
            return

        self.bot.app_emojis = dict(mapping)
        boot_logger.info("Emojis de aplicação: %d prontos", len(mapping))


def setup(bot: BotCore):
    bot.add_cog(AppEmojis(bot))
