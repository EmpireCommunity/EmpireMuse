# -*- coding: utf-8 -*-
from __future__ import annotations

from copy import deepcopy
from typing import Union, Optional, TYPE_CHECKING, Literal

import disnake

from empiremuse.utils.others import (
    CustomContext,
)

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore

from empiremuse.utils.music.interactions.skin_editor_components import (
    SkinEditorComponentsMixin,
)
from empiremuse.utils.music.interactions.skin_editor_data import (
    SKIN_NAME_REFUSED as SKIN_NAME_REFUSED,
    SKIN_REFUSED as SKIN_REFUSED,
    SKIN_LIMIT_REFUSED as SKIN_LIMIT_REFUSED,
    base_skin as base_skin,
)
from empiremuse.utils.music.interactions.skin_editor_modals import (
    SkinEditorModalsMixin,
)
from empiremuse.utils.music.interactions.skin_editor_persist import (
    SkinEditorPersistMixin,
)
from empiremuse.utils.music.interactions.skin_editor_preview import (
    SkinEditorPreviewMixin,
)
from empiremuse.utils.music.interactions.skin_editor_submit import (
    SkinEditorSubmitMixin,
)


class SkinSettingsButton(disnake.ui.View):
    def __init__(self, user: disnake.Member, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.mode = "custom_skins_static"
        self.inter = None
        self.controller_enabled = True
        self.update_components()

    def update_components(self):
        self.clear_items()

        select_mode = disnake.ui.Select(
            min_values=1,
            max_values=1,
            options=[
                disnake.SelectOption(
                    label="Modo Normal",
                    description="Aplicar skin ao modo normal do player",
                    value="custom_skins",
                    default=self.mode == "custom_skins",
                ),
                disnake.SelectOption(
                    label="Jukebox",
                    description="Aplicar skin no modo Jukebox do player",
                    value="custom_skins_static",
                    default=self.mode == "custom_skins_static",
                ),
            ],
        )
        select_mode.callback = self.player_mode
        self.add_item(select_mode)

        if self.mode == "custom_skins":
            controller_btn = disnake.ui.Button(
                emoji="💠",
                label="Ativar Player-Controller" if not self.controller_enabled else "Desativar Player-Controller",
            )
            controller_btn.callback = self.controller_buttons
            self.add_item(controller_btn)

        save_btn = disnake.ui.Button(label="Salvar", emoji="💾")
        save_btn.callback = self.save
        self.add_item(save_btn)

    async def controller_buttons(self, inter: disnake.MessageInteraction):
        self.controller_enabled = not self.controller_enabled
        self.update_components()
        await inter.response.edit_message(view=self)

    async def player_mode(self, inter: disnake.MessageInteraction):
        self.mode = inter.values[0]
        self.update_components()
        await inter.response.edit_message(view=self)

    async def save(self, inter: disnake.ModalInteraction):
        self.inter = inter
        self.stop()

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if inter.user.id != self.user.id:
            await inter.send(f"Apenas o membro {self.user.mention} pode usar es botões da mensagem.", ephemeral=True)
            return False

        return True


class SkinEditorMenu(
    SkinEditorComponentsMixin,
    SkinEditorModalsMixin,
    SkinEditorSubmitMixin,
    SkinEditorPersistMixin,
    SkinEditorPreviewMixin,
    disnake.ui.View,
):
    """Menu do editor de skins; cada responsabilidade vive no mixin do módulo ``skin_editor_*`` correspondente."""

    def __init__(
        self,
        ctx: Union[CustomContext, disnake.ApplicationCommandInteraction],
        bot: BotCore,
        guild: disnake.Guild,
        global_data: dict,
    ):
        super().__init__(timeout=600)
        self.ctx = ctx
        self.bot = bot
        self.guild = guild
        self.message: Optional[disnake.Message] = None
        self.embed_index = 0
        self.embed_field_index = 0
        self.mode: Literal["editor", "select"] = "select"
        self.global_data = global_data
        self.skin_selected = ""
        self.message_data = {}
        self.update_components()

    def disable_buttons(self):
        for c in self.children:
            if c.custom_id != "skin_editor_placeholders":
                c.disabled = True

    async def new_skin(self, inter: disnake.MessageInteraction):
        self.ctx = inter
        self.message_data = deepcopy(base_skin)
        self.mode = "editor"
        self.update_components()
        await self.update_message(inter)

    async def _ask_skin_mode(self, inter: disnake.ModalInteraction) -> SkinSettingsButton:
        """Pergunta em qual modo do player a skin será aplicada e espera a resposta (ou o tempo esgotar)."""
        view = SkinSettingsButton(self.ctx.author, timeout=30)
        view.controller_enabled = self.message_data.get("controller_enabled", True)
        await inter.send("**Selecione o modo de player que será aplicado na skin.**", view=view, ephemeral=True)
        await view.wait()
        return view

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if inter.author.id != self.ctx.author.id:
            await inter.send(
                f"Apenas o membro {self.ctx.author.mention} pode interagir nessa mensagem.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        self.disable_buttons()

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

    async def back(self, inter: disnake.MessageInteraction):
        self.ctx = inter
        self.mode = "select"
        self.skin_selected = ""
        self.message_data = {}
        self.update_components()
        await self.update_message(inter)
