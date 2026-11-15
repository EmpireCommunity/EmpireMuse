"""``hardreset`` (só prefixo, só dono): apaga o banco do bot inteiro menos o premium, depois de UMA confirmação (botão, só quem pediu, 60 s)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import disnake
from disnake.ext import commands

from empiremuse.utils.owner_gate import admin_owner_only
from empiremuse.rules.i18n import fit
from empiremuse.utils.hardreset import hard_reset
from empiremuse.utils.music.errors import PoolException

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore, CustomContext

logger = logging.getLogger(__name__)

CONFIRM_TIMEOUT_S = 60.0


class ConfirmHardReset(disnake.ui.View):
    def __init__(self, bot: BotCore, tr, author_id: int) -> None:
        super().__init__(timeout=CONFIRM_TIMEOUT_S)
        self.bot, self.tr, self.author_id = bot, tr, author_id
        confirm = disnake.ui.Button(
            label=fit(tr("hardreset.confirm"), 80), style=disnake.ButtonStyle.danger, custom_id="hardreset_confirm"
        )
        cancel = disnake.ui.Button(label=fit(tr("hardreset.cancel"), 80), custom_id="hardreset_cancel")
        confirm.callback, cancel.callback = self.confirmed, self.cancelled
        self.add_item(confirm)
        self.add_item(cancel)

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        if interaction.author.id != self.author_id:
            await interaction.response.send_message(self.tr("hardreset.not_yours"), ephemeral=True)
            return False
        return True

    async def cancelled(self, interaction: disnake.MessageInteraction) -> None:
        self.stop()
        await interaction.response.edit_message(content=self.tr("hardreset.cancelled"), view=None)

    async def confirmed(self, interaction: disnake.MessageInteraction) -> None:
        self.stop()
        await interaction.response.edit_message(content=self.tr("hardreset.running"), view=None)
        try:
            report = await hard_reset(self.bot.pool, author_id=interaction.author.id)
        except Exception as error:  # noqa: BLE001 - só o tipo vai ao log e ao dono
            logger.error("hardreset falhou (%s)", type(error).__name__)
            await interaction.edit_original_message(content=self.tr("hardreset.failed"))
            return
        await interaction.edit_original_message(
            content=self.tr(
                "hardreset.done", tables=report.tables, documents=report.documents, files=report.session_files
            )
        )

    async def on_timeout(self) -> None:
        self.stop()


class HardReset(commands.Cog):
    def __init__(self, bot: BotCore) -> None:
        self.bot = bot

    async def cog_check(self, ctx: CustomContext) -> bool:
        """Todo bot carrega este cog: só o principal responde (senão cada um pediria a confirmação). Os reservas saem em silêncio
        (``PoolException`` o tratador de erros ignora): um ``False`` viraria ``CheckFailure`` e seria registrado como erro inesperado."""
        if self.bot.pool.servers.main_bot() is not self.bot:
            raise PoolException("hardreset: só o bot principal responde")
        return True

    @admin_owner_only()
    @commands.command(name="hardreset", hidden=True, description="Apaga o banco do bot, menos o premium (só dono).")
    async def hardreset(self, ctx: CustomContext) -> None:
        tr = await self.bot.pool.i18n.for_guild(ctx.guild)
        await ctx.send(
            tr("hardreset.ask"),
            view=ConfirmHardReset(self.bot, tr, ctx.author.id),
            allowed_mentions=disnake.AllowedMentions.none(),
        )


def setup(bot: BotCore) -> None:
    bot.add_cog(HardReset(bot))
