# -*- coding: utf-8 -*-
"""Registro de uso: desfecho dos comandos de barra, de menu de contexto e de prefixo, e erros de botão/select do player.

O evento do comando é aberto no gancho do ``COMMAND_LOG`` (``BotPool.setup``); aqui só chega o desfecho (``ok`` ou o rótulo fixo do
erro, a latência e o bot que executou) pelos eventos que o disnake já despacha. O ``UsageRecorder`` do pool só põe no buffer, então
nada aqui atrasa nem quebra um comando. Os botões que rodam um comando do player são registrados em ``process_player_interaction``,
a Jukebox em ``jukebox_message`` e o painel web nos tratadores de POST.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from disnake.ext import commands

from empiremuse.rules.usage import KIND_BUTTON
from empiremuse.utils.music.errors import PoolException
from empiremuse.utils.usage import component_name, label_for, usage_of

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore


class UsageTracker(commands.Cog):
    def __init__(self, bot: BotCore):
        self.bot = bot

    def _finish(self, source: object, error: BaseException | None = None) -> None:
        if usage := usage_of(self.bot):
            usage.command_finished(source, error, bot=self.bot, music_bot=getattr(source, "music_bot", None))

    @commands.Cog.listener("on_slash_command_completion")
    @commands.Cog.listener("on_user_command_completion")
    @commands.Cog.listener("on_message_command_completion")
    @commands.Cog.listener("on_command_completion")
    async def command_done(self, source):
        self._finish(source)

    @commands.Cog.listener("on_slash_command_error")
    @commands.Cog.listener("on_user_command_error")
    @commands.Cog.listener("on_message_command_error")
    @commands.Cog.listener("on_command_error")
    async def command_failed(self, source, error: Exception):
        # Inclui o ``PoolException`` (ninguém do pool pôde atender): o evento aberto fecha como ``rejected`` em vez de ficar sem desfecho.
        self._finish(source, error)

    @commands.Cog.listener("on_interaction_player_error")
    async def player_component_failed(self, inter, error: Exception):
        if (usage := usage_of(self.bot)) is not None and not isinstance(error, PoolException):
            usage.record_command(
                KIND_BUTTON, component_name(inter), inter, bot=self.bot, ok=False, error=label_for(error)
            )


def setup(bot: BotCore):
    bot.add_cog(UsageTracker(bot))
