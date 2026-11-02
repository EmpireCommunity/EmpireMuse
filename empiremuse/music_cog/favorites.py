# -*- coding: utf-8 -*-

import disnake
from disnake.ext import commands

from empiremuse.i18n import localized
from empiremuse.utils.db import DBModel
from empiremuse.utils.music.errors import (
    GenericError,
)
from empiremuse.utils.music.interactions import (
    FavMenuView,
    ViewMode,
)
from empiremuse.cogs.server_playlists import NOT_ALLOWED, member_may_manage
from empiremuse.utils.others import CustomContext

# O corpo da classe usa estes nomes, então eles precisam existir neste módulo.


class FavoritesMixin:
    fav_import_export_cd = commands.CooldownMapping.from_cooldown(1, 15, commands.BucketType.member)

    fav_cd = commands.CooldownMapping.from_cooldown(3, 15, commands.BucketType.member)

    @commands.command(
        name="favmanager",
        aliases=[
            "favs",
            "favoritos",
            "itg",
            "spl",
        ],
        description="Gerenciar seus favoritos/integrações e links do server.",
        cooldown=fav_cd,
    )
    async def fav_manager_legacy(self, ctx: CustomContext):
        await self.fav_manager.callback(self=self, inter=ctx)

    @commands.max_concurrency(1, commands.BucketType.member, wait=False)
    @commands.slash_command(
        description=localized("cmd.fav_manager.description"),
        cooldown=fav_cd,
        extras={"allow_private": True},
    )
    @commands.contexts(guild=True)
    async def fav_manager(self, inter: disnake.ApplicationCommandInteraction):
        bot = self.bot

        mode = ViewMode.fav_manager

        interaction = None

        if isinstance(inter, CustomContext):
            prefix = inter.clean_prefix

            if inter.invoked_with == "spl":
                # as playlists são do servidor (não de um bot): sem escolher bot; só dono, Administrador ou Gerenciar Servidor
                if not member_may_manage(inter.author, inter.guild):
                    raise GenericError(NOT_ALLOWED)

                mode = ViewMode.guild_fav_manager

            elif inter.invoked_with == "itg":
                mode = ViewMode.integrations_manager

        else:
            global_data = await bot.get_global_data(inter.guild_id, db_name=DBModel.guilds)
            prefix = global_data["prefix"] or bot.default_prefix

        if not interaction:
            interaction = inter

        cog = self.bot.get_cog("Music")

        if cog:
            ephemeral = await cog.is_jukebox_channel(inter, ignore_thread=True)
            await inter.response.defer(ephemeral=ephemeral)
        else:
            ephemeral = True

        user_data = await bot.get_global_data(inter.author.id, db_name=DBModel.users)

        view = FavMenuView(
            bot=bot,
            ctx=interaction,
            data=user_data,
            prefix=prefix,
            mode=mode,
            is_owner=await bot.is_owner(inter.author),
        )

        if mode == ViewMode.guild_fav_manager:
            await view.load_playlists(inter.guild_id)

        txt = view.build_txt()

        if not txt:
            raise GenericError(
                "**Não há suporte a esse recurso no momento...**\n\n`Suporte ao spotify e YTDL não estão ativados.`"
            )

        view.message = await inter.send(txt, view=view, ephemeral=ephemeral)

        await view.wait()
