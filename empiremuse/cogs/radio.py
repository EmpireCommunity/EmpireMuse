"""Rádio 24/7 (item 8, R1): os comandos de dono. ``/radio ligar|desligar|status`` entram nesta mesma classe.

R-1: premium, bots só-rádio e "pausar todas" são só de quem está em ``OWNER_IDS`` (``bot.env_owner_ids``). Nada de ``is_owner()``, porque ele
também aceita o dono e a equipe da aplicação; nem ``manage_guild`` nem administrador do servidor valem. Lista vazia = ninguém. Como todo bot
carrega este cog, os comandos de dono só rodam no principal (senão cada bot responderia uma vez).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from disnake.ext import commands

from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.others import CustomContext
from empiremuse.utils.radio.control import MainBotRefused
from empiremuse.utils.radio.premium import RadioUnavailable

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore

logger = logging.getLogger(__name__)

GRANT = ("conceder", "grant", "dar")
REVOKE = ("revogar", "revoke", "tirar")
LIST = ("listar", "list")
ADD = ("adicionar", "add")
REMOVE = ("remover", "remove")


def is_env_owner(bot: BotCore, user_id: int) -> bool:
    """O usuário está em ``OWNER_IDS``? Só isso: a equipe e o dono da aplicação do Discord NÃO contam."""
    return user_id in bot.env_owner_ids


def _guild_id(raw: str) -> int | None:
    return int(raw) if raw.isdecimal() and 0 < int(raw) < 1 << 64 else None


class Radio(commands.Cog):
    def __init__(self, bot: BotCore) -> None:
        self.bot = bot

    @property
    def hub(self):
        return self.bot.pool.radio

    async def _tr(self, ctx: CustomContext):
        return await self.bot.pool.i18n.for_guild(ctx.guild)

    def _only_main(self) -> bool:
        return self.bot.pool.servers.main_bot() is self.bot

    async def cog_check(self, ctx: CustomContext) -> bool:  # type: ignore[override]
        # só os comandos de dono vivem aqui como prefixo; a conferência vale para TODOS eles
        return self._only_main() and is_env_owner(self.bot, ctx.author.id)

    # ------------------------------------------------------------------ premium

    @commands.command(name="radio-premium", hidden=True)
    async def radio_premium(self, ctx: CustomContext, action: str = "", guild: str = ""):
        """``radio-premium conceder|revogar <id do servidor>`` e ``radio-premium listar``."""
        tr = await self._tr(ctx)
        action = action.casefold()
        try:
            if action in LIST:
                lines = [f"`{p.guild_id}` — {p.active}/3" for p in await self.hub.premium.all()]
                await ctx.send(tr("radio.owner.premium_list", lines="\n".join(lines) or tr("radio.owner.none")))
                return
            guild_id = _guild_id(guild)
            if action not in GRANT + REVOKE or guild_id is None:
                raise GenericError(tr("radio.owner.premium_usage"))
            if action in GRANT:
                await self.hub.premium.grant(guild_id, by=ctx.author.id)
                logger.info("Premium de rádio concedido")
                await ctx.send(tr("radio.owner.premium_granted", guild=guild_id))
            else:
                off = await self.hub.store.revoke_premium(guild_id, updated_by=str(ctx.author.id))
                logger.info("Premium de rádio revogado (%d rádios desligadas)", off)
                await ctx.send(tr("radio.owner.premium_revoked", guild=guild_id, count=off))
        except RadioUnavailable:
            raise GenericError(tr("radio.owner.unavailable"))

    # ------------------------------------------------------------------ pausar todas

    @commands.command(name="radio-pausar-todas", hidden=True)
    async def radio_pause_all(self, ctx: CustomContext):
        tr = await self._tr(ctx)
        try:
            changed = await self.hub.control.set_paused_all(True, by=ctx.author.id)
        except RadioUnavailable:
            raise GenericError(tr("radio.owner.unavailable"))
        logger.info("Rádios: pausa geral %s", "ligada" if changed else "já estava ligada")
        await ctx.send(tr("radio.owner.paused_all" if changed else "radio.owner.paused_all_already"))

    @commands.command(name="radio-retomar-todas", hidden=True)
    async def radio_resume_all(self, ctx: CustomContext):
        tr = await self._tr(ctx)
        try:
            changed = await self.hub.control.set_paused_all(False, by=ctx.author.id)
        except RadioUnavailable:
            raise GenericError(tr("radio.owner.unavailable"))
        logger.info("Rádios: pausa geral %s", "desligada" if changed else "já estava desligada")
        await ctx.send(tr("radio.owner.resumed_all" if changed else "radio.owner.resumed_all_already"))

    # ------------------------------------------------------------------ bots só-rádio

    @commands.command(name="radio-bot", hidden=True)
    async def radio_bot(self, ctx: CustomContext, action: str = "", identifier: str = ""):
        """``radio-bot adicionar|remover <TOKEN_2>`` e ``radio-bot listar``: os bots reservas dedicados a rádio."""
        tr = await self._tr(ctx)
        action = action.casefold()
        try:
            if action in LIST:
                await self.hub.roles.load()
                bots = ", ".join(f"`{i}`" for i in self.hub.roles.radio_only) or tr("radio.owner.none")
                await ctx.send(tr("radio.owner.bots_list", bots=bots))
                return
            if action not in ADD + REMOVE or not identifier.isascii() or not 1 <= len(identifier) <= 40:
                raise GenericError(tr("radio.owner.bots_usage"))
            if action in ADD:
                await self.hub.roles.add(identifier)
            else:
                await self.hub.roles.remove(identifier)
            await ctx.send(tr("radio.owner.bots_changed", bot=identifier))
        except MainBotRefused:
            raise GenericError(tr("radio.owner.main_refused"))
        except RadioUnavailable:
            raise GenericError(tr("radio.owner.unavailable"))


def setup(bot: BotCore) -> None:
    bot.add_cog(Radio(bot))
