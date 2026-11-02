"""Confirmação por botão, só de quem pediu, com prazo (``MENU_IDLE_S``): usada pelos "redefinir" (Jukebox e rádio)."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

import disnake

from empiremuse.rules.i18n import fit
from empiremuse.utils.ephemeral import CONFIRMATION_TTL_S, MENU_IDLE_S, cleanup_later


class ConfirmView(disnake.ui.View):
    def __init__(
        self,
        tr: Any,
        author_id: int,
        run: Callable[[disnake.MessageInteraction], Awaitable[None]],
        *,
        confirm_key: str,
        cancel_key: str,
        cancelled_key: str,
        not_yours_key: str,
    ) -> None:
        super().__init__(timeout=MENU_IDLE_S)
        self.tr, self.author_id, self.run = tr, author_id, run
        self.cancelled_key, self.not_yours_key = cancelled_key, not_yours_key
        confirm = disnake.ui.Button(label=fit(tr(confirm_key), 80), style=disnake.ButtonStyle.danger)
        cancel = disnake.ui.Button(label=fit(tr(cancel_key), 80))
        confirm.callback, cancel.callback = self._confirmed, self._cancelled
        self.add_item(confirm)
        self.add_item(cancel)
        self.message_interaction: Any = None  # a resposta que carrega a pergunta: some no prazo se ninguém responder

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        if interaction.author.id != self.author_id:
            await interaction.response.send_message(self.tr(self.not_yours_key), ephemeral=True)
            return False
        return True

    async def _cancelled(self, interaction: disnake.MessageInteraction) -> None:
        self.stop()
        await interaction.response.edit_message(content=self.tr(self.cancelled_key), view=None, embed=None)
        cleanup_later(interaction, CONFIRMATION_TTL_S)

    async def _confirmed(self, interaction: disnake.MessageInteraction) -> None:
        self.stop()
        await self.run(interaction)

    async def on_timeout(self) -> None:
        self.stop()
        if self.message_interaction is not None:
            cleanup_later(self.message_interaction, 0)
