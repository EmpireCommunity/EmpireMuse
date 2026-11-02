# -*- coding: utf-8 -*-
from __future__ import annotations

import disnake

from empiremuse.utils.music.errors import error_code_text, log_unexpected_error
from empiremuse.utils.music.skin_utils import skin_converter
from empiremuse.utils.others import (
    CustomContext,
)
from empiremuse.utils.music.interactions.common import (
    logger,
)


class SkinEditorPreviewMixin:
    """Pré-visualização da skin: converte os placeholders e atualiza a mensagem do editor."""

    def build_embeds(self) -> dict:
        player = None
        for b in self.bot.pool.get_guild_bots(self.ctx.guild_id):
            try:
                player = b.music.players[self.ctx.guild_id]
                break
            except KeyError:
                continue

        data = skin_converter(self.message_data, guild=self.guild, ctx=self.ctx, player=player)
        return {"content": data.get("content", ""), "embeds": data.get("embeds", [])}

    async def embed_select_callback(self, inter: disnake.MessageInteraction):
        self.ctx = inter
        self.embed_index = int(inter.values[0][11:])
        await inter.response.defer()

    async def embed_value_select_callback(self, inter: disnake.MessageInteraction):
        self.ctx = inter
        self.embed_field_index = int(inter.values[0][17:])
        await inter.response.defer()

    async def update_message(self, inter: disnake.MessageInteraction):
        self.ctx = inter
        try:
            if isinstance(self.ctx, CustomContext):
                await inter.response.edit_message(view=self, **self.build_embeds())
            elif not inter.response.is_done():
                await inter.response.edit_message(view=self, **self.build_embeds())
            else:
                await inter.edit_original_message(view=self, **self.build_embeds())
        except Exception as e:
            code = log_unexpected_error(logger, e, "skin_editor.update_message")
            await inter.send(self.tr("skin_editor.preview.error") + error_code_text(code), ephemeral=True)
