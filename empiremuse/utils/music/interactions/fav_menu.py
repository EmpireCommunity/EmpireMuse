# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging
from io import BytesIO
from typing import Union, TYPE_CHECKING

import disnake

from empiremuse.rules.errors import RuleError
from empiremuse.rules.playlists import display_name
from empiremuse.utils.db import DBModel
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.music.checks import check_pool_bots
from empiremuse.utils.music.converters import time_format, md_code, md_link
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
from empiremuse.utils.music.interactions.fav_menu_text import (
    FavMenuTextMixin,
)
from empiremuse.utils.music.interactions import fav_menu_playlists as playlists_ui

logger = logging.getLogger(__name__)


class FavMenuView(FavMenuComponentsMixin, FavMenuTextMixin, disnake.ui.View):
    def __init__(
        self,
        bot: BotCore,
        ctx: Union[disnake.ApplicationCommandInteraction, CustomContext],
        data: dict,
        log: str = "",
        prefix="",
        mode: str = ViewMode.fav_manager,
        is_owner=False,
    ):
        super().__init__(timeout=180)
        self.mode = mode
        self.bot = bot
        self.ctx = ctx
        self.guild = ctx.guild
        self.current = None
        self.data = data
        self.playlists: tuple = ()
        self.message = None
        self.log = log
        self.prefix = prefix
        self.components_updater_task = bot.loop.create_task(self.auto_update())
        self.is_owner = is_owner
        self.light_mode = False

        if not self.guild:
            for b in self.bot.pool.get_guild_bots(ctx.guild_id):
                guild = b.get_guild(ctx.guild_id)
                if guild:
                    self.guild = guild
                    break

            if not self.guild:
                self.light_mode = True

    async def auto_update(self):
        while True:
            if self.mode != ViewMode.fav_manager:
                return

            user, data, url = await self.bot.wait_for(
                "fav_add", check=lambda user, data, url: user.id == self.ctx.author.id
            )

            self.log = f"<{url}> foi adicionado nos seus favoritos."

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

    async def load_playlists(self, guild_id: int) -> None:
        self.playlists = await self.bot.pool.servers.playlists.list(guild_id)

    async def finish_write(self, inter: disnake.ModalInteraction, log: str) -> None:
        """Depois de gravar no store: responde ao modal e redesenha o menu com a lista que o store tem agora."""
        await self.load_playlists(inter.guild_id)
        await inter.edit_original_message(content=log, allowed_mentions=disnake.AllowedMentions.none())
        self.log = log.split("\n", 1)[0]  # o relatório completo já foi para o modal; o menu leva só o resumo
        await self._redraw()

    async def add_callback(self, inter: disnake.MessageInteraction):
        if self.mode == ViewMode.guild_fav_manager:
            if not playlists_ui.can_manage(inter):
                await inter.send(playlists_ui.MSG_DENIED, ephemeral=True)
                return
            await inter.response.send_modal(playlists_ui.PlaylistModal(self))
            return
        await inter.response.send_modal(FavModalAdd(name=None, url=None, view=self))

    async def edit_callback(self, inter: disnake.MessageInteraction):
        if not self.current:
            await inter.send("Você deve selecionar um item!", ephemeral=True)
            return

        if self.mode == ViewMode.fav_manager:
            try:
                await inter.response.send_modal(
                    FavModalAdd(name=self.current, url=self.data["fav_links"][self.current], view=self)
                )
            except KeyError:
                await inter.send(f"**Não há favorito com o nome:** {self.current}", ephemeral=True)

        elif self.mode == ViewMode.guild_fav_manager:
            if not playlists_ui.can_manage(inter):
                await inter.send(playlists_ui.MSG_DENIED, ephemeral=True)
                return
            await self.load_playlists(inter.guild_id)
            item = playlists_ui.find_by_name(self.playlists, self.current)
            if item is None:
                await inter.send(f"**Não há playlist com o nome:** {md_code(self.current)}", ephemeral=True)
                return
            await inter.response.send_modal(playlists_ui.PlaylistModal(self, item))

    async def remove_callback(self, inter: disnake.MessageInteraction):
        if not self.current:
            await inter.send("Você deve selecionar um item!", ephemeral=True)
            return

        if self.mode == ViewMode.guild_fav_manager:
            await self._remove_playlist(inter)
            return

        await inter.response.defer(ephemeral=True)

        self.data = await self.bot.get_global_data(inter.author.id, db_name=DBModel.users)

        if self.mode == ViewMode.fav_manager:
            try:
                url = md_link(self.current, self.data["fav_links"][self.current])
                del self.data["fav_links"][self.current]
            except Exception:
                await inter.edit_original_message(f"**Não há favorito na lista com o nome:** {self.current}")
                return

            await self.bot.update_global_data(inter.author.id, self.data, db_name=DBModel.users)

            self.log = f"Favorito {url} foi removido com sucesso!"

        elif self.mode == ViewMode.integrations_manager:
            item = self.data["integration_links"][self.current]

            if isinstance(item, dict):
                item = item["url"]

            try:
                url = md_link(self.current, item)
                del self.data["integration_links"][self.current]
            except Exception:
                await inter.send(f"**Não há integração na lista com o nome:** {self.current}", ephemeral=True)
                return

            await self.bot.update_global_data(inter.author.id, self.data, db_name=DBModel.users)

            self.log = f"Integração {url} foi removida com sucesso!"

        await inter.edit_original_message(content=self.build_txt(), view=self)

    async def clear_callback(self, inter: disnake.MessageInteraction):
        if self.mode == ViewMode.guild_fav_manager:
            if not await playlists_ui.clear_all(self, inter):
                return
            self.log = "Lista de playlists do servidor foi limpa com sucesso!"
            await self.load_playlists(inter.guild_id)
            self.current = None
            await self._redraw()
            return

        await inter.response.defer(ephemeral=True)

        self.data = await self.bot.get_global_data(inter.author.id, db_name=DBModel.users)

        if self.mode == ViewMode.fav_manager:
            if not self.data["fav_links"]:
                await inter.send("**Você não possui links favoritos!**", ephemeral=True)
                return

            fp = BytesIO(bytes(json.dumps(self.data["fav_links"], indent=4), "utf-8"))

            self.data["fav_links"].clear()

            await self.bot.update_global_data(inter.author.id, self.data, db_name=DBModel.users)

            self.log = "Sua lista de favoritos foi limpa com sucesso!"

            await inter.send(
                "### Seus favoritos foram excluídos com sucesso!\n"
                "`Um arquivo de backup foi gerado e caso queira reverter essa exclusão, copie o "
                'conteúdo do arquivo e clique no botão "importar" e cole o conteudo no campo indicado.`',
                ephemeral=True,
                file=disnake.File(fp, filename="favs.json"),
            )

        elif self.mode == ViewMode.integrations_manager:
            if not self.data["integration_links"]:
                await inter.response.edit_message(content="**Você não possui integrações salvas!**", view=None)
                return

            fp = BytesIO(bytes(json.dumps(self.data["integration_links"], indent=4), "utf-8"))

            self.data["integration_links"].clear()

            await self.bot.update_global_data(inter.author.id, self.data, db_name=DBModel.users)

            self.log = "Sua lista de integrações foi limpa com sucesso!"

            await inter.send(
                "### Suas integrações foram excluídas com sucesso!\n"
                "`um arquivo de backup foi gerado e caso queira reverter essa exclusão, copie o "
                'conteúdo do arquivo e clique no botão "importar" e cole o conteudo no campo indicado.`',
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

    async def _remove_playlist(self, inter: disnake.MessageInteraction) -> None:
        """Remove a playlist selecionada (o nome é re-resolvido no store); erros vão numa resposta efêmera à parte."""
        if not playlists_ui.can_manage(inter):
            await inter.send(playlists_ui.MSG_DENIED, ephemeral=True)
            return
        await inter.response.defer()
        try:
            removed = await self.bot.pool.servers.playlists.remove(inter.guild_id, self.current)
            await self.load_playlists(inter.guild_id)
        except GenericError as exc:
            await inter.send(exc.text, ephemeral=True)
            return
        self.current = None
        if isinstance(removed, RuleError):
            await inter.send(playlists_ui.error_text(removed), ephemeral=True)
            await self._redraw()
            return
        self.log = f"Playlist {md_link(display_name(removed), removed.url)} foi removida da lista do servidor!"
        await self._redraw()

    async def import_callback(self, inter: disnake.MessageInteraction):
        if self.mode == ViewMode.guild_fav_manager:
            if not playlists_ui.can_manage(inter):
                await inter.send(playlists_ui.MSG_DENIED, ephemeral=True)
                return
            await inter.response.send_modal(playlists_ui.PlaylistImportModal(self))
            return
        await inter.response.send_modal(FavModalImport(view=self))

    async def play_callback(self, inter: disnake.MessageInteraction):
        await check_pool_bots(inter, check_player=False)
        await self.bot.get_cog("Music").player_controller(
            inter,
            PlayerControls.add_song,
            query=f"> itg: {self.current}" if self.mode == ViewMode.integrations_manager else f"> fav: {self.current}",
        )

    async def export_callback(self, inter: disnake.MessageInteraction):
        if self.mode == ViewMode.guild_fav_manager:
            await playlists_ui.export_file(self, inter)
            return

        cog = self.bot.get_cog("Music")

        if retry_after := cog.fav_import_export_cd.get_bucket(inter).update_rate_limit():
            if retry_after < 1:
                retry_after = 1
            await inter.send(
                "**Você deve aguardar {} para exportar.**".format(time_format(int(retry_after) * 1000, use_names=True)),
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
                    f"**Você não possui links favoritos..\nVocê pode adicionar usando o comando: {cmd}**",
                    ephemeral=True,
                )
                return

            fp = BytesIO(bytes(json.dumps(self.data["fav_links"], indent=4), "utf-8"))

            await inter.send(
                embed=disnake.Embed(
                    description=f"Seus favoritos estão aqui.\nVocê pode importar usando o comando: {cmd}",
                    color=self.bot.get_color(),
                ),
                file=disnake.File(fp=fp, filename="favoritos.json"),
                ephemeral=True,
            )

        elif self.mode == ViewMode.integrations_manager:
            if not self.data["integration_links"]:
                await inter.edit_original_message(
                    f"**Você não possui integrações adicionadas...\nVocê pode adicionar usando o comando: {cmd}**"
                )
                return

            fp = BytesIO(bytes(json.dumps(self.data["integration_links"], indent=4), "utf-8"))

            await inter.send(
                embed=disnake.Embed(
                    description=f"Suas integrações estão aqui.\nVocê pode importar usando o comando: {cmd}",
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

        await inter.response.edit_message(content="**Gerenciador fechado.**", view=None)
        self.stop()

    async def mode_callback(self, inter: disnake.MessageInteraction):
        self.mode = inter.values[0][14:]

        try:
            self.components_updater_task.cancel()
        except Exception:
            pass

        if self.mode == ViewMode.fav_manager:
            self.components_updater_task = self.bot.loop.create_task(self.auto_update())

        elif self.mode == ViewMode.guild_fav_manager:
            await inter.response.defer()
            await self.load_playlists(inter.guild_id)

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

        await inter.send(f"Apenas o membro {self.ctx.author.mention} pode interagir nessa mensagem.", ephemeral=True)
