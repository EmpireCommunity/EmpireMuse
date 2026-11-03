"""Controles do dono (item 8, R1): "pausar todas" sobrevive a reinício e começa fechado; o principal nunca é só-rádio."""

from types import SimpleNamespace

import pytest

from empiremuse.utils.radio import RadioHub
from empiremuse.utils.radio.control import MainBotRefused
from empiremuse.utils.radio.premium import RadioUnavailable
from tests.persistence.test_radio_store import FakeDb

pytestmark = pytest.mark.critical


@pytest.fixture
def world():
    db = FakeDb()
    pool = SimpleNamespace(database=db)
    return SimpleNamespace(db=db, pool=pool, hub=RadioHub(pool))


def restarted(world):
    """Um processo novo sobre o mesmo banco."""
    return RadioHub(world.pool)


class TestPauseAll:
    def test_before_the_first_read_everything_counts_as_paused(self, world):
        assert world.hub.control.paused_all is True

    @pytest.mark.asyncio
    async def test_no_document_means_not_paused_once_it_was_read(self, world):
        assert await world.hub.control.load() is False and world.hub.control.paused_all is False

    @pytest.mark.asyncio
    async def test_the_value_survives_a_restart(self, world):
        await world.hub.control.set_paused_all(True, by=7)

        again = restarted(world)
        await again.control.load()

        assert again.control.paused_all is True
        assert world.db.tables["radio_control"]["control"]["by"] == "7"

    @pytest.mark.asyncio
    async def test_resuming_clears_it_and_repeating_a_command_changes_nothing(self, world):
        assert await world.hub.control.set_paused_all(True, by=7) is True
        assert await world.hub.control.set_paused_all(True, by=7) is False
        assert await world.hub.control.set_paused_all(False, by=7) is True

        assert world.hub.control.paused_all is False

    @pytest.mark.asyncio
    async def test_listeners_hear_only_real_changes_and_a_broken_one_does_not_stop_the_rest(self, world):
        heard = []
        world.hub.control.on_change(lambda value: (_ for _ in ()).throw(RuntimeError("defeito")))
        world.hub.control.on_change(heard.append)

        await world.hub.control.set_paused_all(True, by=1)
        await world.hub.control.set_paused_all(True, by=1)
        await world.hub.control.set_paused_all(False, by=1)

        assert heard == [True, False]

    @pytest.mark.asyncio
    async def test_it_never_touches_the_premium_counter(self, world):
        await world.hub.premium.grant(500, by=1)
        await world.hub.premium.reserve(500)

        await world.hub.control.set_paused_all(True, by=1)

        assert (await world.hub.premium.fresh(500)).active == 1

    @pytest.mark.asyncio
    async def test_a_database_failure_is_radio_unavailable_and_keeps_the_safe_default(self, world):
        world.db.fail = True

        with pytest.raises(RadioUnavailable):
            await world.hub.control.load()
        assert world.hub.control.paused_all is True


class TestRadioOnlyBots:
    @pytest.mark.asyncio
    async def test_add_remove_and_list(self, world):
        roles = world.hub.roles

        assert await roles.add("TOKEN_3") == ("TOKEN_3",)
        assert await roles.add("TOKEN_2") == ("TOKEN_2", "TOKEN_3")
        assert roles.is_radio_only("TOKEN_3") and not roles.is_radio_only("TOKEN_4")
        assert await roles.remove("TOKEN_3") == ("TOKEN_2",)

    @pytest.mark.asyncio
    async def test_the_main_bot_can_never_be_radio_only(self, world):
        for main in ("TOKEN", "TOKEN_1"):
            with pytest.raises(MainBotRefused):
                await world.hub.roles.add(main)
        world.db.tables["bot_roles"] = {
            "roles": {"_id": "roles", "radio_only": ["TOKEN", "TOKEN_2"], "rev": 1}
        }  # adulterado

        await world.hub.roles.load()

        assert world.hub.roles.radio_only == ("TOKEN_2",) and not world.hub.roles.is_radio_only("TOKEN")

    @pytest.mark.asyncio
    async def test_the_list_survives_a_restart_and_garbage_entries_are_dropped(self, world):
        await world.hub.roles.add("TOKEN_2")
        world.db.tables["bot_roles"]["roles"]["radio_only"] += [5, "", None]

        again = restarted(world)
        await again.roles.load()

        assert again.roles.radio_only == ("TOKEN_2",)

    def test_a_non_string_identifier_is_never_radio_only(self, world):
        assert world.hub.roles.is_radio_only(None) is False and world.hub.roles.is_radio_only(3) is False
