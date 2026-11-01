# -*- coding: utf-8 -*-

import disnake
from disnake.ext import commands

from empiremuse.i18n import localized
from empiremuse.rules.errors import RuleError
from empiremuse.rules.settings import validate_voice_status
from empiremuse.utils.db import DBModel
from empiremuse.utils.music.errors import (
    GenericError,
    NoVoice,
    DiffVoiceChannel,
    NoPlayer,
)
from empiremuse.utils.music.checks import VOICE_STATUS_PERMISSION, voice_status_blocked_channel
from empiremuse.utils.music.interactions import (
    SetStageTitle,
)
from empiremuse.utils.music.interactions.stage_title import voice_status_error_text
from empiremuse.utils.others import (
    CustomContext,
    pool_command,
)

# O corpo da classe usa ``desc_prefix`` (como no corpo de ``Music``), então o nome precisa existir neste módulo.


class VoiceStatusMixin:
    stage_cd = commands.CooldownMapping.from_cooldown(2, 45, commands.BucketType.guild)
    stage_mc = commands.MaxConcurrency(1, per=commands.BucketType.guild, wait=False)

    @commands.has_guild_permissions(manage_guild=True)
    @pool_command(
        only_voiced=True,
        name="setvoicestatus",
        aliases=[
            "stagevc",
            "togglestageannounce",
            "announce",
            "vcannounce",
            "setstatus",
            "voicestatus",
            "setvcstatus",
            "statusvc",
            "vcstatus",
            "stageannounce",
        ],
        description="Ativar o sistema de anuncio/status automático do canal com o nome da música.",
        cooldown=stage_cd,
        max_concurrency=stage_mc,
        extras={"exclusive_cooldown": True},
        usage="{prefix}{cmd} <placeholders>\nEx: {track.author} - {track.title}",
    )
    async def setvoicestatus_legacy(self, ctx: CustomContext, *, template=""):
        await self.set_voice_status.callback(self=self, inter=ctx, template=template)

    @commands.slash_command(
        description=localized("cmd.set_voice_status.description"),
        extras={"only_voiced": True, "exclusive_cooldown": True},
        cooldown=stage_cd,
        max_concurrency=stage_mc,
        default_member_permissions=disnake.Permissions(manage_guild=True),
    )
    @commands.contexts(guild=True)
    async def set_voice_status(
        self,
        inter: disnake.ApplicationCommandInteraction,
        template: str = commands.Param(
            name=localized("cmd.set_voice_status.opt.template.name"),
            default="",
            description=localized("cmd.set_voice_status.opt.template.description"),
        ),
    ):
        if isinstance(template, commands.ParamInfo):
            template = ""

        try:
            bot = inter.music_bot
            guild = inter.music_guild
            author = guild.get_member(inter.author.id)
        except AttributeError:
            bot = inter.bot
            guild = inter.guild
            author = inter.author

        if not author.guild_permissions.manage_guild and not (await bot.is_owner(author)):
            raise GenericError((await inter.bot.pool.i18n.for_guild(inter.guild))("player.you_do_not_have_the"))

        if not template:
            await inter.response.defer(ephemeral=True, with_message=True)
            global_data = await self.bot.get_global_data(inter.guild_id, db_name=DBModel.guilds)
            view = SetStageTitle(ctx=inter, bot=bot, data=global_data, guild=guild)
            view.message = await inter.send(view=view, embeds=view.build_embeds(), ephemeral=True)
            await view.wait()
        else:
            status = validate_voice_status(template)

            if isinstance(status, RuleError) or not status:
                raise GenericError(
                    voice_status_error_text(status)
                    if isinstance(status, RuleError)
                    else (await inter.bot.pool.i18n.for_guild(inter.guild))(
                        "player.you_must_use_at_least", placeholder_text=str(SetStageTitle.placeholder_text)
                    )
                )

            template = status

            try:
                player = bot.music.players[inter.guild_id]
            except KeyError:
                raise NoPlayer()

            if not author.voice:
                raise NoVoice()

            if author.id not in guild.me.voice.channel.voice_states:
                raise DiffVoiceChannel()

            if blocked := voice_status_blocked_channel(player.guild):
                raise GenericError(
                    (await inter.bot.pool.i18n.for_guild(inter.guild))(
                        "player.i_don_t_have_the",
                        voice_status_permission=str(VOICE_STATUS_PERMISSION),
                        mention=str(blocked.mention),
                    )
                )

            await inter.response.defer()

            player.stage_title_event = True
            player.stage_title_template = template
            player.start_time = disnake.utils.utcnow()

            await player.update_stage_topic(force=True)

            await player.process_save_queue()

            player.set_command_log(
                text=(await inter.bot.pool.i18n.for_guild(inter.guild))("player.enabled_the_automatic_status"),
                emoji="📢",
            )

            player.update = True

            if isinstance(inter, CustomContext):
                await inter.send(
                    (await inter.bot.pool.i18n.for_guild(inter.guild))("player.the_automatic_status_was_set")
                )
            else:
                await inter.edit_original_message(
                    (await inter.bot.pool.i18n.for_guild(inter.guild))("player.the_automatic_status_was_set")
                )

    @set_voice_status.autocomplete("template")
    async def default_models(self, inter: disnake.Interaction, query: str):
        return [
            "{track.title} - By: {track.author} | {track.timestamp}",
            "{track.emoji} | {track.title}",
            "{track.title} ( {track.playlist} )",
            "{track.title}  Solicitado por: {requester.name}",
        ]
