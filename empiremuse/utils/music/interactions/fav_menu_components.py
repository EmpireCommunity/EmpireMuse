# -*- coding: utf-8 -*-
from __future__ import annotations

import disnake

from empiremuse.rules.playlists import may_manage, select_description
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

        if self.mode == ViewMode.fav_manager:
            self._add_fav_select()

        elif self.mode == ViewMode.guild_fav_manager and self.guild:
            self._add_guild_fav_selects()

        elif self.mode == ViewMode.integrations_manager:
            self._add_integration_select()

        self._add_button("Adicionar", "➕", self.add_callback)

        self._add_manage_buttons()

        self._add_button("Importar", "📥", self.import_callback)

        self._add_play_button()

        self._add_button("Fechar", "❌", self.cancel_callback)

    def _can_manage_playlists(self) -> bool:
        """Mostra o modo do servidor só a quem pode gerenciá-lo (sem dado do membro, não mostra); cada escrita rechecará."""
        perms = getattr(self.ctx.author, "guild_permissions", None)
        if perms is None:
            return False
        return may_manage(self.guild.owner_id == self.ctx.author.id, perms.administrator, perms.manage_guild)

    def _add_button(self, label: str, emoji: str, callback, **kwargs):
        button = disnake.ui.Button(label=label, emoji=emoji, **kwargs)
        button.callback = callback
        self.add_item(button)

    def _add_mode_select(self):
        mode_select = disnake.ui.Select(
            options=[
                disnake.SelectOption(
                    label="Gerenciador de Favoritos",
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
                label="Gerenciador de Integrações",
                value=f"fav_view_mode_{ViewMode.integrations_manager}",
                emoji="💠",
                default=self.mode == ViewMode.integrations_manager,
            )
        )

        if self.guild and self._can_manage_playlists():
            mode_select.options.insert(
                1,
                disnake.SelectOption(
                    label="Gerenciador de Playlists do Servidor",
                    value=f"fav_view_mode_{ViewMode.guild_fav_manager}",
                    emoji="📌",
                    default=self.mode == ViewMode.guild_fav_manager,
                ),
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

    def _add_guild_fav_selects(self):
        """Select das playlists do servidor (as 25 primeiras por ordem); o valor é o nome, re-resolvido no store."""
        if not self.playlists:
            return

        fav_select = disnake.ui.Select(
            options=[
                disnake.SelectOption(
                    label=item.name,
                    value=item.name,
                    emoji=item.icon or music_source_emoji_url(item.url, self.bot)[0],
                    description=select_description(item.description) or None,
                )
                for item in self.playlists[:MAX_SELECT_OPTIONS]
            ],
            min_values=1,
            max_values=1,
        )
        fav_select.options[0].default = True
        self.current = fav_select.options[0].value
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
            self._add_button("Editar", "✍️", self.edit_callback, disabled=no_links)
            self._add_button("Remover", "♻️", self.remove_callback, disabled=no_links)
            self._add_button("Limpar favoritos", "🚮", self.clear_callback, disabled=no_links)
            self._add_button("Exportar", "📤", self.export_callback, disabled=no_links)

        elif self.mode == ViewMode.guild_fav_manager:
            no_links = not self.playlists
            self._add_button("Editar", "✍️", self.edit_callback, disabled=no_links)
            self._add_button("Remover", "♻️", self.remove_callback, disabled=no_links)
            self._add_button("Limpar playlists", "🚮", self.clear_callback, disabled=no_links)
            self._add_button("Exportar", "📤", self.export_callback, disabled=no_links)

        elif self.mode == ViewMode.integrations_manager:
            no_links = not self.data["integration_links"]
            self._add_button("Remover", "♻️", self.remove_callback, disabled=no_links)
            self._add_button("Limpar Integrações", "🚮", self.clear_callback, disabled=no_links)
            self._add_button("Exportar", "📤", self.export_callback, disabled=no_links)

    def _add_play_button(self):
        if self.mode == ViewMode.fav_manager:
            if self.data["fav_links"] and not self.light_mode:
                self._add_button(
                    "Tocar o favorito selecionado", "▶", self.play_callback, custom_id="favmanager_play_button"
                )

        elif self.mode == ViewMode.integrations_manager:
            if self.data["integration_links"]:
                self._add_button(
                    "Tocar uma playlist da integração selecionada",
                    "▶",
                    self.play_callback,
                    custom_id="favmanager_play_button",
                )
