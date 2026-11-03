# -*- coding: utf-8 -*-
from __future__ import annotations

import json

import disnake

from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.music.interactions.common import (
    logger,
)

# Máximo de dígitos aceito em `queue_max_entries` numa skin importada; acima disso volta ao padrão.
IMPORT_QUEUE_MAX_ENTRIES_DIGITS = 2
DEFAULT_QUEUE_MAX_ENTRIES = 7
# Chaves do catálogo das recusas (o texto sai pelo ``view.tr``).
QUEUE_MAX_ENTRIES_INVALID = "skin_editor.import.queue_max_entries_invalid"
IMPORT_SHAPE_REFUSED = "skin_editor.import.shape_refused"
IMPORT_INVALID_JSON = "skin_editor.import.invalid_json"
IMPORTED_SKIN_KEYS = ("embeds", "content", "queue_format", "queue_max_entries")


def _set_or_clear(embed: dict, key: str, value: dict | None):
    """Grava ``embed[key]`` ou, se não houver valor, remove a chave (se existir)."""
    if value is None:
        embed.pop(key, None)
        return
    embed[key] = value


def _apply_message_content(view, inter: disnake.ModalInteraction):
    view.message_data["content"] = inter.text_values["message_content"]


def _apply_add_embed(view, inter: disnake.ModalInteraction):
    e = (
        disnake.Embed(
            title=inter.text_values["skin_embed_title"],
            description=inter.text_values["skin_embed_description"],
        )
        .set_image(url=inter.text_values["image_url"])
        .set_thumbnail(inter.text_values["thumbnail_url"])
        .to_dict()
    )

    e["color"] = inter.text_values["skin_embed_color"].strip("#")

    view.message_data["embeds"].append(e)
    view.embed_index = len(view.message_data["embeds"]) - 1


def _apply_edit_embed(view, inter: disnake.ModalInteraction):
    embed = view.message_data["embeds"][view.embed_index]

    embed["title"] = inter.text_values["skin_embed_title"]
    embed["description"] = inter.text_values["skin_embed_description"]

    image_url = inter.text_values["image_url"]
    _set_or_clear(embed, "image", {"url": image_url} if image_url else None)

    thumbnail_url = inter.text_values["thumbnail_url"]
    _set_or_clear(embed, "thumbnail", {"url": thumbnail_url} if thumbnail_url else None)

    embed["color"] = inter.text_values["skin_embed_color"].strip("#")


def _apply_add_field(view, inter: disnake.ModalInteraction):
    embed = view.message_data["embeds"][view.embed_index]
    field = {"name": inter.text_values["add_field_name"], "value": inter.text_values["add_field_value"]}

    if not embed.get("fields"):
        embed["fields"] = [field]
    else:
        embed["fields"].append(field)

    view.embed_field_index = len(embed["fields"]) - 1


def _apply_edit_field(view, inter: disnake.ModalInteraction):
    view.message_data["embeds"][view.embed_index]["fields"][view.embed_field_index] = {
        "name": inter.text_values["edit_field_name"],
        "value": inter.text_values["edit_field_value"],
    }


def _apply_author_footer(view, inter: disnake.ModalInteraction):
    embed = view.message_data["embeds"][view.embed_index]

    footer_text = inter.text_values["footer_text"]
    _set_or_clear(
        embed,
        "footer",
        {"text": footer_text, "icon_url": inter.text_values["footer_icon_url"]} if footer_text else None,
    )

    author_name = inter.text_values["set_author_name"]
    _set_or_clear(
        embed,
        "author",
        {
            "name": author_name,
            "url": inter.text_values["set_author_url"],
            "icon_url": inter.text_values["set_author_icon"],
        }
        if author_name
        else None,
    )


def _apply_queue_setup(view, inter: disnake.ModalInteraction):
    try:
        queue_max_entries = int(inter.text_values["queue_max_entries"])
    except TypeError, ValueError:
        raise GenericError(view.tr(QUEUE_MAX_ENTRIES_INVALID))

    view.message_data["queue_format"] = inter.text_values["queue_format"]
    view.message_data["queue_max_entries"] = queue_max_entries


def _import_shape_is_valid(info: object) -> bool:
    """A skin importada precisa ser um objeto e, se trouxer ``embeds``, uma lista de objetos (``null`` é recusado)."""
    if not isinstance(info, dict):
        return False

    if "embeds" not in info:
        return True

    embeds = info["embeds"]
    return isinstance(embeds, list) and all(isinstance(embed, dict) for embed in embeds)


async def _refuse_import(view, inter: disnake.ModalInteraction, reason: str) -> bool:
    """Recusa a importação sem tocar na skin: log só com o código do motivo (nunca o conteúdo) e aviso ao dono."""
    logger.warning("Editor de skin: importação recusada (%s)", reason)
    await inter.send(view.tr(IMPORT_SHAPE_REFUSED), ephemeral=True)
    return False


async def _apply_import_skin(view, inter: disnake.ModalInteraction) -> bool:
    """Importa o JSON enviado; devolve ``False`` se o conteúdo for inválido (nada muda na skin)."""
    try:
        info = json.loads(inter.text_values["skin"])
    except ValueError, RecursionError:
        logger.exception("Editor de skin: JSON inválido na importação")
        await inter.send(view.tr(IMPORT_INVALID_JSON), ephemeral=True)
        return False

    if not isinstance(info, dict):
        return await _refuse_import(view, inter, "root_not_object")

    try:
        if len(str(info["queue_max_entries"])) > IMPORT_QUEUE_MAX_ENTRIES_DIGITS:
            info["queue_max_entries"] = DEFAULT_QUEUE_MAX_ENTRIES
    except KeyError, TypeError:
        pass

    try:
        if not isinstance(info["queue_format"], str):
            info["queue_format"] = view.message_data["queue_format"]
    except KeyError:
        pass

    if not _import_shape_is_valid(info):
        return await _refuse_import(view, inter, "embeds_shape")

    for key in IMPORTED_SKIN_KEYS:
        try:
            view.message_data[key] = info[key]
        except KeyError:
            pass

    view.embed_index = 0
    view.embed_field_index = 0
    return True


MODAL_APPLIERS = {
    "skin_editor_message_content": _apply_message_content,
    "skin_editor_add_embed": _apply_add_embed,
    "skin_editor_edit_embed": _apply_edit_embed,
    "skin_editor_add_field": _apply_add_field,
    "skin_editor_edit_field": _apply_edit_field,
    "skin_editor_set_authorfooter": _apply_author_footer,
    "skin_editor_setup_queue": _apply_queue_setup,
}


class SkinEditorSubmitMixin:
    """Aplica ao estado da skin o que o usuário enviou nos modais do editor."""

    async def modal_handler(self, inter: disnake.ModalInteraction):
        await self._ensure_translator()
        if inter.custom_id == "skin_editor_save":
            await self.save_skin_from_modal(inter)
            return

        applier = MODAL_APPLIERS.get(inter.custom_id)

        if inter.custom_id == "skin_editor_import_skin":
            self.ctx = inter
            if not await _apply_import_skin(self, inter):
                return

        elif applier:
            self.ctx = inter
            applier(self, inter)

        self.update_components()
        await self.update_message(inter)
