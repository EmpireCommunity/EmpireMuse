# -*- coding: utf-8 -*-
from __future__ import annotations

import disnake

from empiremuse.rules.i18n import fit
from empiremuse.utils.others import music_source_emoji_url
from empiremuse.utils.music.interactions.common import (
    ViewMode,
)

# O Discord limita um select a 25 opções.
MAX_SELECT_OPTIONS = 25


class FavMenuComponentsMixin:
    """Montagem dos componentes (selects e botões) do gerenciador de favoritos, conforme o modo atual."""

    def update_components(self):
        self.clear_items()

        if not self.light_mode:
            self._add_mode_select()

        if self.mode == ViewMode.radio_favorites:
            self.add_radio_components()  # só Remover e Limpar; sem Adicionar, Importar nem Tocar (fav_menu_radio)
            return

        if self.mode == ViewMode.fav_manager:
            self._add_fav_select()

        elif self.mode == ViewMode.integrations_manager:
            self._add_integration_select()

        self._add_button(self.tr("fav.add.label"), "➕", self.add_callback)

        self._add_manage_buttons()

        self._add_button(self.tr("fav.import.label"), "📥", self.import_callback)

        self._add_play_button()

        self._add_button(self.tr("fav.close.label"), "❌", self.cancel_callback)

    def _add_button(self, label: str, emoji: str, callback, **kwargs):
        button = disnake.ui.Button(label=fit(label, 80), emoji=emoji, **kwargs)
        button.callback = callback
        self.add_item(button)

    def _add_mode_select(self):
        mode_select = disnake.ui.Select(
            options=[
                disnake.SelectOption(
                    label=fit(self.tr("fav.mode.favorites.label"), 100),
                    value=f"fav_view_mode_{ViewMode.fav_manager}",
                    emoji="⭐",
                    default=self.mode == ViewMode.fav_manager,
                )
            ],
            min_values=1,
            max_values=1,
        )

        mode_select.append_option(
            disnake.SelectOption(
                label=fit(self.tr("fav.mode.integrations.label"), 100),
                value=f"fav_view_mode_{ViewMode.integrations_manager}",
                emoji="💠",
                default=self.mode == ViewMode.integrations_manager,
            )
        )

        if self.radio_available():
            mode_select.append_option(
                disnake.SelectOption(
                    label=fit(self.tr("radio.favorites.mode.label"), 100),
                    value=f"fav_view_mode_{ViewMode.radio_favorites}",
                    emoji="📻",
                    default=self.mode == ViewMode.radio_favorites,
                )
            )

        if len(mode_select.options) < 2:
            mode_select.disabled = True

        mode_select.callback = self.mode_callback
        self.add_item(mode_select)

    def _add_fav_select(self):
        if not self.data["fav_links"]:
            return

        opts = []
        for k, v in list(self.data["fav_links"].items())[
            :MAX_SELECT_OPTIONS
        ]:  # TODO: Lidar depois com os dados existentes que excedem a quantidade permitida
            emoji, platform = music_source_emoji_url(v, self.bot)
            opts.append(disnake.SelectOption(label=k, emoji=emoji, description=platform))
        fav_select = disnake.ui.Select(options=opts, min_values=1, max_values=1)
        fav_select.options[0].default = True
        self.current = fav_select.options[0].label
        fav_select.callback = self.select_callback
        self.add_item(fav_select)

    def _add_integration_select(self):
        if not self.data["integration_links"]:
            return

        opts = []
        for k, v in list(self.data["integration_links"].items())[
            :MAX_SELECT_OPTIONS
        ]:  # TODO: Lidar depois com os dados existentes que excedem a quantidade permitida
            if isinstance(v, dict):
                v = v["url"]
            emoji, platform = music_source_emoji_url(v, self.bot)
            opts.append(disnake.SelectOption(label=k[5:], emoji=emoji, description=platform, value=k))
        integration_select = disnake.ui.Select(options=opts, min_values=1, max_values=1)
        integration_select.options[0].default = True
        self.current = integration_select.options[0].value
        integration_select.callback = self.select_callback
        self.add_item(integration_select)

    def _add_manage_buttons(self):
        if self.mode == ViewMode.fav_manager:
            no_links = not self.data["fav_links"]
            self._add_button(self.tr("fav.edit.label"), "✍️", self.edit_callback, disabled=no_links)
            self._add_button(self.tr("fav.remove.label"), "♻️", self.remove_callback, disabled=no_links)
            self._add_button(self.tr("fav.clear.label"), "🚮", self.clear_callback, disabled=no_links)
            self._add_button(self.tr("fav.export.label"), "📤", self.export_callback, disabled=no_links)

        elif self.mode == ViewMode.integrations_manager:
            no_links = not self.data["integration_links"]
            self._add_button(self.tr("fav.remove.label"), "♻️", self.remove_callback, disabled=no_links)
            self._add_button(self.tr("fav.itg.clear.label"), "🚮", self.clear_callback, disabled=no_links)
            self._add_button(self.tr("fav.export.label"), "📤", self.export_callback, disabled=no_links)

    def _add_play_button(self):
        if self.mode == ViewMode.fav_manager:
            if self.data["fav_links"] and not self.light_mode:
                self._add_button(self.tr("fav.play.label"), "▶", self.play_callback, custom_id="favmanager_play_button")

        elif self.mode == ViewMode.integrations_manager:
            if self.data["integration_links"]:
                self._add_button(
                    self.tr("fav.itg.play.label"),
                    "▶",
                    self.play_callback,
                    custom_id="favmanager_play_button",
                )
