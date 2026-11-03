"""O prefixo ``radio`` (aliases ``rd`` e ``247``) e os comandos de dono (item 8, contrato-nomes-radios §2): status, on, off, reset e setup para quem gerencia o
servidor, e premium, bot, pauseall e resumeall (``pausartodas`` e ``retomartodas``) só para ``OWNER_IDS``.

Mixin do cog ``Radio`` (``cogs/radio.py``): os subcomandos usam o mesmo ``_turn_on``, ``_switch_off`` e ``_status_text`` do slash, então a regra é uma só.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional, Union

import disnake
from disnake.ext import commands

from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.others import CustomContext
from empiremuse.utils.radio import setup_flow
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


RADIO_COOLDOWN = commands.CooldownMapping.from_cooldown(1, 10, commands.BucketType.guild)


class RadioPrefixCommands:
    bot: BotCore
    hub: Any

    async def owner_check(self, ctx: CustomContext) -> bool:
        """Os subcomandos de dono do prefixo ``radio`` (premium, bot, pauseall, resumeall): só ``OWNER_IDS`` e só no bot principal."""
        return self._only_main() and is_env_owner(self.bot, ctx.author.id)

    async def manager_check(self, ctx: CustomContext) -> bool:
        """O prefixo ``radio`` e seus subcomandos públicos: ``manage_guild`` no servidor, conferido em execução, e só no bot principal."""
        return self._only_main() and ctx.guild is not None and bool(ctx.author.guild_permissions.manage_guild)

    async def group_check(self, ctx: CustomContext) -> bool:
        return await self.manager_check(ctx) or await self.owner_check(ctx)

    # ------------------------------------------------------------------ prefixo ``radio`` (aliases ``rd`` e ``247``)

    @commands.group(name="radio", aliases=["rd", "247"], invoke_without_command=True)
    @commands.check(lambda ctx: ctx.cog.group_check(ctx))
    async def radio_prefix(self, ctx: CustomContext):
        """Sozinho, o status das rádios do servidor; os subcomandos fazem o resto."""
        await self.prefix_status(ctx)

    async def _prefix_gate(self, ctx: CustomContext):
        """Traduz e aplica o cooldown de 10 s por servidor, como o slash."""
        tr = await self._tr(ctx)
        bucket = RADIO_COOLDOWN.get_bucket(ctx.message)
        if bucket is not None and (retry := bucket.update_rate_limit()):
            raise GenericError(tr("radio.cooldown", seconds=int(retry) + 1))
        return tr

    @staticmethod
    def _prefix_channel(ctx: CustomContext, channel: Any, tr: Any) -> Any:
        found = channel or getattr(getattr(ctx.author, "voice", None), "channel", None)
        if found is None:
            raise GenericError(tr("radio.prefix.channel_needed"))
        return found

    @radio_prefix.command(name="status")
    @commands.check(lambda ctx: ctx.cog.manager_check(ctx))
    async def prefix_status(self, ctx: CustomContext):
        tr = await self._prefix_gate(ctx)
        await ctx.send(await self._status_text(tr, ctx.guild.id), allowed_mentions=disnake.AllowedMentions.none())

    @radio_prefix.command(name="on", aliases=["ligar"])
    @commands.check(lambda ctx: ctx.cog.manager_check(ctx))
    async def prefix_on(
        self, ctx: CustomContext, channel: Optional[Union[disnake.VoiceChannel, disnake.StageChannel]] = None
    ):
        tr = await self._prefix_gate(ctx)
        target = self._prefix_channel(ctx, channel, tr)
        await ctx.send(
            await self._turn_on(ctx.author, ctx.guild.id, tr, target), allowed_mentions=disnake.AllowedMentions.none()
        )

    @radio_prefix.command(name="off", aliases=["desligar"])
    @commands.check(lambda ctx: ctx.cog.manager_check(ctx))
    async def prefix_off(
        self, ctx: CustomContext, channel: Optional[Union[disnake.VoiceChannel, disnake.StageChannel]] = None
    ):
        tr = await self._prefix_gate(ctx)
        target = self._prefix_channel(ctx, channel, tr)
        await ctx.send(
            await self._switch_off(ctx.author, ctx.guild.id, tr, target),
            allowed_mentions=disnake.AllowedMentions.none(),
        )

    @radio_prefix.command(name="reset", aliases=["redefinir"])
    @commands.check(lambda ctx: ctx.cog.manager_check(ctx))
    async def prefix_reset(
        self, ctx: CustomContext, channel: Optional[Union[disnake.VoiceChannel, disnake.StageChannel]] = None
    ):
        tr = await self._prefix_gate(ctx)
        content, view = await setup_flow.prepare_reset(
            self, tr, ctx.author, ctx.guild.id, self._prefix_channel(ctx, channel, tr)
        )
        await ctx.send(content, view=view, allowed_mentions=disnake.AllowedMentions.none())

    @radio_prefix.command(name="setup", aliases=["configurar"])
    @commands.check(lambda ctx: ctx.cog.manager_check(ctx))
    async def prefix_setup(self, ctx: CustomContext):
        """A configuração tem seletores (bot, tipo e estação): só existe no comando de barra."""
        tr = await self._tr(ctx)
        await ctx.send(tr("radio.prefix.use_slash"))

    # ------------------------------------------------------------------ premium

    @radio_prefix.command(name="premium", hidden=True)
    @commands.check(lambda ctx: ctx.cog.owner_check(ctx))
    async def radio_premium(self, ctx: CustomContext, action: str = "", guild: str = ""):
        """``radio premium conceder|revogar <id do servidor>`` e ``radio premium listar``."""
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
                off = await self.hub.manager.revoke_premium(guild_id, updated_by=str(ctx.author.id))
                logger.info("Premium de rádio revogado (%d rádios desligadas)", off)
                await ctx.send(tr("radio.owner.premium_revoked", guild=guild_id, count=off))
        except RadioUnavailable:
            raise GenericError(tr("radio.owner.unavailable"))

    # ------------------------------------------------------------------ pausar todas

    @radio_prefix.command(name="pauseall", aliases=["pausartodas"], hidden=True)
    @commands.check(lambda ctx: ctx.cog.owner_check(ctx))
    async def radio_pause_all(self, ctx: CustomContext):
        tr = await self._tr(ctx)
        try:
            changed = await self.hub.control.set_paused_all(True, by=ctx.author.id)
        except RadioUnavailable:
            raise GenericError(tr("radio.owner.unavailable"))
        logger.info("Rádios: pausa geral %s", "ligada" if changed else "já estava ligada")
        await ctx.send(tr("radio.owner.paused_all" if changed else "radio.owner.paused_all_already"))

    @radio_prefix.command(name="resumeall", aliases=["retomartodas"], hidden=True)
    @commands.check(lambda ctx: ctx.cog.owner_check(ctx))
    async def radio_resume_all(self, ctx: CustomContext):
        tr = await self._tr(ctx)
        try:
            changed = await self.hub.control.set_paused_all(False, by=ctx.author.id)
        except RadioUnavailable:
            raise GenericError(tr("radio.owner.unavailable"))
        logger.info("Rádios: pausa geral %s", "desligada" if changed else "já estava desligada")
        await ctx.send(tr("radio.owner.resumed_all" if changed else "radio.owner.resumed_all_already"))

    # ------------------------------------------------------------------ bots só-rádio

    @radio_prefix.command(name="bot", hidden=True)
    @commands.check(lambda ctx: ctx.cog.owner_check(ctx))
    async def radio_bot(self, ctx: CustomContext, action: str = "", identifier: str = ""):
        """``radio bot adicionar|remover <TOKEN_2>`` e ``radio bot listar``: os bots reservas dedicados a rádio."""
        tr = await self._tr(ctx)
        action = action.casefold()
        try:
            if action in LIST:
                await self.hub.roles.load()
                bots = ", ".join(f"`{i}`" for i in self.hub.roles.radio_only) or tr("radio.owner.none")
                await ctx.send(tr("radio.owner.bots_list", bots=bots), allowed_mentions=disnake.AllowedMentions.none())
                return
            if action not in ADD + REMOVE or not identifier.isascii() or not 1 <= len(identifier) <= 40:
                raise GenericError(tr("radio.owner.bots_usage"))
            if action in ADD:
                await self.hub.roles.add(identifier)
            else:
                await self.hub.roles.remove(identifier)
            await ctx.send(
                tr("radio.owner.bots_changed", bot=identifier), allowed_mentions=disnake.AllowedMentions.none()
            )
        except MainBotRefused:
            raise GenericError(tr("radio.owner.main_refused"))
        except RadioUnavailable:
            raise GenericError(tr("radio.owner.unavailable"))
