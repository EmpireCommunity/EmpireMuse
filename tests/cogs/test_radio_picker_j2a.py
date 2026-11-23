"""Seletor de estação da J2a (T-434 A2/A3/B1): busca por nome, estrelas pessoal e do servidor e filtros, com os stores de verdade onde há porta."""

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from empiremuse.rules.radio_stations import Station
from empiremuse.utils.radio.custom_favorites import Toggled
from empiremuse.utils.radio.directory import REASON_LIMITED, REASON_UNAVAILABLE, SearchResult
from empiremuse.utils.radio.favorites import FavoriteStation, RadioFavorites
from empiremuse.utils.radio.locations import LocNode, LocOption
from empiremuse.utils.radio.picker import FAVORITES_ID, FILTERS_ID, MINE_ID, SEARCH_ID, RadioPicker
from empiremuse.utils.radio.premium import RadioUnavailable
from empiremuse.utils.radio.user_favorites import UserRadioFavorites

GUILD, AUTHOR = 10, 7
UUID = "8f0f8b0c-3c6a-4d36-9c76-1b2a3c4d5e6f"
OTHER = "11111111-2222-4333-8444-555555555555"


def tr(key, **kwargs):
    return key


class Tree:
    nodes = {
        "r:latam": LocNode("r:latam", "region", "america_latina", None, False),
        "c:BR": LocNode("c:BR", "country", "BR", "r:latam", False),
        "x:1": LocNode("x:1", "city", "Goiania", "c:BR", True),
    }

    def node(self, node_id):
        return self.nodes.get(node_id) if isinstance(node_id, str) else None

    def path(self, node_id):
        chain, found = [], self.node(node_id)
        while found is not None:
            chain.append(found)
            found = self.node(found.parent)
        return chain[::-1]

    def label(self, node, locale):
        return node.name

    def children(self, node_id, locale, *, guild_locale=None):
        return [LocOption("r:latam", "Latam", "", 1)] if node_id is None else []


def hub(**over):
    tree = Tree()
    base = NS(
        locations=tree,
        premium=NS(get=AsyncMock(return_value=object())),
        health=NS(unavailable=lambda uuid: False),
        directory=NS(
            search_result=AsyncMock(return_value=SearchResult([])),
            by_uuid=AsyncMock(return_value=None),
            by_uuid_strict=AsyncMock(return_value=None),
            region=AsyncMock(),
        ),
        favorites=NS(list=AsyncMock(return_value=[]), add_live=AsyncMock(), remove=AsyncMock()),
        user_favorites=NS(
            stations=AsyncMock(return_value=[]),
            filters=AsyncMock(return_value=[]),
            toggle_station=AsyncMock(return_value=Toggled.ADDED),
            toggle_filter=AsyncMock(return_value=Toggled.ADDED),
        ),
    )
    for key, value in over.items():
        setattr(base, key, value)
    return base


def picker(h):
    return RadioPicker(
        h,
        h.locations,
        tr,
        author_id=AUTHOR,
        guild_id=GUILD,
        locale="pt-BR",
        guild_locale=None,
        apply=AsyncMock(return_value="ok"),
        confirm_label="jukebox.radio.picker.confirm",
        check=lambda interaction: True,
        deny_key="player.you_must_join_a_voice_2",
    )


def click(*values, author=AUTHOR, manage=False):
    return NS(
        author=NS(id=author, guild_permissions=NS(manage_guild=manage, administrator=False)),
        data=NS(values=list(values)),
        text_values={},
        response=NS(defer=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock()),
        edit_original_message=AsyncMock(),
        send=AsyncMock(),
    )


def note(interaction):
    return interaction.edit_original_message.await_args.kwargs["content"].splitlines()[-1]


def station(uuid=UUID, name="Rock FM"):
    return Station(uuid, name, "http://x", "MP3", 128, "Brasil")


def top_values(view):
    return [o.value for o in view.children[0].options]


@pytest.mark.critical
@pytest.mark.asyncio
async def test_the_top_brings_search_mine_server_and_filters_in_order_and_hides_empty_or_unavailable_lists():
    h = hub()
    h.user_favorites.stations = AsyncMock(return_value=[FavoriteStation(UUID, "Rock FM")])
    h.favorites.list = AsyncMock(return_value=[FavoriteStation(OTHER, "Jazz")])
    h.user_favorites.filters = AsyncMock(return_value=["c:BR", "c:gone"])
    full = picker(h)
    await full.render()
    assert top_values(full) == [SEARCH_ID, MINE_ID, FAVORITES_ID, FILTERS_ID, "r:latam"]
    assert full._filters == ["c:BR"]  # o nó que a árvore esqueceu sai na leitura

    h.user_favorites.stations = AsyncMock(side_effect=RadioUnavailable)
    h.favorites.list = AsyncMock(return_value=[])
    h.user_favorites.filters = AsyncMock(return_value=[])
    empty = picker(h)
    await empty.render()
    assert top_values(empty) == [SEARCH_ID, "r:latam"]


@pytest.mark.critical
@pytest.mark.asyncio
async def test_a_value_that_was_not_offered_is_expired_and_changes_nothing():
    view = picker(hub())
    await view.render()
    forged = click("x:1")  # um nó que existe, mas não está no select de agora
    await view._on_level(forged)
    assert note(forged) == "radio.local.expired" and view.node_id is None


@pytest.mark.critical
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "key"),
    [
        (SearchResult([], REASON_LIMITED), "jukebox.radio.search.limited"),
        (SearchResult([], REASON_UNAVAILABLE), "radio.owner.unavailable"),
        (SearchResult([]), "jukebox.radio.search.empty"),
    ],
)
async def test_search_notes_when_the_bucket_is_empty_the_directory_is_down_or_nothing_matches(result, key):
    h = hub()
    h.directory.search_result = AsyncMock(return_value=result)
    view = picker(h)
    await view.render()
    submit = click()
    await view._on_search(submit, "rock")
    h.directory.search_result.assert_awaited_once_with("rock", key=GUILD)  # o balde é o do servidor
    assert note(submit) == key and view.list_mode is None


@pytest.mark.critical
@pytest.mark.asyncio
async def test_search_lists_at_most_24_stations_and_only_the_author_may_submit():
    h = hub()
    found = [station(f"{i:08x}-0000-4000-8000-000000000000", f"S{i}") for i in range(30)]
    h.directory.search_result = AsyncMock(return_value=SearchResult(found))
    view = picker(h)
    await view.render()
    stranger = click(author=99)
    await view._on_search(stranger, "rock")
    stranger.response.send_message.assert_awaited_once()
    h.directory.search_result.assert_not_awaited()

    submit = click()
    await view._on_search(submit, "rock")
    assert view.list_mode == "search" and len(view.hits) == 24 and len(view._offered) == 24


@pytest.mark.asyncio
async def test_the_search_entry_opens_the_modal():
    view = picker(hub())
    await view.render()
    pick = click(SEARCH_ID)
    await view._on_level(pick)
    pick.response.send_modal.assert_awaited_once()


@pytest.mark.critical
@pytest.mark.asyncio
async def test_the_personal_star_uses_the_chosen_uuid_and_the_clicker_never_a_component_value():
    h = hub()
    view = picker(h)
    view.chosen = station()
    press = click(OTHER)  # valor forjado no componente
    await view._on_star_mine(press)
    h.user_favorites.toggle_station.assert_awaited_once_with(AUTHOR, UUID)
    assert note(press) == "radio.favorites.mine.added"


@pytest.mark.critical
@pytest.mark.asyncio
async def test_the_server_star_is_hidden_from_members_and_the_store_refuses_a_non_manager_without_writing():
    database = NS(find_doc=AsyncMock(return_value=None), save_doc_cas=AsyncMock(return_value=True))
    member = NS(id=AUTHOR, guild_permissions=NS(manage_guild=False, administrator=False))
    guild = NS(owner_id=1, get_member=lambda user_id: member)
    pool = NS(database=database, get_all_bots=lambda: [NS(get_guild=lambda guild_id: guild)])
    h = hub(favorites=RadioFavorites(pool))
    view = picker(h)
    view.chosen = station()
    await view.render()
    assert "radio.local.favorites" not in [getattr(item, "label", None) for item in view.children]

    view._manager = True  # o botão estava na tela (a permissão caiu depois): quem grava confere o ator
    press = click(manage=False)
    await view._on_star_server(press)
    assert note(press) == "radio.refused.no_manage_guild"
    database.save_doc_cas.assert_not_awaited()
    h.directory.by_uuid_strict.assert_not_awaited()


@pytest.mark.critical
@pytest.mark.asyncio
@pytest.mark.parametrize(("node", "written"), [("c:gone", False), ("c:BR", True)])
async def test_saving_a_filter_writes_only_a_node_the_tree_knows(node, written):
    database = NS(find_doc=AsyncMock(return_value=None), save_doc_cas=AsyncMock(return_value=True))
    pool = NS(
        database=database,
        erasure_tombstones=NS(now=lambda: 0.0, stale=lambda user, read_at: False),
        user_locks=NS(lock=lambda user: _NoLock()),
    )
    h = hub()
    h.user_favorites = UserRadioFavorites(pool, h)
    view = picker(h)
    view.node_id = node
    press = click()
    await view._on_filter_toggle(press)
    assert database.save_doc_cas.await_count == int(written)
    assert note(press) == ("radio.favorites.picker.filter_saved" if written else "radio.local.expired")


@pytest.mark.asyncio
async def test_choosing_one_of_my_filters_opens_that_place():
    h = hub()
    h.user_favorites.filters = AsyncMock(return_value=["c:BR"])
    view = picker(h)
    await view.render()
    await view._on_level(click(FILTERS_ID))
    pick = click("c:BR")
    await view._on_filter(pick)
    assert view.node_id == "c:BR" and view.list_mode is None


class _NoLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False
