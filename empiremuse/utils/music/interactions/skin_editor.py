# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Union, Optional, TYPE_CHECKING, Literal

import disnake

from empiremuse.rules.i18n import Translator, fit
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
    base_skin_for,
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
    def __init__(self, user: disnake.Member, *args, tr: Translator, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.tr = tr
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
                    label=fit(self.tr("skin_editor.mode.normal.label"), 80),
                    description=fit(self.tr("skin_editor.mode.normal.description"), 100),
                    value="custom_skins",
                    default=self.mode == "custom_skins",
                ),
                disnake.SelectOption(
                    label=fit(self.tr("skin_editor.mode.jukebox.label"), 80),
                    description=fit(self.tr("skin_editor.mode.jukebox.description"), 100),
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
                label=fit(
                    self.tr(
                        "skin_editor.mode.controller_off.label"
                        if self.controller_enabled
                        else "skin_editor.mode.controller_on.label"
                    ),
                    80,
                ),
            )
            controller_btn.callback = self.controller_buttons
            self.add_item(controller_btn)

        save_btn = disnake.ui.Button(label=fit(self.tr("skin_editor.mode.save.label"), 80), emoji="💾")
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
            await inter.send(self.tr("skin_editor.mode.only_member", mention=self.user.mention), ephemeral=True)
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
    """Menu do editor de skins; cada responsabilidade vive no mixin do módulo ``skin_editor_*`` correspondente.

    Os textos saem do catálogo pelo ``tr`` do servidor. Quem cria o menu pode passá-lo; sem ele, a montagem (síncrona) usa
    o idioma do Discord do servidor e a primeira interação troca pelo tradutor do servidor (``settings.language``).
    """

    def __init__(
        self,
        ctx: Union[CustomContext, disnake.ApplicationCommandInteraction],
        bot: BotCore,
        guild: disnake.Guild,
        global_data: dict,
        tr: Optional[Translator] = None,
    ):
        super().__init__(timeout=600)
        self.ctx = ctx
        self.bot = bot
        self.guild = guild
        self._tr_resolved = tr is not None
        self.tr: Translator = tr or self._sync_translator()
        self.message: Optional[disnake.Message] = None
        self.embed_index = 0
        self.embed_field_index = 0
        self.mode: Literal["editor", "select"] = "select"
        self.global_data = global_data
        self.skin_selected = ""
        self.message_data = {}
        self.update_components()

    def _sync_translator(self) -> Translator:
        """O tradutor pelo idioma do Discord do servidor (não espera o store): só até a primeira interação."""
        i18n = self.bot.pool.i18n
        return i18n.for_locale(i18n.resolve(None, getattr(self.guild, "preferred_locale", None)))

    async def _ensure_translator(self) -> None:
        """Troca o tradutor síncrono pelo do servidor (``settings.language``) uma vez, na primeira interação."""
        if self._tr_resolved:
            return
        self._tr_resolved = True
        self.tr = await self.bot.pool.i18n.for_guild(self.guild)

    def disable_buttons(self):
        for c in self.children:
            if c.custom_id != "skin_editor_placeholders":
                c.disabled = True

    async def new_skin(self, inter: disnake.MessageInteraction):
        self.ctx = inter
        self.message_data = base_skin_for(self.tr)
        self.mode = "editor"
        self.update_components()
        await self.update_message(inter)

    async def _ask_skin_mode(self, inter: disnake.ModalInteraction) -> SkinSettingsButton:
        """Pergunta em qual modo do player a skin será aplicada e espera a resposta (ou o tempo esgotar)."""
        view = SkinSettingsButton(self.ctx.author, timeout=30, tr=self.tr)
        view.controller_enabled = self.message_data.get("controller_enabled", True)
        await inter.send(self.tr("skin_editor.mode.ask"), view=view, ephemeral=True)
        await view.wait()
        return view

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        await self._ensure_translator()
        if inter.author.id != self.ctx.author.id:
            await inter.send(self.tr("skin_editor.menu.only_member", mention=self.ctx.author.mention), ephemeral=True)
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
