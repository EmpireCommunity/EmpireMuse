# -*- coding: utf-8 -*-
"""Menu de ``/play`` com as playlists do servidor: um select de gênero ("Todos" + até 24) acima do select de playlists, que filtra.

Estende a ``SelectInteraction`` (paginada, só quem pediu interage). O gênero escolhido vem de um select do Discord e é conferido contra a lista
de gêneros desta tela; o valor de cada playlist (``> pin: <nome>``) é resolvido de novo no store por quem trata a escolha.
"""

from __future__ import annotations

from collections.abc import Sequence

import disnake

from empiremuse.rules.playlists import Playlist, filter_by_genre, genres_of, select_description
from empiremuse.utils.music.converters import fix_characters
from empiremuse.utils.music.interactions import SelectInteraction

ALL_GENRES = "all"  # os gêneros usam o índice como valor, então nenhum gênero (qualquer texto) colide com este
MAX_GENRE_OPTIONS = 24  # "Todos" + 24 = 25, o teto de um select
FALLBACK_DESCRIPTION = "[📌 Favorito do servidor 📌]"


class GuildPlaylistSelect(SelectInteraction):
    def __init__(self, user: disnake.Member, bot, playlists: Sequence[Playlist], *, timeout: float = 75) -> None:
        from empiremuse.utils.others import music_source_emoji_url  # tarde: utils.others importa muita coisa

        self.bot = bot
        self.playlists = tuple(playlists)
        self.genre: str | None = None
        self.genres = genres_of(self.playlists)[:MAX_GENRE_OPTIONS]
        self._emoji = lambda url: music_source_emoji_url(url, bot)[0]
        super().__init__(user=user, opts=self._options(), timeout=timeout)

    def _options(self) -> list[disnake.SelectOption]:
        return [
            disnake.SelectOption(
                label=fix_characters(p.name, 45),
                value=f"> pin: {p.name}",
                description=select_description(p.description) or FALLBACK_DESCRIPTION,
                emoji=p.icon or self._emoji(p.url),
            )
            for p in filter_by_genre(self.playlists, self.genre)
        ]

    def load_components(self) -> None:
        self.clear_items()

        if self.genres:
            genre_select = disnake.ui.Select(
                placeholder="Filtrar por gênero:",
                options=[
                    disnake.SelectOption(label="Todos", value=ALL_GENRES, default=self.genre is None),
                    *(
                        disnake.SelectOption(label=g, value=str(index), default=g == self.genre)
                        for index, g in enumerate(self.genres)
                    ),
                ],
                row=0,
            )
            genre_select.callback = self.genre_callback
            self.add_item(genre_select)

        select_menu = disnake.ui.Select(
            placeholder="Selecione uma opção:", options=self.item_pages[self.current_page], row=1
        )
        select_menu.callback = self.callback
        self.add_item(select_menu)
        self.selected = self.item_pages[self.current_page][0].value

        if len(self.item_pages) > 1:
            back_button = disnake.ui.Button(emoji="⬅", row=2)
            if self.current_page == 0:
                back_button.disabled = True
            else:
                back_button.label = str(self.current_page)
            back_button.callback = self.back_callback
            self.add_item(back_button)

            next_button = disnake.ui.Button(emoji="➡", row=2)
            if self.current_page == self.max_page:
                next_button.disabled = True
                next_button.label = f"{self.current_page + 1} / {self.max_page + 1}"
            else:
                next_button.label = f"{self.current_page + 2} / {self.max_page + 1}"
            next_button.callback = self.next_callback
            self.add_item(next_button)

        cancel = disnake.ui.Button(label="Cancelar", emoji="❌", row=2)
        cancel.callback = self.cancel_callback
        self.add_item(cancel)

    async def genre_callback(self, interaction: disnake.MessageInteraction) -> None:
        value = interaction.data.values[0]
        if value == ALL_GENRES:
            genre = None
        elif value.isdigit() and int(value) < len(self.genres):
            genre = self.genres[int(value)]
        else:
            await interaction.response.defer()  # valor que não é desta tela: ignora
            return
        self.genre = genre
        self.item_pages = list(disnake.utils.as_chunks(self._options(), 25))
        self.current_page = 0
        self.max_page = len(self.item_pages) - 1
        self.load_components()
        await interaction.response.edit_message(view=self)
