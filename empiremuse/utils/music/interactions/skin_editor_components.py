# -*- coding: utf-8 -*-
from __future__ import annotations

import disnake
from empiremuse.utils.music.app_emojis import emoji as app_emoji

# Limite de fields por embed e de embeds por mensagem aceitos pelo Discord.
MAX_EMBED_FIELDS = 25
MAX_EMBEDS = 8
# Tamanho mínimo do conteúdo para salvar/exportar uma skin sem embeds.
MIN_CONTENT_LENGTH = 15

NO_SKINS_TEXT = "**Não há skins salvas...\nClique no botão abaixo para criar uma nova skin/template.**"
PICK_SKIN_TEXT = "**Selecione uma skin abaixo para editá-la ou crie uma nova usando um modelo base clicando no botão de adicionar abaixo.**"


class SkinEditorComponentsMixin:
    """Montagem dos componentes (selects e botões) do editor de skins, conforme o modo atual."""

    def update_components(self):
        self.clear_items()

        if self.mode == "select":
            self._add_skin_picker_components()

        elif self.mode == "editor":
            self._add_editor_components()

    def _add_skin_picker_components(self):
        add_skin_prefix = lambda d: [f"> cs: {i}" for i in d.keys()]
        skins_opts = [
            disnake.SelectOption(emoji="💠", label=f"Modo normal: {s.replace('> cs: ', '', 1)}", value=s)
            for s in add_skin_prefix(self.global_data["custom_skins"])
        ]
        add_skin_prefix = lambda d: [f"> css: {i}" for i in d.keys()]
        static_skins_opts = [
            disnake.SelectOption(emoji="💠", label=f"Jukebox: {s.replace('> css: ', '', 1)}", value=s)
            for s in add_skin_prefix(self.global_data["custom_skins_static"])
        ]

        if skins_opts:
            skin_select = disnake.ui.Select(
                min_values=1, max_values=1, options=skins_opts, placeholder="Skins do modo normal do player"
            )
            skin_select.callback = self.load_skin
            self.add_item(skin_select)

        if static_skins_opts:
            static_skin_select = disnake.ui.Select(
                min_values=1,
                max_values=1,
                options=static_skins_opts,
                placeholder="Skins do modo Jukebox do player",
            )
            static_skin_select.callback = self.load_skin
            self.add_item(static_skin_select)

        has_skins = bool(skins_opts or static_skins_opts)

        self.message_data = {
            "embeds": [
                {
                    "description": PICK_SKIN_TEXT if has_skins else NO_SKINS_TEXT,
                    "color": self.guild.me.color.value,
                }
            ]
        }
        new_skin_btn = disnake.ui.Button(
            label="Adicionar nova skin",
            custom_id="skin_editor_new_skin",
            disabled=len(static_skins_opts) > 2 and len(skins_opts) > 2,
        )
        new_skin_btn.callback = self.new_skin
        self.add_item(new_skin_btn)

    def _add_editor_components(self):
        embeds = self.message_data.get("embeds")

        if embeds:
            self._add_embed_components(embeds)

        self._add_message_components(embeds)

    def _add_embed_components(self, embeds: list):
        select_embed = disnake.ui.Select(
            min_values=1,
            max_values=1,
            options=[
                disnake.SelectOption(label=f"Embed {n + 1}", value=f"skin_embed_{n}", default=n == self.embed_index)
                for n, e in enumerate(embeds)
            ],
        )

        select_embed.callback = self.embed_select_callback
        self.add_item(select_embed)

        self._add_field_components(embeds[self.embed_index].get("fields", []))

        edit_embed_btn = disnake.ui.Button(label="Editar Embed", emoji="📋")
        edit_embed_btn.callback = self.edit_embed_button
        self.add_item(edit_embed_btn)

        remove_embed_btn = disnake.ui.Button(label="Remover Embed", emoji="📋")
        remove_embed_btn.callback = self.remove_embed
        self.add_item(remove_embed_btn)

        set_author_footer_btn = disnake.ui.Button(label="Embed Author + Footer", emoji="👤")
        set_author_footer_btn.callback = self.set_author_footer
        self.add_item(set_author_footer_btn)

    def _add_field_components(self, fields: list):
        if fields:
            select_embed_field = disnake.ui.Select(
                min_values=1,
                max_values=1,
                options=[
                    disnake.SelectOption(
                        label=f"Field {n + 1}",
                        value=f"skin_embed_field_{n}",
                        default=n == self.embed_field_index,
                    )
                    for n, e in enumerate(fields)
                ],
            )
            select_embed_field.callback = self.embed_value_select_callback
            self.add_item(select_embed_field)

        if len(fields) < MAX_EMBED_FIELDS:
            add_field_btn = disnake.ui.Button(label="Adicionar Field", emoji="🔖")
            add_field_btn.callback = self.add_field
            self.add_item(add_field_btn)

        if not fields:
            return

        edit_field_btn = disnake.ui.Button(label="Editar Field", emoji="🔖")
        edit_field_btn.callback = self.edit_embed_field_button
        self.add_item(edit_field_btn)

        delete_field_btn = disnake.ui.Button(label="Remover Field", emoji="🔖")
        delete_field_btn.callback = self.delete_embed_field_button
        self.add_item(delete_field_btn)

    def _add_message_components(self, embeds: list | None):
        edit_content_btn = disnake.ui.Button(
            label=("Adicionar" if not self.message_data.get("content") else "Editar") + " Mensagem", emoji="💬"
        )
        edit_content_btn.callback = self.edit_content
        self.add_item(edit_content_btn)

        add_embed_btn = disnake.ui.Button(label="Adicionar Embed", disabled=len(embeds) >= MAX_EMBEDS, emoji="📋")
        add_embed_btn.callback = self.add_embed
        self.add_item(add_embed_btn)

        setup_queue_btn = disnake.ui.Button(
            label="Configurar placeholder da fila", emoji=app_emoji(self.bot, "music_queue")
        )
        setup_queue_btn.callback = self.setup_queue
        self.add_item(setup_queue_btn)

        save_disabled = not embeds and len(self.message_data.get("content", "")) < MIN_CONTENT_LENGTH

        export_btn = disnake.ui.Button(label="Exportar Skin", emoji="📤", disabled=save_disabled)
        export_btn.callback = self.export
        self.add_item(export_btn)

        import_btn = disnake.ui.Button(label="Importar Skin", emoji="📥")
        import_btn.callback = self.import_
        self.add_item(import_btn)

        if self.skin_selected:
            delete_skin_btn = disnake.ui.Button(label="Excluir Skin", emoji="🚮")
            delete_skin_btn.callback = self.delete_skin
            self.add_item(delete_skin_btn)

        back_btn = disnake.ui.Button(label="Voltar ao menu anterior", emoji=app_emoji(self.bot, "arrow_left"))
        back_btn.callback = self.back
        self.add_item(back_btn)

        self.add_item(
            disnake.ui.Button(
                label="Lista de placeholders", emoji=app_emoji(self.bot, "help"), custom_id="skin_editor_placeholders"
            )
        )

        save_btn = disnake.ui.Button(label="Salvar Skin", emoji="💾", disabled=save_disabled)
        save_btn.callback = self.save
        self.add_item(save_btn)
