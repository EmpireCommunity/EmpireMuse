# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from io import BytesIO

import disnake

from empiremuse.utils.music.interactions.view_modal import (
    ViewModal,
)


def _nested_text(data: dict, *keys: str) -> str:
    """Lê ``data[k1][k2]...``; devolve ``""`` se alguma chave faltar."""
    try:
        for key in keys:
            data = data[key]
    except KeyError:
        return ""
    return data


class SkinEditorModalsMixin:
    """Callbacks dos botões do editor que abrem modais (ou exportam) a partir do estado atual da skin; textos pelo ``self.tr``."""

    async def edit_content(self, inter: disnake.MessageInteraction):
        self.ctx = inter
        await inter.response.send_modal(
            ViewModal(
                view=self,
                title=self.tr("skin_editor.modal.content.modal_title"),
                custom_id="skin_editor_message_content",
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.long,
                        label=self.tr("skin_editor.modal.content.message.label"),
                        custom_id="message_content",
                        value=self.message_data.get("content", ""),
                        max_length=1700,
                        required=False,
                    ),
                ],
            )
        )

    async def add_embed(self, inter: disnake.MessageInteraction):
        self.ctx = inter

        await inter.response.send_modal(
            ViewModal(
                view=self,
                title=self.tr("skin_editor.modal.add_embed.modal_title"),
                custom_id="skin_editor_add_embed",
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label=self.tr("skin_editor.modal.embed.title.label"),
                        custom_id="skin_embed_title",
                        max_length=170,
                        required=False,
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.long,
                        label=self.tr("skin_editor.modal.embed.description.label"),
                        custom_id="skin_embed_description",
                        max_length=1700,
                        required=True,
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label=self.tr("skin_editor.modal.embed.color.label"),
                        placeholder=self.tr(
                            "skin_editor.modal.embed.color.placeholder", color_placeholder="{guild.color}"
                        ),
                        custom_id="skin_embed_color",
                        max_length=15,
                        required=False,
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label=self.tr("skin_editor.modal.embed.image.label"),
                        custom_id="image_url",
                        max_length=400,
                        required=False,
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label=self.tr("skin_editor.modal.embed.thumbnail.label"),
                        custom_id="thumbnail_url",
                        max_length=400,
                        required=False,
                    ),
                ],
            )
        )

    async def edit_embed_button(self, inter: disnake.MessageInteraction):
        self.ctx = inter

        embed = self.message_data["embeds"][self.embed_index]

        image_url = _nested_text(embed, "image", "url")
        thumb_url = _nested_text(embed, "thumbnail", "url")

        await inter.response.send_modal(
            ViewModal(
                view=self,
                title=self.tr("skin_editor.modal.edit_embed.modal_title"),
                custom_id="skin_editor_edit_embed",
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label=self.tr("skin_editor.modal.embed.title.label"),
                        custom_id="skin_embed_title",
                        value=embed.get("title", ""),
                        max_length=170,
                        required=False,
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.long,
                        label=self.tr("skin_editor.modal.embed.description.label"),
                        custom_id="skin_embed_description",
                        value=embed.get("description", ""),
                        max_length=1700,
                        required=True,
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label=self.tr("skin_editor.modal.embed.color.label"),
                        placeholder=self.tr(
                            "skin_editor.modal.embed.color.placeholder", color_placeholder="{guild.color}"
                        ),
                        custom_id="skin_embed_color",
                        value=str(embed.get("color", "")),
                        max_length=14,
                        required=False,
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label=self.tr("skin_editor.modal.embed.image.label"),
                        custom_id="image_url",
                        value=image_url,
                        max_length=400,
                        required=False,
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label=self.tr("skin_editor.modal.embed.thumbnail.label"),
                        custom_id="thumbnail_url",
                        value=thumb_url,
                        max_length=400,
                        required=False,
                    ),
                ],
            )
        )

    async def remove_embed(self, inter: disnake.MessageInteraction):
        self.ctx = inter
        del self.message_data["embeds"][self.embed_index]
        self.embed_index = 0
        self.update_components()
        await inter.response.edit_message(view=self, **self.build_embeds())

    async def add_field(self, inter: disnake.MessageInteraction):
        self.ctx = inter
        await inter.response.send_modal(
            ViewModal(
                view=self,
                title=self.tr("skin_editor.modal.add_field.modal_title"),
                custom_id="skin_editor_add_field",
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label=self.tr("skin_editor.modal.field.name.label"),
                        custom_id="add_field_name",
                        max_length=170,
                        required=True,
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.long,
                        label=self.tr("skin_editor.modal.field.value.label"),
                        custom_id="add_field_value",
                        max_length=1700,
                        required=True,
                    ),
                ],
            )
        )

    async def edit_embed_field_button(self, inter: disnake.MessageInteraction):
        self.ctx = inter

        field = self.message_data["embeds"][self.embed_index]["fields"][self.embed_field_index]

        await inter.response.send_modal(
            ViewModal(
                view=self,
                title=self.tr("skin_editor.modal.edit_field.modal_title"),
                custom_id="skin_editor_edit_field",
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label=self.tr("skin_editor.modal.field.edit_name.label"),
                        custom_id="edit_field_name",
                        value=field["name"],
                        max_length=170,
                        required=True,
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.long,
                        label=self.tr("skin_editor.modal.field.edit_value.label"),
                        custom_id="edit_field_value",
                        value=field["value"],
                        max_length=1700,
                        required=True,
                    ),
                ],
            )
        )

    async def delete_embed_field_button(self, inter: disnake.MessageInteraction):
        self.ctx = inter
        del self.message_data["embeds"][self.embed_index]["fields"][self.embed_field_index]
        self.embed_field_index = 0
        self.update_components()
        await inter.response.edit_message(view=self, **self.build_embeds())

    async def set_author_footer(self, inter: disnake.MessageInteraction):
        self.ctx = inter

        embed = self.message_data["embeds"][self.embed_index]
        author_name = _nested_text(embed, "author", "name")
        author_url = _nested_text(embed, "author", "url")
        author_icon_url = _nested_text(embed, "author", "icon_url")
        footer_text = _nested_text(embed, "footer", "text")
        footer_icon_url = _nested_text(embed, "footer", "icon_url")

        await inter.response.send_modal(
            ViewModal(
                view=self,
                custom_id="skin_editor_set_authorfooter",
                title=self.tr("skin_editor.modal.author_footer.modal_title"),
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label=self.tr("skin_editor.modal.author.name.label"),
                        custom_id="set_author_name",
                        value=author_name,
                        max_length=170,
                        required=False,
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label=self.tr("skin_editor.modal.author.url.label"),
                        custom_id="set_author_url",
                        value=author_url,
                        max_length=400,
                        required=False,
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label=self.tr("skin_editor.modal.author.icon.label"),
                        custom_id="set_author_icon",
                        value=author_icon_url,
                        max_length=400,
                        required=False,
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.long,
                        label=self.tr("skin_editor.modal.footer.text.label"),
                        custom_id="footer_text",
                        value=footer_text,
                        max_length=1700,
                        required=False,
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label=self.tr("skin_editor.modal.footer.icon.label"),
                        custom_id="footer_icon_url",
                        value=footer_icon_url,
                        max_length=400,
                        required=False,
                    ),
                ],
            )
        )

    async def setup_queue(self, inter: disnake.MessageInteraction):
        self.ctx = inter

        await inter.response.send_modal(
            ViewModal(
                view=self,
                title=self.tr("skin_editor.modal.queue.modal_title"),
                custom_id="skin_editor_setup_queue",
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label=self.tr("skin_editor.modal.queue.format.label"),
                        custom_id="queue_format",
                        value=self.message_data["queue_format"],
                        max_length=120,
                        required=True,
                    ),
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label=self.tr("skin_editor.modal.queue.max_entries.label"),
                        custom_id="queue_max_entries",
                        value=str(self.message_data["queue_max_entries"]),
                        max_length=2,
                        required=True,
                    ),
                ],
            )
        )

    async def export(self, inter: disnake.MessageInteraction):
        self.ctx = inter
        fp = BytesIO(bytes(json.dumps(self.message_data, indent=4), "utf-8"))
        await inter.response.send_message(file=disnake.File(fp=fp, filename="skin.json"), ephemeral=True)

    async def import_(self, inter: disnake.MessageInteraction):
        self.ctx = inter
        await inter.response.send_modal(
            ViewModal(
                view=self,
                title=self.tr("skin_editor.modal.import.modal_title"),
                custom_id="skin_editor_import_skin",
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.long,
                        label=self.tr("skin_editor.modal.import.code.label"),
                        custom_id="skin",
                        max_length=2000,
                        required=True,
                    )
                ],
            )
        )

    async def save(self, inter: disnake.MessageInteraction):
        self.ctx = inter
        await inter.response.send_modal(
            ViewModal(
                view=self,
                title=self.tr("skin_editor.modal.save.modal_title"),
                custom_id="skin_editor_save",
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label=self.tr("skin_editor.modal.save.name.label"),
                        custom_id="skin_name",
                        value=self.skin_selected.replace("> css: ", "", 1).replace("> cs: ", "", 1),
                        max_length=15,
                        required=True,
                    )
                ],
            )
        )
