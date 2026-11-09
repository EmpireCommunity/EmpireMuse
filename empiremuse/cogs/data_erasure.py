"""``/apagar_meus_dados`` (``/delete_my_data``): o membro apaga os próprios dados com UM clique de confirmação (§3.2 do contrato).

As views, o ``run_erasure`` e as regras de segurança do fluxo moram em ``empiremuse/utils/erasure_views.py`` (também usadas pelo comando
do dono em ``legacy_cmds.py``). Aqui ficam só o comando e a resposta a botões da DM do titular cujo pedido já expirou.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import disnake
from disnake.ext import commands

from empiremuse.i18n import localized
from empiremuse.utils.erasure_views import GRANT_PREFIX, ErasureConfirmView, translator_for

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore

logger = logging.getLogger(__name__)

ERASE_COMMAND_COOLDOWN = commands.CooldownMapping.from_cooldown(1, 15, commands.BucketType.user)


def view_claims(bot: BotCore, interaction: disnake.MessageInteraction) -> bool:
    """Alguma view viva reivindica este botão? Mesma busca do ``ViewStore.dispatch`` (que roda ANTES do evento ``message_interaction``)."""
    store = bot._connection._view_store  # noqa: SLF001 - não há consulta pública; só leitura
    component_type = int(interaction.data.component_type.value)
    custom_id = interaction.data.custom_id
    message_id = interaction.message.id if interaction.message else None
    entry = store._views.get((component_type, message_id, custom_id)) or store._views.get(
        (component_type, None, custom_id)
    )
    # ``ViewStore.remove_view`` deixa entradas de views paradas até o próximo ``add_view``/``dispatch``: view acabada não reivindica.
    return entry is not None and not entry[0].is_finished()


class DataErasure(commands.Cog):
    def __init__(self, bot: BotCore) -> None:
        self.bot = bot

    @commands.slash_command(
        name=localized("cmd.delete_my_data.name"),
        description=localized("cmd.delete_my_data.description"),
        extras={"allow_private": True},
        cooldown=ERASE_COMMAND_COOLDOWN,
    )
    @commands.contexts(guild=True, bot_dm=True)
    async def erase_my_data(self, inter: disnake.ApplicationCommandInteraction) -> None:
        # Resposta efêmera sem consulta lenta: a varredura só roda no clique de confirmação. Sem comando de prefixo (a mensagem seria pública).
        tr = translator_for(self.bot.pool, inter.locale)
        view = ErasureConfirmView(self.bot, tr, inter.author.id, origin=inter)
        await inter.response.send_message(tr("erasure.ask"), view=view, ephemeral=True)

    @commands.Cog.listener("on_message_interaction")
    async def expired_grant_button(self, interaction: disnake.MessageInteraction) -> None:
        """Botão da DM do titular que nenhuma view viva reivindica (reinício, view expirada, concessão já usada): responde "expirou"."""
        custom_id = getattr(interaction.data, "custom_id", "")
        if not custom_id.startswith(GRANT_PREFIX) or view_claims(self.bot, interaction):
            return
        tr = translator_for(self.bot.pool, interaction.locale)
        try:
            await interaction.response.edit_message(content=tr("erasure.grant_expired"), view=None)
        except disnake.HTTPException as error:
            logger.warning("apagamento: resposta de concessão expirada falhou (%s)", type(error).__name__)


def setup(bot: BotCore) -> None:
    bot.add_cog(DataErasure(bot))
