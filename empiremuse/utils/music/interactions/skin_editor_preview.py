# -*- coding: utf-8 -*-
"""A prévia do editor de skin personalizada (``/custom_skin``).

- **Call privada:** a mensagem do editor é pública no canal, então numa call privada (ou sem call conhecida, a regra de
  todas as skins) a prévia não usa o player de verdade. Com a visão mascarada (``private_call.PrivateCallView``) ela mostra
  o player sem faixa, capa, quem pediu nem fila; sem essa visão, os dados de exemplo (``player=None``).
"""

from __future__ import annotations

from typing import Any

import disnake

from empiremuse.utils.jukebox_panel import is_private
from empiremuse.utils.music.errors import error_code_text, log_unexpected_error
from empiremuse.utils.music.skin_utils import skin_converter
from empiremuse.utils.others import (
    CustomContext,
)
from empiremuse.utils.music.interactions.common import (
    logger,
)


def preview_player(player: Any) -> Any:
    """O player que a prévia pode mostrar: o próprio numa call pública; numa privada, a visão mascarada ou nenhum."""
    if player is None:
        return None
    voice = getattr(getattr(getattr(getattr(player, "guild", None), "me", None), "voice", None), "channel", None)
    if voice is not None and not is_private(voice):
        return player
    try:
        from empiremuse.utils.music.private_call import PrivateCallView
    except ImportError:  # sem a visão mascarada: os dados de exemplo
        return None
    return PrivateCallView(player)


class SkinEditorPreviewMixin:
    """Pré-visualização da skin: converte os placeholders e atualiza a mensagem do editor."""

    def build_embeds(self) -> dict:
        player = None
        for b in self.bot.pool.get_all_bots():
            try:
                player = b.music.players[self.ctx.guild_id]
                break
            except KeyError:
                continue

        data = skin_converter(self.message_data, guild=self.guild, ctx=self.ctx, player=preview_player(player))
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
            await inter.send(self.tr("skin_editor.preview.error") + error_code_text(code, self.tr), ephemeral=True)
