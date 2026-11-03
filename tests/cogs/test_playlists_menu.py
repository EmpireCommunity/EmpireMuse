"""Menu de playlists do servidor no ``/play`` (``play_guild_menu``). O modo servidor do ``/fav_manager`` não existe mais
(playlists por escopo, item 8: a gestão é por ``/jukebox playlists`` e ``/radio playlists``, e pelo painel)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.rules.playlists import Playlist


@pytest.mark.regression
@pytest.mark.asyncio
async def test_a_genre_named_like_the_all_option_is_a_real_genre_not_the_reset():
    from empiremuse.music_cog.play_guild_menu import ALL_GENRES, GuildPlaylistSelect

    def playlist(name, genre):
        return Playlist(0, name, "", (genre,), f"https://ok.exemplo/{name.lower()}", None)

    bot = SimpleNamespace(pool=SimpleNamespace(), music=None)
    view = GuildPlaylistSelect(
        SimpleNamespace(id=1), bot, (playlist("Rock", "Rock"), playlist("Todos", ALL_GENRES)), timeout=5
    )
    assert ALL_GENRES in view.genres  # o gênero existe e é filtrável
    interaction = SimpleNamespace(
        data=SimpleNamespace(values=[str(view.genres.index(ALL_GENRES))]),
        response=SimpleNamespace(edit_message=AsyncMock(), defer=AsyncMock()),
    )

    await view.genre_callback(interaction)

    assert view.genre == ALL_GENRES  # filtrou por ele, não voltou para "Todos"
    assert [o.label for o in view.item_pages[0]] == ["Todos"]
