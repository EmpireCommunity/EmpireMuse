"""B9 · rádios favoritas, os armazéns (contrato T-456 rev. 9 §4 ``[r9-fav]`` e §14 ``[r9]``): o teto soma ao vivo, ``custom`` e ``genre``; o diretório só é
consultado numa adição nova e válida; todo escritor preserva a outra lista; nenhuma gravação leva id de usuário; adicionar no armazém do usuário e do membro
respeita formato, já está, cheia e existência, nessa ordem."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.utils.radio import RadioHub
from empiremuse.utils.radio.custom_favorites import CUSTOM_FAVORITES_DB, MAX_CUSTOM_FAVORITES, CustomFavorites
from empiremuse.utils.radio.directory import DirectoryError
from empiremuse.utils.radio.favorites import (
    FAVORITES_DB,
    MAX_FAVORITES,
    AddResult,
    FavoriteForbidden,
    FavoriteNotFound,
    RadioFavorites,
)
from empiremuse.utils.radio.premium import RadioUnavailable
from empiremuse.utils.radio.user_favorites import USER_FAVORITES_DB, USER_FILTERS_MAX, USER_LIVE_FAVORITES_MAX
from tests.persistence.test_radio_favoritas import station, uuid
from tests.persistence.test_radio_store import FakeDb

pytestmark = [pytest.mark.critical, pytest.mark.asyncio]

GUILD, ADMIN, USER = 500, 7, 1001
CUSTOM = "a" * 24
GENRE = "g:rock"


def perms(manager: bool = True):
    guild = SimpleNamespace(
        owner_id=ADMIN if manager else 0,
        get_member=lambda uid: SimpleNamespace(
            guild_permissions=SimpleNamespace(manage_guild=manager, administrator=False)
        ),
    )
    return SimpleNamespace(get_all_bots=lambda: [SimpleNamespace(get_guild=lambda gid: guild)])


@pytest.fixture
def world():
    db = FakeDb()
    pool = SimpleNamespace(database=db, **vars(perms()))
    hub = RadioHub(pool)
    hub.directory = SimpleNamespace(by_uuid_strict=AsyncMock(side_effect=lambda value: station(int(value[-12:]))))
    hub.locations = SimpleNamespace(node=lambda value: object() if value.startswith("node:") else None)
    return SimpleNamespace(
        db=db, pool=pool, hub=hub, favorites=hub.favorites, custom=hub.custom_favorites, mine=hub.user_favorites
    )


async def yes(ref):
    return True


async def no(ref):
    return False


class TestServerList:
    async def test_the_cap_is_the_sum_of_live_custom_and_genre(self, world):
        for n in range(MAX_FAVORITES - 1):
            await world.favorites.add_live(GUILD, ADMIN, uuid(n), world.hub.directory.by_uuid_strict)
        assert await world.favorites.add_ref(GUILD, ADMIN, CUSTOM, yes) is AddResult.ADDED  # 24º

        assert await world.favorites.add_ref(GUILD, ADMIN, GENRE, yes) is AddResult.FULL
        assert (
            await world.favorites.add_live(GUILD, ADMIN, uuid(99), world.hub.directory.by_uuid_strict) is AddResult.FULL
        )
        stations, refs = await world.favorites.list_all(GUILD)
        assert len(stations) + len(refs) == MAX_FAVORITES

    async def test_already_and_full_never_ask_the_directory_or_the_existence_check(self, world):
        directory = world.hub.directory.by_uuid_strict
        await world.favorites.add_live(GUILD, ADMIN, uuid(1), directory)
        directory.reset_mock()
        exists = AsyncMock(return_value=True)
        await world.favorites.add_ref(GUILD, ADMIN, CUSTOM, exists)
        exists.reset_mock()

        assert await world.favorites.add_live(GUILD, ADMIN, uuid(1), directory) is AddResult.ALREADY
        assert await world.favorites.add_ref(GUILD, ADMIN, CUSTOM, exists) is AddResult.ALREADY
        for n in range(2, MAX_FAVORITES):
            await world.favorites.add_live(GUILD, ADMIN, uuid(n), directory)
        directory.reset_mock()
        assert await world.favorites.add_live(GUILD, ADMIN, uuid(98), directory) is AddResult.FULL
        assert await world.favorites.add_ref(GUILD, ADMIN, GENRE, exists) is AddResult.FULL

        directory.assert_not_awaited()
        exists.assert_not_awaited()

    async def test_every_writer_keeps_the_other_list_and_an_old_document_reads_fine(self, world):
        world.db.tables[FAVORITES_DB] = {
            str(GUILD): {"_id": str(GUILD), "rev": 3, "stations": [{"uuid": uuid(1), "name": "Antiga"}]}
        }  # sem ``refs``
        assert await world.favorites.add_ref(GUILD, ADMIN, CUSTOM, yes) is AddResult.ADDED
        assert (
            await world.favorites.add_live(GUILD, ADMIN, uuid(2), world.hub.directory.by_uuid_strict) is AddResult.ADDED
        )
        assert await world.favorites.remove(GUILD, ADMIN, uuid(1)) is True
        assert await world.favorites.add_ref(GUILD, ADMIN, GENRE, yes) is AddResult.ADDED
        assert await world.favorites.remove_ref(GUILD, ADMIN, CUSTOM) is True

        stations, refs = await world.favorites.list_all(GUILD)

        assert [f.uuid for f in stations] == [uuid(2)] and refs == [GENRE]
        assert await world.favorites.clear(GUILD, ADMIN) is True  # o botão do Discord limpa só as ao vivo
        assert await world.favorites.list_all(GUILD) == ([], [GENRE])

    async def test_nothing_with_a_user_id_is_ever_stored(self, world):
        await world.favorites.add_live(GUILD, ADMIN, uuid(1), world.hub.directory.by_uuid_strict)
        await world.favorites.add_ref(GUILD, ADMIN, CUSTOM, yes)

        stored = world.db.tables[FAVORITES_DB][str(GUILD)]

        assert set(stored) == {"_id", "rev", "stations", "refs"}
        assert str(ADMIN) not in str(stored) and "added_by" not in str(stored)
        assert set(stored["stations"][0]) == {"uuid", "name"} and stored["refs"] == [CUSTOM]  # ref SEM nome

    async def test_a_non_manager_never_writes_and_the_store_checks_the_actor(self, world):
        world.pool.get_all_bots = perms(False).get_all_bots

        for call in (
            world.favorites.add_live(GUILD, USER, uuid(1), world.hub.directory.by_uuid_strict),
            world.favorites.add_ref(GUILD, USER, CUSTOM, yes),
            world.favorites.remove(GUILD, USER, uuid(1)),
            world.favorites.remove_ref(GUILD, USER, CUSTOM),
        ):
            with pytest.raises(FavoriteForbidden):
                await call

        assert world.db.tables.get(FAVORITES_DB, {}) == {}

    async def test_format_directory_and_existence_errors_are_distinct_and_nothing_is_written(self, world):
        directory = world.hub.directory.by_uuid_strict
        with pytest.raises(FavoriteNotFound):
            await world.favorites.add_live(GUILD, ADMIN, "nao-uuid", directory)
        with pytest.raises(FavoriteNotFound):
            await world.favorites.add_ref(GUILD, ADMIN, "g:Rock Alternativo", yes)
        with pytest.raises(FavoriteNotFound):
            await world.favorites.add_ref(
                GUILD, ADMIN, CUSTOM, no
            )  # a custom de OUTRO servidor é igual a uma que não existe
        world.hub.directory.by_uuid_strict = AsyncMock(return_value=None)
        with pytest.raises(FavoriteNotFound):
            await world.favorites.add_live(GUILD, ADMIN, uuid(1), world.hub.directory.by_uuid_strict)
        world.hub.directory.by_uuid_strict = AsyncMock(side_effect=DirectoryError("unavailable"))
        with pytest.raises(RadioUnavailable):  # diretório fora: 503, nunca "não encontrada"
            await world.favorites.add_live(GUILD, ADMIN, uuid(1), world.hub.directory.by_uuid_strict)

        assert world.db.tables.get(FAVORITES_DB, {}) == {}

    async def test_two_adds_racing_for_the_last_slot_one_wins_one_is_full(self, world):
        for n in range(MAX_FAVORITES - 1):
            await world.favorites.add_live(GUILD, ADMIN, uuid(n), world.hub.directory.by_uuid_strict)

        results = await asyncio.gather(
            world.favorites.add_ref(GUILD, ADMIN, CUSTOM, yes), world.favorites.add_ref(GUILD, ADMIN, "b" * 24, yes)
        )

        assert sorted(r.value for r in results) == ["added", "full"]
        stations, refs = await world.favorites.list_all(GUILD)
        assert len(stations) + len(refs) == MAX_FAVORITES

    async def test_database_down_is_unavailable_and_the_name_is_cleaned_when_stored(self, world):
        world.hub.directory.by_uuid_strict = AsyncMock(
            side_effect=lambda value: station(1, "@everyone https://evil.example Rádio")
        )
        await world.favorites.add_live(GUILD, ADMIN, uuid(1), world.hub.directory.by_uuid_strict)
        assert "@everyone" not in str(world.db.tables[FAVORITES_DB][str(GUILD)]) and "https://" not in str(
            world.db.tables[FAVORITES_DB][str(GUILD)]
        )

        world.db.fail = True
        with pytest.raises(RadioUnavailable):
            await world.favorites.add_ref(GUILD, ADMIN, CUSTOM, yes)


class TestMyCustomFavorites:
    async def test_add_remove_are_per_member_and_per_guild_and_cap_at_24(self, world):
        assert await world.custom.add(GUILD, USER, CUSTOM, yes) is AddResult.ADDED
        assert await world.custom.add(GUILD, USER, CUSTOM, yes) is AddResult.ALREADY
        assert (
            await world.custom.list(GUILD, USER + 1) == [] and await world.custom.list(GUILD + 1, USER) == []
        )  # outro membro, outro servidor
        for n in range(MAX_CUSTOM_FAVORITES - 1):
            await world.custom.add(GUILD, USER, f"{n:024x}", yes)
        assert await world.custom.add(GUILD, USER, GENRE, yes) is AddResult.FULL

        assert await world.custom.remove(GUILD, USER, CUSTOM) is True
        assert await world.custom.remove(GUILD, USER, CUSTOM) is False  # remover o que não está não é erro
        assert set(world.db.tables[CUSTOM_FAVORITES_DB]) == {f"{GUILD}:{USER}"}

    async def test_format_then_full_then_existence(self, world):
        exists = AsyncMock(return_value=True)
        with pytest.raises(FavoriteNotFound):
            await world.custom.add(GUILD, USER, "g:Rock", exists)
        exists.assert_not_awaited()
        assert await world.custom.add(GUILD, USER, CUSTOM, exists) is AddResult.ADDED
        exists.reset_mock()
        assert await world.custom.add(GUILD, USER, CUSTOM, exists) is AddResult.ALREADY
        exists.assert_not_awaited()
        with pytest.raises(FavoriteNotFound):
            await world.custom.add(GUILD, USER, "c" * 24, no)
        world.db.fail = True
        with pytest.raises(RadioUnavailable):
            await world.custom.remove(GUILD, USER, CUSTOM)


class TestMyAccountList:
    async def test_add_station_orders_the_checks_and_never_trusts_a_name(self, world):
        directory = world.hub.directory.by_uuid_strict
        assert await world.mine.add_station(USER, uuid(1)) is AddResult.ADDED
        assert await world.mine.add_station(USER, uuid(1)) is AddResult.ALREADY
        assert directory.await_count == 1  # já está: sem diretório
        for n in range(2, USER_LIVE_FAVORITES_MAX + 1):
            await world.mine.add_station(USER, uuid(n))
        directory.reset_mock()
        assert await world.mine.add_station(USER, uuid(98)) is AddResult.FULL
        directory.assert_not_awaited()
        with pytest.raises(FavoriteNotFound):
            await world.mine.add_station(USER + 1, "nao-uuid")
        world.hub.directory.by_uuid_strict = AsyncMock(return_value=None)
        with pytest.raises(FavoriteNotFound):
            await world.mine.add_station(USER + 1, uuid(1))
        world.hub.directory.by_uuid_strict = AsyncMock(side_effect=DirectoryError("unavailable"))
        with pytest.raises(RadioUnavailable):
            await world.mine.add_station(USER + 1, uuid(1))

    async def test_add_filter_keeps_the_stations_and_the_inverse(self, world):
        await world.mine.add_station(USER, uuid(1))
        assert await world.mine.add_filter(USER, "node:a") is AddResult.ADDED
        assert await world.mine.add_filter(USER, "node:a") is AddResult.ALREADY
        assert [f.uuid for f in await world.mine.stations(USER)] == [uuid(1)]
        await world.mine.add_station(USER, uuid(2))
        assert await world.mine.filters(USER) == ["node:a"]
        for n in range(1, USER_FILTERS_MAX):
            await world.mine.add_filter(USER, f"node:{n}")
        assert await world.mine.add_filter(USER, "node:extra") is AddResult.FULL
        with pytest.raises(FavoriteNotFound):
            await world.mine.add_filter(USER + 1, "sem-prefixo")  # a árvore não o conhece
        with pytest.raises(FavoriteNotFound):
            await world.mine.add_filter(USER + 1, "<script>")
        assert set(world.db.tables[USER_FAVORITES_DB][str(USER)]) == {"_id", "rev", "stations", "filters"}


class TestErasureStaysTrue:
    """E1 não muda (contrato §4 ``[r9-fav]`` j): o que os armazéns gravam continua sendo achado pela exclusão e a lista do servidor não tem id de usuário."""

    async def test_keys_written_by_the_stores_are_the_ones_the_erasure_matches(self, world):
        from empiremuse.rules.erasure import custom_favorite_key_matches

        await world.mine.add_station(USER, uuid(1))
        await world.custom.add(GUILD, USER, CUSTOM, yes)
        await world.custom.add(GUILD + 1, USER, GENRE, yes)
        await world.custom.add(GUILD, USER * 10, CUSTOM, yes)  # um id que contém o dele como sufixo/prefixo

        assert set(world.db.tables[USER_FAVORITES_DB]) == {str(USER)}  # o ``_id`` decimal que a exclusão apaga
        mine = [key for key in world.db.tables[CUSTOM_FAVORITES_DB] if custom_favorite_key_matches(key, USER)]
        assert sorted(mine) == [f"{GUILD}:{USER}", f"{GUILD + 1}:{USER}"]

    async def test_the_server_document_never_has_a_user_id(self, world):
        await world.favorites.add_live(GUILD, ADMIN, uuid(1), world.hub.directory.by_uuid_strict)
        await world.favorites.add_ref(GUILD, ADMIN, GENRE, yes)

        assert str(ADMIN) not in str(world.db.tables[FAVORITES_DB])
