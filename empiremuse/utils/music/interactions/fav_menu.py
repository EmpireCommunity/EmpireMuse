# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging
from io import BytesIO
from typing import Union, TYPE_CHECKING

import disnake

from empiremuse.utils.db import DBModel
from empiremuse.utils.music.checks import check_pool_bots
from empiremuse.utils.music.converters import time_format, md_link
from empiremuse.utils.others import (
    CustomContext,
    PlayerControls,
)

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore

from empiremuse.utils.music.interactions.fav_modals import (
    FavModalAdd,
    FavModalImport,
)
from empiremuse.utils.music.interactions.common import (
    ViewMode,
)
from empiremuse.utils.music.interactions.fav_menu_components import (
    FavMenuComponentsMixin,
)
from empiremuse.utils.music.interactions.fav_menu_radio import (
    FavMenuRadioMixin,
)
from empiremuse.utils.music.interactions.fav_menu_text import (
    FavMenuTextMixin,
)

logger = logging.getLogger(__name__)

NO_MENTIONS = disnake.AllowedMentions.none()


def message_is_ephemeral(inter: disnake.MessageInteraction) -> bool:
    """``True`` só se a mensagem do componente é efêmera (a lista pessoal de rádios só aparece em uma)."""
    return getattr(getattr(getattr(inter, "message", None), "flags", None), "ephemeral", False) is True


class FavMenuView(FavMenuComponentsMixin, FavMenuTextMixin, FavMenuRadioMixin, disnake.ui.View):
    def __init__(
        self,
        bot: BotCore,
        ctx: Union[disnake.ApplicationCommandInteraction, CustomContext],
        data: dict,
        log: str = "",
        prefix="",
        mode: str = ViewMode.fav_manager,
        is_owner=False,
        tr=None,
        radio_private: bool = False,
    ):
        super().__init__(timeout=180)
        self.mode = mode
        self.bot = bot
        self.ctx = ctx
        self.guild = ctx.guild
        self.current = None
        self.data = data
        self.message = None
        self.log = log
        self.prefix = prefix
        self.components_updater_task = bot.loop.create_task(self.auto_update())
        self.is_owner = is_owner
        self.light_mode = False
        self.init_radio_state(private=radio_private)  # o modo rádio só lista com a mensagem efêmera (fav_menu_radio)

        if not self.guild:
            for b in self.bot.pool.get_all_bots():
                guild = b.get_guild(ctx.guild_id)
                if guild:
                    self.guild = guild
                    break

            if not self.guild:
                self.light_mode = True

        # o tradutor do servidor vem de quem cria o menu; sem ele, vale o idioma do Discord do servidor
        i18n = bot.pool.i18n
        self.tr = (
            tr if tr is not None else i18n.for_locale(i18n.resolve(None, getattr(self.guild, "preferred_locale", None)))
        )

    async def auto_update(self):
        while True:
            if self.mode != ViewMode.fav_manager:
                return

            user, data, url = await self.bot.wait_for(
                "fav_add", check=lambda user, data, url: user.id == self.ctx.author.id
            )

            self.log = self.tr("fav.log.added", url=str(url))

            if not isinstance(self.ctx, CustomContext):
                await self.ctx.edit_original_message(content=self.build_txt(), view=self)
            elif self.message:
                await self.message.edit(content=self.build_txt(), view=self)

    async def on_timeout(self):
        try:
            self.components_updater_task.cancel()
        except Exception:
            pass

        try:
            for i in self.children[1].options:
                i.default = self.current == i.value
        except Exception:
            pass

        for c in self.children:
            c.disabled = True

        if isinstance(self.ctx, CustomContext):
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

        else:
            try:
                await self.ctx.edit_original_message(view=self)
            except Exception:
                pass

        self.stop()

    async def add_callback(self, inter: disnake.MessageInteraction):
        await inter.response.send_modal(FavModalAdd(name=None, url=None, view=self))

    async def edit_callback(self, inter: disnake.MessageInteraction):
        if not self.current:
            await inter.send(self.tr("fav.select_item"), ephemeral=True)
            return

        if self.mode == ViewMode.fav_manager:
            try:
                await inter.response.send_modal(
                    FavModalAdd(name=self.current, url=self.data["fav_links"][self.current], view=self)
                )
            except KeyError:
                await inter.send(self.tr("fav.not_found", name=str(self.current)), ephemeral=True)

    async def remove_callback(self, inter: disnake.MessageInteraction):
        if not self.current:
            await inter.send(self.tr("fav.select_item"), ephemeral=True)
            return

        await inter.response.defer(ephemeral=True)

        self.data = await self.bot.get_global_data(inter.author.id, db_name=DBModel.users)

        if self.mode == ViewMode.fav_manager:
            try:
                url = md_link(self.current, self.data["fav_links"][self.current])
                del self.data["fav_links"][self.current]
            except Exception:
                await inter.edit_original_message(self.tr("fav.not_in_list", name=str(self.current)))
                return

            await self.bot.update_global_data(inter.author.id, self.data, db_name=DBModel.users)

            self.log = self.tr("fav.log.removed", link=url)

        elif self.mode == ViewMode.integrations_manager:
            item = self.data["integration_links"][self.current]

            if isinstance(item, dict):
                item = item["url"]

            try:
                url = md_link(self.current, item)
                del self.data["integration_links"][self.current]
            except Exception:
                await inter.send(self.tr("fav.itg.not_in_list", name=str(self.current)), ephemeral=True)
                return

            await self.bot.update_global_data(inter.author.id, self.data, db_name=DBModel.users)

            self.log = self.tr("fav.itg.log.removed", link=url)

        await inter.edit_original_message(content=self.build_txt(), view=self)

    async def clear_callback(self, inter: disnake.MessageInteraction):
        await inter.response.defer(ephemeral=True)

        self.data = await self.bot.get_global_data(inter.author.id, db_name=DBModel.users)

        if self.mode == ViewMode.fav_manager:
            if not self.data["fav_links"]:
                await inter.send(self.tr("fav.no_favorites"), ephemeral=True)
                return

            fp = BytesIO(bytes(json.dumps(self.data["fav_links"], indent=4), "utf-8"))

            self.data["fav_links"].clear()

            await self.bot.update_global_data(inter.author.id, self.data, db_name=DBModel.users)

            self.log = self.tr("fav.log.cleared")

            await inter.send(
                self.tr("fav.cleared"),
                ephemeral=True,
                file=disnake.File(fp, filename="favs.json"),
            )

        elif self.mode == ViewMode.integrations_manager:
            if not self.data["integration_links"]:
                await inter.response.edit_message(content=self.tr("fav.itg.none_saved"), view=None)
                return

            fp = BytesIO(bytes(json.dumps(self.data["integration_links"], indent=4), "utf-8"))

            self.data["integration_links"].clear()

            await self.bot.update_global_data(inter.author.id, self.data, db_name=DBModel.users)

            self.log = self.tr("fav.itg.log.cleared")

            await inter.send(
                self.tr("fav.itg.cleared"),
                ephemeral=True,
                file=disnake.File(fp, filename="integrations.json"),
            )

        self.current = None

        await self._redraw()

    async def _redraw(self) -> None:
        """Redesenha a mensagem do menu (texto com nomes de terceiros: nunca menciona ninguém)."""
        kwargs = {"content": self.build_txt(), "view": self, "allowed_mentions": disnake.AllowedMentions.none()}
        if not isinstance(self.ctx, CustomContext):
            await self.ctx.edit_original_message(**kwargs)
        elif self.message:
            await self.message.edit(**kwargs)

    async def import_callback(self, inter: disnake.MessageInteraction):
        await inter.response.send_modal(FavModalImport(view=self))

    async def play_callback(self, inter: disnake.MessageInteraction):
        await check_pool_bots(inter, check_player=False)
        await self.bot.get_cog("Music").player_controller(
            inter,
            PlayerControls.add_song,
            query=f"> itg: {self.current}" if self.mode == ViewMode.integrations_manager else f"> fav: {self.current}",
        )

    async def export_callback(self, inter: disnake.MessageInteraction):
        cog = self.bot.get_cog("Music")

        if retry_after := cog.fav_import_export_cd.get_bucket(inter).update_rate_limit():
            if retry_after < 1:
                retry_after = 1
            await inter.send(
                self.tr("fav.export.wait", time=time_format(int(retry_after) * 1000, use_names=True)),
                ephemeral=True,
            )
            return

        await inter.response.defer(ephemeral=True)

        try:
            cmd = (
                f"</{cog.fav_manager.name}:"
                + str(
                    self.bot.get_global_command_named(
                        cog.fav_manager.name, cmd_type=disnake.ApplicationCommandType.chat_input
                    ).id
                )
                + ">"
            )
        except AttributeError:
            cmd = "/play"

        if self.mode == ViewMode.fav_manager:
            if not self.data["fav_links"]:
                await inter.send(
                    self.tr("fav.export.none", cmd=cmd),
                    ephemeral=True,
                )
                return

            fp = BytesIO(bytes(json.dumps(self.data["fav_links"], indent=4), "utf-8"))

            await inter.send(
                embed=disnake.Embed(
                    description=self.tr("fav.export.here", cmd=cmd),
                    color=self.bot.get_color(),
                ),
                file=disnake.File(fp=fp, filename="favoritos.json"),
                ephemeral=True,
            )

        elif self.mode == ViewMode.integrations_manager:
            if not self.data["integration_links"]:
                await inter.edit_original_message(self.tr("fav.itg.export.none", cmd=cmd))
                return

            fp = BytesIO(bytes(json.dumps(self.data["integration_links"], indent=4), "utf-8"))

            await inter.send(
                embed=disnake.Embed(
                    description=self.tr("fav.itg.export.here", cmd=cmd),
                    color=self.bot.get_color(),
                ),
                file=disnake.File(fp=fp, filename="integrations.json"),
                ephemeral=True,
            )

    async def cancel_callback(self, inter: disnake.MessageInteraction):
        try:
            self.components_updater_task.cancel()
        except Exception:
            pass

        await inter.response.edit_message(content=self.tr("fav.closed"), view=None)
        self.stop()

    async def _open_radio_favorites(self, inter: disnake.MessageInteraction):
        """Entra no modo rádio. A lista pessoal nunca vai a uma mensagem pública: se este menu está numa, o modo abre
        numa mensagem efêmera nova e este menu fica como está."""
        if not self.radio_available():
            await inter.send(self.tr("fav.unsupported"), ephemeral=True)
            return

        if message_is_ephemeral(inter):
            await inter.response.defer()
            try:
                self.components_updater_task.cancel()
            except Exception:
                pass
            self.mode, self.radio_private, self.log = ViewMode.radio_favorites, True, ""
            await self.load_radio()
            await inter.edit_original_message(content=self.build_txt(), view=self, allowed_mentions=NO_MENTIONS)
            return

        await inter.response.defer(with_message=True, ephemeral=True)
        private_view = type(self)(
            bot=self.bot,
            ctx=inter,
            data=self.data,
            prefix=self.prefix,
            mode=ViewMode.radio_favorites,
            is_owner=self.is_owner,
            tr=self.tr,
            radio_private=True,
        )
        await private_view.load_radio()
        await inter.edit_original_message(
            content=private_view.build_txt(), view=private_view, allowed_mentions=NO_MENTIONS
        )

    async def mode_callback(self, inter: disnake.MessageInteraction):
        if inter.values[0][14:] == ViewMode.radio_favorites:
            await self._open_radio_favorites(inter)
            return

        self.mode = inter.values[0][14:]

        try:
            self.components_updater_task.cancel()
        except Exception:
            pass

        if self.mode == ViewMode.fav_manager:
            self.components_updater_task = self.bot.loop.create_task(self.auto_update())

        if inter.response.is_done():
            await inter.edit_original_message(content=self.build_txt(), view=self)
        else:
            await inter.response.edit_message(content=self.build_txt(), view=self)

    async def select_callback(self, inter: disnake.MessageInteraction):
        self.current = inter.values[0]
        await inter.response.defer()

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if inter.author.id == self.ctx.author.id:
            return True

        await inter.send(self.tr("fav.only_member", mention=self.ctx.author.mention), ephemeral=True)
