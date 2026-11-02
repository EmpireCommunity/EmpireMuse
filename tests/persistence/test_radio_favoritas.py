"""As estações favoritas do servidor (item 8): uma lista por servidor, até 24, com compare-and-set; só uuid e nome limpo, nunca o endereço do fluxo."""

import asyncio
from types import SimpleNamespace

import pytest

from empiremuse.rules.radio_stations import Station
from empiremuse.utils.radio.favorites import FAVORITES_DB, MAX_FAVORITES, AddResult, RadioFavorites
from empiremuse.utils.radio.premium import RadioUnavailable
from tests.persistence.test_radio_store import FakeDb

pytestmark = pytest.mark.critical

GUILD, OTHER = 500, 501


def uuid(n):
    return f"00000000-0000-0000-0000-{n:012d}"


def station(n, name=None):
    return Station(uuid(n), name or f"Rádio {n}", "http://stream.example:8000/live?token=SEGREDO", "MP3", 128, "Brasil")


@pytest.fixture
def world():
    db = FakeDb()
    return SimpleNamespace(db=db, favorites=RadioFavorites(SimpleNamespace(database=db)))


class TestAdd:
    @pytest.mark.asyncio
    async def test_it_adds_lists_and_does_not_repeat(self, world):
        assert await world.favorites.add(GUILD, station(1)) is AddResult.ADDED
        assert await world.favorites.add(GUILD, station(2)) is AddResult.ADDED
        assert await world.favorites.add(GUILD, station(1)) is AddResult.ALREADY

        assert [f.uuid for f in await world.favorites.list(GUILD)] == [uuid(1), uuid(2)]
        assert await world.favorites.list(OTHER) == []  # cada servidor tem a sua

    @pytest.mark.asyncio
    async def test_only_uuid_and_name_are_stored_never_the_stream_address(self, world):
        await world.favorites.add(GUILD, station(1))

        stored = world.db.tables[FAVORITES_DB][str(GUILD)]
        assert stored["stations"] == [{"uuid": uuid(1), "name": "Rádio 1"}]
        assert "SEGREDO" not in str(stored) and "stream.example" not in str(stored)

    @pytest.mark.asyncio
    async def test_the_list_stops_at_24(self, world):
        for n in range(MAX_FAVORITES):
            assert await world.favorites.add(GUILD, station(n)) is AddResult.ADDED

        assert await world.favorites.add(GUILD, station(99)) is AddResult.FULL
        assert len(await world.favorites.list(GUILD)) == MAX_FAVORITES

    @pytest.mark.asyncio
    async def test_two_members_favoriting_at_once_lose_nobody(self, world):
        results = await asyncio.gather(*(world.favorites.add(GUILD, station(n)) for n in range(4)))

        assert all(r is AddResult.ADDED for r in results)
        assert {f.uuid for f in await world.favorites.list(GUILD)} == {uuid(n) for n in range(4)}

    @pytest.mark.asyncio
    async def test_a_database_failure_is_radio_unavailable_not_a_crash(self, world):
        world.db.fail = True

        with pytest.raises(RadioUnavailable):
            await world.favorites.add(GUILD, station(1))
        with pytest.raises(RadioUnavailable):
            await world.favorites.list(GUILD)


class TestReadingBadData:
    @pytest.mark.asyncio
    async def test_bad_items_in_the_document_are_ignored_and_the_name_is_neutralised(self, world):
        world.db.tables[FAVORITES_DB] = {
            str(GUILD): {
                "_id": str(GUILD),
                "rev": 3,
                "stations": [
                    {"uuid": uuid(1), "name": "@everyone <@1> https://evil.example"},
                    {"uuid": "nao-e-uuid", "name": "x"},
                    {"uuid": uuid(2)},
                    "lixo",
                    {"uuid": uuid(1), "name": "repetida"},
                ],
            }
        }

        found = await world.favorites.list(GUILD)

        assert [f.uuid for f in found] == [uuid(1)]
        assert "@everyone" not in found[0].name and "<@" not in found[0].name and "https://" not in found[0].name
