"""``/apagar_meus_dados`` (``/delete_my_data``): o membro apaga os próprios dados com UM clique de confirmação (§3.2 do contrato).

As views, o ``run_erasure`` e as regras de segurança do fluxo moram em ``empiremuse/utils/erasure_views.py`` (também usadas pelo comando
do dono em ``legacy_cmds.py``). Aqui fica só o comando. Depois de um reinício, o botão da DM do titular falha com o aviso genérico do
Discord (a concessão mora em memória) e o dono reemite; os textos já dizem que o pedido vale "por até 24 h".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import disnake
from disnake.ext import commands

from empiremuse.i18n import localized
from empiremuse.utils.erasure_views import ErasureConfirmView, translator_for

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore

ERASE_COMMAND_COOLDOWN = commands.CooldownMapping.from_cooldown(1, 15, commands.BucketType.user)


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


def setup(bot: BotCore) -> None:
    bot.add_cog(DataErasure(bot))
