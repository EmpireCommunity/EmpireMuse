# -*- coding: utf-8 -*-
from __future__ import annotations

import disnake

from empiremuse.utils.db import DBModel
from empiremuse.serialization.schema import SchemaError
from empiremuse.serialization.skins import MAX_SKINS_PER_MODE, validate_skin, validate_skin_name
from empiremuse.utils.music.skin_store import load_custom_skins
from empiremuse.utils.others import (
    CustomContext,
)
from empiremuse.utils.music.interactions.common import (
    logger,
)
from empiremuse.utils.music.interactions.skin_editor_data import (
    SKIN_LIMIT_REFUSED,
    SKIN_NAME_REFUSED,
    SKIN_REFUSED,
    base_skin_for,
)


def _skin_name_from_selection(selected: str) -> str:
    """Tira o prefixo de modo (``> cs: `` / ``> css: ``) do valor do select."""
    return selected.replace("> css: ", "", 1).replace("> cs: ", "", 1)


class SkinEditorPersistMixin:
    """Leitura, gravação e exclusão das skins do servidor feitas pelo editor; textos pelo ``self.tr``."""

    async def _refuse_skin(self, reply, guild_id: int, error: SchemaError, key: str):
        """Não grava nada: avisa o dono (``key`` do catálogo) e deixa no log só o servidor e o código da falha."""
        logger.warning("Skin do editor não salva: servidor %s, motivo %s", guild_id, error.code)
        await reply.edit_original_message(self.tr(key), view=None)

    async def load_skin(self, inter: disnake.MessageInteraction):
        self.ctx = inter
        selected = inter.values[0]

        if selected.startswith("> cs: "):
            mode, name = "custom_skins", selected[6:]
        elif selected.startswith("> css: "):
            mode, name = "custom_skins_static", selected[7:]
        else:
            mode = name = None

        message_data = base_skin_for(self.tr)

        if mode:
            skins = await load_custom_skins(self.bot, self.guild.id, self.global_data)
            readable = skins.normal if mode == "custom_skins" else skins.static
            if name in readable:
                message_data = readable[name]
            elif self.global_data[mode][name]:
                await inter.send(self.tr("skin_editor.persist.incompatible"), ephemeral=True)
                return

        self.skin_selected = selected
        self.mode = "editor"
        self.message_data = message_data
        self.update_components()
        await self.update_message(inter)

    async def delete_skin(self, inter: disnake.MessageInteraction):
        self.ctx = inter

        await inter.response.defer()

        self.global_data = await self.bot.get_global_data(id_=inter.guild_id, db_name=DBModel.guilds)

        if self.skin_selected.startswith("> cs:"):
            try:
                del self.global_data["custom_skins"][self.skin_selected[6:]]
            except KeyError:
                await inter.send(self.tr("skin_editor.persist.gone", name=self.skin_selected[6:]), ephemeral=True)
                return

        elif self.skin_selected.startswith("> css:"):
            try:
                del self.global_data["custom_skins_static"][self.skin_selected[7:]]
            except KeyError:
                await inter.send(self.tr("skin_editor.persist.gone", name=self.skin_selected[7:]), ephemeral=True)
                return

        await self.bot.update_global_data(id_=inter.guild_id, data=self.global_data, db_name=DBModel.guilds)

        self.mode = "select"
        self.skin_selected = ""
        self.update_components()

        await inter.edit_original_message(view=self, **self.build_embeds())

    async def save_skin_from_modal(self, inter: disnake.ModalInteraction):
        view = await self._ask_skin_mode(inter)

        if view.mode is None:
            await inter.edit_original_message(self.tr("skin_editor.persist.timeout"), components=[])
            return

        self.message_data["controller_enabled"] = view.controller_enabled

        if view.inter:
            await view.inter.response.defer(ephemeral=True)

        self.global_data = await self.bot.get_global_data(self.ctx.guild_id, db_name=DBModel.guilds)

        skin_name = _skin_name_from_selection(self.skin_selected)

        validated = await self._validate_skin_to_save(inter, view, skin_name)
        if not validated:
            return
        modal_skin_name, new_skin = validated

        await load_custom_skins(self.bot, inter.guild_id, self.global_data)  # migra as legadas antes de gravar

        if modal_skin_name != skin_name:
            self.global_data[view.mode].pop(skin_name, None)

        self.global_data[view.mode][modal_skin_name] = new_skin

        await self.bot.update_global_data(id_=inter.guild_id, data=self.global_data, db_name=DBModel.guilds)

        await self._reload_guild_players(inter.guild_id)

        saved_text = self._skin_saved_text()

        if not view.inter:
            view.inter = inter

        await view.inter.edit_original_message(saved_text, view=None)

        self.skin_selected = ("> cs: " if view.mode == "custom_skins" else "> css: ") + modal_skin_name

        self.update_components()

        if isinstance(self.ctx, CustomContext):
            await self.message.edit(view=self, **self.build_embeds())
        else:
            await self.ctx.edit_original_message(view=self, **self.build_embeds())

    async def _validate_skin_to_save(self, inter: disnake.ModalInteraction, view, skin_name: str):
        """Valida nome, conteúdo e limite do modo; devolve ``(nome, skin)`` ou ``None`` após avisar a recusa."""
        reply = view.inter or inter

        try:
            modal_skin_name = validate_skin_name(inter.text_values["skin_name"])
        except SchemaError as error:
            await self._refuse_skin(reply, inter.guild_id, error, SKIN_NAME_REFUSED)
            return None

        try:
            new_skin = validate_skin(self.message_data)
        except SchemaError as error:
            await self._refuse_skin(reply, inter.guild_id, error, SKIN_REFUSED)
            return None

        names = set(self.global_data[view.mode])
        if modal_skin_name != skin_name:
            names.discard(skin_name)
        names.add(modal_skin_name)
        if len(names) > MAX_SKINS_PER_MODE:
            await self._refuse_skin(reply, inter.guild_id, SchemaError("", "length"), SKIN_LIMIT_REFUSED)
            return None

        return modal_skin_name, new_skin

    async def _reload_guild_players(self, guild_id: int):
        """Aplica as skins recém-gravadas aos players ativos do servidor em todos os bots do pool."""
        for bot in self.bot.pool.get_all_bots():
            try:
                player = bot.music.players[guild_id]
            except KeyError:
                continue

            skins = await load_custom_skins(self.bot, guild_id, self.global_data)

            player.custom_skin_data = skins.normal
            player.custom_skin_static_data = skins.static
            player.setup_features()
            player.setup_hints()
            player.process_hint()

    def _skin_saved_text(self) -> str:
        try:
            cmd = (
                f"</change_skin:"
                + str(
                    self.bot.get_global_command_named(
                        "change_skin", cmd_type=disnake.ApplicationCommandType.chat_input
                    ).id
                )
                + ">"
            )
        except AttributeError:
            cmd = "/change_skin"

        try:
            guild_prefix = self.bot.pool.guild_prefix_cache[self.ctx.guild_id]
        except KeyError:
            guild_prefix = self.global_data.get("prefix")

        if not guild_prefix:
            guild_prefix = self.bot.config.get("DEFAULT_PREFIX") or "!!"

        return self.tr("skin_editor.persist.saved", command=cmd, prefix=guild_prefix)
