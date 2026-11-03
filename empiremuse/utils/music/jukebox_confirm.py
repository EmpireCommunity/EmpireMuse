"""Confirmação de ``/jukebox setup`` num canal que já existe: configurar apaga as mensagens do bot nele, então o autor confirma antes (botão, só o autor, 60 s)."""

from __future__ import annotations

import asyncio
from typing import Any

import disnake

CONFIRM_TIMEOUT_S = 60


class ConfirmJukeboxPurge(disnake.ui.View):
    def __init__(self, author_id: int) -> None:
        super().__init__(timeout=CONFIRM_TIMEOUT_S)
        self.author_id = author_id
        self.confirmed: bool | None = None

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        if interaction.author.id == self.author_id:
            return True
        await interaction.response.send_message("Só quem usou o comando pode confirmar.", ephemeral=True)
        return False

    @disnake.ui.button(label="Confirmar", style=disnake.ButtonStyle.danger, emoji="🧹")
    async def confirm(self, _button: disnake.ui.Button, interaction: disnake.MessageInteraction) -> None:
        self.confirmed = True
        await interaction.response.edit_message(content="Confirmado, configurando a Jukebox...", view=None)
        self.stop()

    @disnake.ui.button(label="Cancelar", style=disnake.ButtonStyle.secondary, emoji="❌")
    async def cancel(self, _button: disnake.ui.Button, interaction: disnake.MessageInteraction) -> None:
        self.confirmed = False
        await interaction.response.edit_message(content="Operação cancelada.", view=None)
        self.stop()


async def confirm_purge(inter: Any, target: Any) -> bool:
    """Pergunta ao autor se pode limpar ``target``; ``False`` em cancelar ou em 60 s sem resposta (nada é apagado)."""
    view = ConfirmJukeboxPurge(inter.author.id)
    text = (
        f"**{inter.author.mention}, configurar a Jukebox em {target.mention} apaga as mensagens do bot nesse canal. "
        f"Confirme em {CONFIRM_TIMEOUT_S}s.**"
    )
    if hasattr(inter, "followup"):
        message = await inter.followup.send(text, view=view, ephemeral=True, wait=True)
    else:
        message = await inter.send(text, view=view)
    try:
        await asyncio.wait_for(view.wait(), CONFIRM_TIMEOUT_S)
    except asyncio.TimeoutError:
        view.stop()
    if view.confirmed is None:  # tempo esgotado
        try:
            await message.edit(content="Tempo esgotado: nada foi alterado.", view=None)
        except disnake.HTTPException:
            pass
    return bool(view.confirmed)
