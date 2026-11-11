# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import disnake
from disnake.ext import commands

from empiremuse.utils.owner_gate import is_admin_owner
from empiremuse.utils.music.errors import error_code_text, log_unexpected_error, parse_error

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore

logger = logging.getLogger(__name__)


def panel_command(*args, **kwargs) -> PanelCommand:
    return commands.command(*args, **kwargs, cls=PanelCommand)


class PanelCommand(commands.Command):
    def __init__(self, func, **kwargs):
        self.emoji = kwargs.pop("emoji")
        super().__init__(func, **kwargs)
        self.hidden = kwargs.pop("hidden", True)
        self.alt_name = kwargs.pop("alt_name", self.name)


class PanelView(disnake.ui.View):
    def __init__(self, bot: BotCore):
        super().__init__(timeout=None)
        self.bot = bot

        opts = []

        for cmd in self.bot.commands:
            if not isinstance(cmd, PanelCommand):
                continue

            opts.append(
                disnake.SelectOption(label=cmd.alt_name, description=cmd.description, value=cmd.name, emoji=cmd.emoji)
            )

        select = disnake.ui.Select(placeholder="Selecione uma tarefa:", options=opts, custom_id="onwer_panel_dropdown")

        select.callback = self.opts_callback

        self.add_item(select)

    async def opts_callback(self, interaction: disnake.MessageInteraction):
        txt = await self.bot.get_command(interaction.data.values[0])(interaction)

        if interaction.response.is_done():
            edit = (await interaction.original_message()).edit
        else:
            edit = interaction.response.edit_message
        interaction.message.embeds[0].description = txt or "Comando executado com sucesso!"
        await edit(embed=interaction.message.embeds[0], view=self)

    async def interaction_check(self, interaction: disnake.MessageInteraction):
        if not is_admin_owner(self.bot, interaction.user):
            await interaction.send("Apenas meu(s) desenvolvedor(es) pode(m) usar essas opções.", ephemeral=True)
            return

        return True

    async def on_error(self, error: Exception, item: disnake.ui.Item, interaction: disnake.MessageInteraction):
        error_text = parse_error(interaction, error, no_log=True)[0]

        if not error_text:
            code = log_unexpected_error(logger, error, "painel do dono")
            error_text = f"**Ocorreu um erro inesperado.**{error_code_text(code)}"

        interaction.message.embeds[0].description = error_text

        if not interaction.response.is_done():
            edit = interaction.response.edit_message
        else:
            edit = (await interaction.original_message()).edit
        await edit(embed=interaction.message.embeds[0], view=self)
