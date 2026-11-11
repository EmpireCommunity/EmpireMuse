# -*- coding: utf-8 -*-
"""Comandos do dono para banir e desbanir usuário (E5, MJ-E4, contrato §7).

* ``<prefixo>banuser <id> [motivo]`` e ``<prefixo>unbanuser <id>``: SÓ no privado do dono com o bot, SÓ para ``env_owner_ids`` (nunca
  ``is_owner``), SÓ no bot principal (a reserva ignora). Listener de ``on_message`` no molde de ``erasure_request_dm`` (o ``on_message`` do
  bot retorna sem servidor, então o comando de prefixo não chega aqui).
* A entrada ``userbans`` do painel do dono abre uma view e um modal próprios, que conferem ``env_owner_ids``.
* O portão do banimento (``utils/user_ban_gate.py``, ``on_message``, login, RPC, scrobble) NÃO passa por este cog.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Union

import disnake
from disnake.ext import commands

from empiremuse.utils.owner_gate import admin_owner_only
from empiremuse.rules.user_ban import parse_ban_command
from empiremuse.utils.owner_panel import panel_command
from empiremuse.utils.user_ban_views import UserBanPanelView, is_env_owner, prepare_confirmation

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore

logger = logging.getLogger(__name__)


class UserBansCog(commands.Cog, name="UserBans"):
    def __init__(self, bot: BotCore) -> None:
        self.bot = bot

    @commands.Cog.listener("on_message")
    async def owner_ban_dm(self, message: disnake.Message) -> None:
        if not isinstance(message.channel, disnake.DMChannel) or not is_env_owner(self.bot, message.author.id):
            return  # primeira linha: qualquer outra coisa é ignorada em silêncio, sem resposta a ninguém
        if self.bot.is_reserve:
            return  # a DM do dono pode chegar a qualquer bot do pool; só o principal (ou um bot único) atende
        command = parse_ban_command(message.content, self.bot.default_prefix)
        if command is None:
            return
        tr = self.bot.pool.i18n.for_locale(None)
        prepared = await prepare_confirmation(
            self.bot, tr, message.author.id, command.action, command.target, command.reason
        )
        sent = await message.channel.send(
            content=prepared.content,
            embed=prepared.embed,
            view=prepared.view,
            allowed_mentions=disnake.AllowedMentions.none(),
        )
        if prepared.view is not None:
            prepared.view.sent_message = sent  # type: ignore[attr-defined]

    @admin_owner_only()
    @panel_command(description="Banir ou desbanir um usuário.", emoji="🔨", alt_name="Banir/desbanir usuário")
    async def userbans(self, ctx: Union[disnake.MessageInteraction, commands.Context]):
        """Entrada do painel do dono. Só ``env_owner_ids`` (``is_owner`` também chega aqui, pela equipe da aplicação: não basta)."""
        if not isinstance(ctx, disnake.MessageInteraction):
            return None  # por prefixo não faz nada: o painel é o único caminho (o comando é oculto)
        tr = self.bot.pool.i18n.for_locale(self.bot.pool.i18n.resolve(None, ctx.locale))
        if not is_env_owner(self.bot, ctx.author.id):
            return tr("erasure.not_yours")
        # O ``PanelView`` edita a mensagem original depois do comando: reconhece antes (``defer``) e responde ao dono por mensagem à parte.
        await ctx.response.defer()
        await ctx.followup.send(
            content=tr("erasure.ban.panel.prompt"), view=UserBanPanelView(self.bot, tr, ctx.author.id), ephemeral=True
        )
        return tr("erasure.ban.panel.opened")


def setup(bot: BotCore) -> None:
    bot.add_cog(UserBansCog(bot))
