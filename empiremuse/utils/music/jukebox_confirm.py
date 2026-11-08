"""Confirmação de ``/jukebox setup`` num canal que já existe: configurar apaga as mensagens nele, exceto o painel atual da Jukebox, então o autor confirma antes (botão, só o autor, 60 s)."""

from __future__ import annotations

import asyncio
from typing import Any

import disnake

CONFIRM_TIMEOUT_S = 60


class ConfirmJukeboxPurge(disnake.ui.View):
    def __init__(self, author_id: int, tr: Any) -> None:
        super().__init__(timeout=CONFIRM_TIMEOUT_S)
        self.tr = tr
        self.confirm.label = tr("jukebox.setup.purge.confirm")
        self.cancel.label = tr("jukebox.setup.purge.cancel")
        self.author_id = author_id
        self.confirmed: bool | None = None

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        if interaction.author.id == self.author_id:
            return True
        await interaction.response.send_message(self.tr("jukebox.setup.purge.author_only"), ephemeral=True)
        return False

    @disnake.ui.button(style=disnake.ButtonStyle.danger, emoji="🧹")
    async def confirm(self, _button: disnake.ui.Button, interaction: disnake.MessageInteraction) -> None:
        self.confirmed = True
        await interaction.response.edit_message(content=self.tr("jukebox.setup.purge.confirmed"), view=None)
        self.stop()

    @disnake.ui.button(style=disnake.ButtonStyle.secondary, emoji="❌")
    async def cancel(self, _button: disnake.ui.Button, interaction: disnake.MessageInteraction) -> None:
        self.confirmed = False
        await interaction.response.edit_message(content=self.tr("jukebox.setup.purge.cancelled"), view=None)
        self.stop()


async def confirm_purge(inter: Any, target: Any) -> bool:
    """Pergunta ao autor se pode limpar ``target``; ``False`` em cancelar ou em 60 s sem resposta (nada é apagado)."""
    tr = await inter.bot.pool.i18n.for_guild(inter.guild)
    view = ConfirmJukeboxPurge(inter.author.id, tr)
    text = tr("jukebox.setup.purge.ask", user=inter.author.mention, channel=target.mention, seconds=CONFIRM_TIMEOUT_S)
    if hasattr(inter, "followup"):
        if not inter.response.is_done():  # a interação do select de canal ainda não foi reconhecida
            await inter.response.defer(ephemeral=True)
        message = await inter.followup.send(text, view=view, ephemeral=True, wait=True)
    else:
        message = await inter.send(text, view=view)
    try:
        await asyncio.wait_for(view.wait(), CONFIRM_TIMEOUT_S)
    except asyncio.TimeoutError:
        view.stop()
    if view.confirmed is None:  # tempo esgotado
        try:
            await message.edit(content=tr("jukebox.setup.purge.timeout"), view=None)
        except disnake.HTTPException:
            pass
    return bool(view.confirmed)
