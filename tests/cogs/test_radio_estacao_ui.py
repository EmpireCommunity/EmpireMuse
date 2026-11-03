"""O CRUD de estações no Discord (``station_ui.py``): a autorização de cada clique (autor, ``manage_guild`` e premium AGORA) e o fluxo criar por gênero até gravar."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.i18n import get_i18n
from empiremuse.utils.radio import RadioHub
from empiremuse.utils.radio.station_ui import ConfirmDelete, StationEditor
from tests.persistence.test_radio_store import FakeDb

pytestmark = pytest.mark.critical

GUILD, AUTHOR, STRANGER = 500, 7, 99


def tr():
    return get_i18n().for_locale("pt-BR")


def make_hub(*, premium=True):
    hub = RadioHub(SimpleNamespace(database=FakeDb()))
    hub.premium.get = AsyncMock(return_value=object() if premium else None)
    return hub


def click(user_id=AUTHOR, values=None, manage_guild=True):
    return SimpleNamespace(
        author=SimpleNamespace(id=user_id, guild_permissions=SimpleNamespace(manage_guild=manage_guild)),
        guild_id=GUILD,
        data=SimpleNamespace(values=values or []),
        response=SimpleNamespace(
            send_message=AsyncMock(), edit_message=AsyncMock(), defer=AsyncMock(), send_modal=AsyncMock()
        ),
        edit_original_message=AsyncMock(),
    )


def allow(interaction):
    return bool(interaction.author.guild_permissions.manage_guild)


def editor(hub, **over):
    args = dict(
        author_id=AUTHOR, guild_id=GUILD, playlists=["Rock"], station=None, name="Minha", kind="genre", can_manage=allow
    )
    return StationEditor(hub, tr(), **{**args, **over})


class TestEveryClickIsAuthorizedNow:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "make",
        [
            lambda hub: editor(hub),
            lambda hub: ConfirmDelete(hub, tr(), author_id=AUTHOR, station=SimpleNamespace(), can_manage=allow),
        ],
    )
    async def test_another_author_is_refused_ephemerally_and_nothing_is_saved(self, make):
        hub = make_hub()
        view = make(hub)
        stranger = click(user_id=STRANGER)

        assert await view.interaction_check(stranger) is False

        stranger.response.send_message.assert_awaited_once()
        assert stranger.response.send_message.await_args.kwargs == {"ephemeral": True}
        assert await hub.stations.list_for_guild(GUILD) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "make",
        [
            lambda hub: editor(hub),
            lambda hub: ConfirmDelete(hub, tr(), author_id=AUTHOR, station=SimpleNamespace(), can_manage=allow),
        ],
    )
    async def test_the_right_author_who_lost_manage_guild_is_refused(self, make):
        view = make(make_hub())
        demoted = click(manage_guild=False)

        assert await view.interaction_check(demoted) is False

        assert "Gerenciar Servidor" in demoted.response.send_message.await_args.args[0]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "make",
        [
            lambda hub: editor(hub),
            lambda hub: ConfirmDelete(hub, tr(), author_id=AUTHOR, station=SimpleNamespace(), can_manage=allow),
        ],
    )
    async def test_premium_revoked_while_the_screen_was_open_is_refused(self, make):
        view = make(make_hub(premium=False))
        press = click()

        assert await view.interaction_check(press) is False

        assert "premium" in press.response.send_message.await_args.args[0]

    @pytest.mark.asyncio
    async def test_the_right_author_with_the_right_permissions_passes(self):
        press = click()

        assert await editor(make_hub()).interaction_check(press) is True
        press.response.send_message.assert_not_awaited()


class TestCreatingAGenreStation:
    @pytest.mark.asyncio
    async def test_picking_kind_group_and_genre_then_saving_creates_the_station(self):
        hub = make_hub()
        view = editor(hub)
        group = next(iter(hub.stations.genre_groups()))[0]
        genre = next(g for g in hub.stations.genres() if g.group == group)

        await view._on_kind(click(values=["genre"]))
        await view._on_group(click(values=[group]))
        await view._on_genre(click(values=[genre.id]))
        save = click()
        await view._on_save(save)

        stations = await hub.stations.list_for_guild(GUILD)
        assert [(s.name, s.kind, s.source) for s in stations] == [("Minha", "genre", {"genre": genre.id})]
        assert stations[0].updated_by == str(AUTHOR)
        sent = save.edit_original_message.await_args.kwargs
        assert "Minha" in sent["content"] and sent["view"] is None  # a tela fecha dizendo o nome salvo

    @pytest.mark.asyncio
    async def test_a_genre_the_select_never_offered_is_ignored_and_saving_without_a_source_writes_nothing(self):
        hub = make_hub()
        view = editor(hub)
        group = next(iter(hub.stations.genre_groups()))[0]
        other_group_genre = next(g for g in hub.stations.genres() if g.group != group)
        await view._on_group(click(values=[group]))

        await view._on_genre(click(values=[other_group_genre.id]))  # forjado: de outro grupo
        await view._on_genre(click(values=["nao_existe"]))
        await view._on_save(click())

        assert view.genre is None and await hub.stations.list_for_guild(GUILD) == []

    @pytest.mark.asyncio
    async def test_an_invalid_station_is_shown_as_an_error_and_not_saved(self):
        hub = make_hub()
        view = editor(hub, name="   ")  # nome vazio: as regras recusam
        group = next(iter(hub.stations.genre_groups()))[0]
        genre = next(g for g in hub.stations.genres() if g.group == group)
        await view._on_group(click(values=[group]))
        await view._on_genre(click(values=[genre.id]))
        save = click()

        await view._on_save(save)

        assert await hub.stations.list_for_guild(GUILD) == []
        assert save.edit_original_message.await_args.kwargs["view"] is view  # a tela fica para corrigir
