"""O autocomplete de ``estacao:`` (item 8, R3, R-7): vazio e SEM requisição ao diretório sem manage_guild ou sem premium; ⛔ nas indisponíveis."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.cogs.radio import station_autocomplete
from empiremuse.rules.radio_stations import Station
from empiremuse.utils.radio.premium import RadioUnavailable

pytestmark = pytest.mark.critical

GOOD = "11111111-1111-1111-1111-111111111111"
DOWN = "22222222-2222-2222-2222-222222222222"


def stations():
    return [
        Station(GOOD, "Boa FM", "http://stream.example:8000/x", "MP3", 128, "Brasil"),
        Station(DOWN, "Fora FM", "http://stream.example:8000/y", "AAC", 0, ""),
    ]


def inter(*, manage_guild=True, premium=object(), guild_id=500, search=None):
    directory = SimpleNamespace(search=search or AsyncMock(return_value=stations()))

    async def get_premium(gid):
        if isinstance(premium, Exception):
            raise premium
        return premium

    hub = SimpleNamespace(
        premium=SimpleNamespace(get=get_premium),
        directory=directory,
        health=SimpleNamespace(unavailable=lambda uuid: uuid == DOWN),
    )
    tr = lambda key, **kw: {"radio.live.unavailable": "indisponível"}.get(key, key)  # noqa: E731
    bot = SimpleNamespace(pool=SimpleNamespace(radio=hub, i18n=SimpleNamespace(for_guild=AsyncMock(return_value=tr))))
    return SimpleNamespace(
        guild_id=guild_id,
        guild=SimpleNamespace(id=guild_id),
        bot=bot,
        author=SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=manage_guild)),
    ), directory


class TestStationAutocomplete:
    @pytest.mark.asyncio
    async def test_without_manage_guild_it_is_empty_and_never_asks_the_directory(self):
        i, directory = inter(manage_guild=False)

        assert await station_autocomplete(i, "rock") == {}
        directory.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_without_premium_it_is_empty_and_never_asks_the_directory(self):
        i, directory = inter(premium=None)

        assert await station_autocomplete(i, "rock") == {}
        directory.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_premium_lookup_that_fails_is_empty_and_never_asks_the_directory(self):
        i, directory = inter(premium=RadioUnavailable())

        assert await station_autocomplete(i, "rock") == {}
        directory.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_in_a_dm_it_is_empty_and_never_asks_the_directory(self):
        i, directory = inter(guild_id=None)

        assert await station_autocomplete(i, "rock") == {}
        directory.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_with_premium_and_manage_guild_it_lists_uuids_and_marks_the_unavailable(self):
        i, directory = inter()

        choices = await station_autocomplete(i, "rock")

        directory.search.assert_awaited_once_with("rock")
        assert choices[next(k for k in choices if k.startswith("Boa FM"))] == GOOD
        down = next(k for k in choices if "Fora FM" in k)
        assert down.startswith("⛔") and "indisponível" in down and choices[down] == DOWN
        good = next(k for k in choices if k.startswith("Boa FM"))
        assert "⛔" not in good and "MP3 128k" in good and "Brasil" in good
        assert all(len(label) <= 100 for label in choices)

    @pytest.mark.asyncio
    async def test_at_most_25_choices(self):
        many = [
            Station(f"00000000-0000-0000-0000-{n:012d}", f"E{n}", "http://s.example:8000/x", "MP3", 128, "")
            for n in range(60)
        ]
        i, _ = inter(search=AsyncMock(return_value=many))

        assert len(await station_autocomplete(i, "e")) == 25
