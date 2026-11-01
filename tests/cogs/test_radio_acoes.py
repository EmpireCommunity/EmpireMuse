"""As ações de `/radio ligar|desligar|status` (item 8, R1): a matriz de autorização (R-4), o premium, o teto de 3 (R-2) e a escolha do bot."""

import asyncio
from types import SimpleNamespace

import pytest

from empiremuse.rules.radio import BotCandidate, radio_id
from empiremuse.utils.radio import RadioHub, actions
from empiremuse.utils.radio.actions import Actor, Refused
from empiremuse.utils.radio.service import RadioState
from tests.persistence.test_radio_store import FakeDb

pytestmark = pytest.mark.critical

GUILD, CHANNEL, OTHER_CHANNEL = 500, 900, 901
ADMIN = Actor(user_id=7, manage_guild=True, manage_messages_in_channel=True)


@pytest.fixture
def world():
    db = FakeDb()
    pool = SimpleNamespace(database=db)
    hub = RadioHub(pool)
    return SimpleNamespace(db=db, hub=hub)


def bots(*ids, **over):
    base = dict(main=False, radio_only=False, ready=True, in_guild=True, busy=False, has_radio_here=False)
    return [BotCandidate(f"TOKEN_{n}", n, **{**base, **over}) for n in ids]


async def on(world, **over):
    args = dict(
        guild_id=GUILD,
        channel_id=CHANNEL,
        playlist="Rock",
        actor=ADMIN,
        candidates=bots(2, 3, 4, 5),
        playlist_exists=True,
        bot_can_join=True,
    )
    return await actions.turn_on(world.hub, **{**args, **over})


async def premium(world, guild_id=GUILD):
    await world.hub.premium.grant(guild_id, by=1)


class TestTurnOn:
    @pytest.mark.asyncio
    async def test_it_creates_enables_and_counts_on_the_first_free_reserve(self, world):
        await premium(world)

        radio = await on(world)

        assert radio.enabled and radio.id == radio_id(2, GUILD) and radio.channel_id == CHANNEL
        assert radio.source == {"playlist": "Rock"} and (await world.hub.premium.fresh(GUILD)).active == 1

    @pytest.mark.asyncio
    async def test_the_whole_matrix_manage_guild_and_manage_messages_are_both_needed(self, world):
        await premium(world)

        with pytest.raises(Refused) as no_guild:
            await on(world, actor=Actor(7, manage_guild=False, manage_messages_in_channel=True))
        with pytest.raises(
            Refused
        ) as no_messages:  # R-4: manage_guild sozinho passaria a apagar mensagens alheias pelo bot
            await on(world, actor=Actor(7, manage_guild=True, manage_messages_in_channel=False))

        assert no_guild.value.code == actions.NO_MANAGE_GUILD and no_messages.value.code == actions.NO_MANAGE_MESSAGES
        assert await world.hub.store.list_for_guild(GUILD) == []

    @pytest.mark.asyncio
    async def test_without_premium_nothing_is_written(self, world):
        with pytest.raises(Refused) as refusal:
            await on(world)

        assert refusal.value.code == actions.NO_PREMIUM and await world.hub.store.list_for_guild(GUILD) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("over", "code"),
        [
            ({"playlist_exists": False}, actions.NO_PLAYLIST),
            ({"bot_can_join": False}, actions.BOT_CANNOT_JOIN),
            ({"candidates": bots(1, main=True)}, actions.NO_FREE_BOT),
            ({"candidates": bots(2, busy=True)}, actions.NO_FREE_BOT),
            ({"candidates": []}, actions.NO_FREE_BOT),
            ({"playlist": ""}, actions.INVALID),
        ],
    )
    async def test_every_refusal_has_its_code_and_leaves_no_radio_behind(self, world, over, code):
        await premium(world)

        with pytest.raises(Refused) as refusal:
            await on(world, **over)

        assert refusal.value.code == code and await world.hub.store.list_for_guild(GUILD) == []

    @pytest.mark.asyncio
    async def test_a_channel_that_already_has_a_radio_is_taken(self, world):
        await premium(world)
        await on(world)

        with pytest.raises(Refused) as refusal:
            await on(world)

        assert refusal.value.code == actions.CHANNEL_TAKEN

    @pytest.mark.asyncio
    async def test_a_second_radio_goes_to_another_bot_never_to_the_one_already_running_here(self, world):
        await premium(world)
        first = await on(world)

        second = await on(world, channel_id=OTHER_CHANNEL)

        assert first.bot_id == 2 and second.bot_id == 3

    @pytest.mark.asyncio
    async def test_the_fourth_radio_is_refused_by_the_cap_and_stays_off(self, world):
        await premium(world)
        for channel in (900, 901, 902):
            await on(world, channel_id=channel)

        with pytest.raises(Refused) as refusal:
            await on(world, channel_id=903)

        assert refusal.value.code == actions.CAP_REACHED
        assert sum(r.enabled for r in await world.hub.store.list_for_guild(GUILD)) == 3
        assert (await world.hub.premium.fresh(GUILD)).active == 3

    @pytest.mark.asyncio
    async def test_a_radio_the_bot_had_before_is_reused_with_the_new_channel_and_playlist(self, world):
        await premium(world)
        first = await on(world)
        await actions.turn_off(world.hub, guild_id=GUILD, channel_id=CHANNEL, actor=ADMIN)

        again = await on(world, channel_id=OTHER_CHANNEL, playlist="Jazz", candidates=bots(2))

        assert again.id == first.id and again.channel_id == OTHER_CHANNEL and again.source == {"playlist": "Jazz"}
        assert again.enabled and (await world.hub.premium.fresh(GUILD)).active == 1

    @pytest.mark.asyncio
    async def test_the_radio_only_reserve_is_preferred(self, world):
        await premium(world)
        candidates = [*bots(2, 3), *bots(9, radio_only=True)]

        radio = await on(world, candidates=candidates)

        assert radio.bot_id == 9

    @pytest.mark.asyncio
    async def test_ten_simultaneous_turn_ons_with_two_already_on_let_exactly_one_through(self, world):
        await premium(world)
        await on(world, channel_id=1)
        await on(world, channel_id=2)

        results = await asyncio.gather(
            *(on(world, channel_id=100 + n, candidates=bots(10 + n)) for n in range(10)), return_exceptions=True
        )

        assert sum(not isinstance(r, Exception) for r in results) == 1
        assert sum(isinstance(r, Refused) for r in results) == 9
        assert (await world.hub.premium.fresh(GUILD)).active == 3

    @pytest.mark.asyncio
    async def test_a_premium_revoked_in_the_middle_leaves_nothing_on(self, world):
        await premium(world)

        await asyncio.gather(on(world), world.hub.store.revoke_premium(GUILD), return_exceptions=True)

        assert await world.hub.premium.fresh(GUILD) is None
        assert not any(r.enabled for r in await world.hub.store.list_for_guild(GUILD))


class TestTurnOff:
    @pytest.mark.asyncio
    async def test_it_turns_the_radio_of_that_channel_off_and_gives_the_slot_back(self, world):
        await premium(world)
        await on(world)

        off = await actions.turn_off(world.hub, guild_id=GUILD, channel_id=CHANNEL, actor=ADMIN)

        assert [r.enabled for r in off] == [False] and (await world.hub.premium.fresh(GUILD)).active == 0

    @pytest.mark.asyncio
    async def test_it_needs_manage_guild_and_a_radio_on_that_channel(self, world):
        await premium(world)
        await on(world)

        with pytest.raises(Refused) as no_guild:
            await actions.turn_off(world.hub, guild_id=GUILD, channel_id=CHANNEL, actor=Actor(7, manage_guild=False))
        with pytest.raises(Refused) as nothing:
            await actions.turn_off(world.hub, guild_id=GUILD, channel_id=OTHER_CHANNEL, actor=ADMIN)

        assert no_guild.value.code == actions.NO_MANAGE_GUILD and nothing.value.code == actions.NOT_FOUND
        assert (await world.hub.premium.fresh(GUILD)).active == 1

    @pytest.mark.asyncio
    async def test_it_never_touches_the_radios_of_another_guild(self, world):
        await premium(world)
        await premium(world, 600)
        await on(world)
        await on(world, guild_id=600)

        await actions.turn_off(world.hub, guild_id=GUILD, channel_id=CHANNEL, actor=ADMIN)

        assert (await world.hub.premium.fresh(600)).active == 1


class TestStatus:
    @pytest.mark.asyncio
    async def test_without_premium_there_is_only_the_notice(self, world):
        result = await actions.status(world.hub, guild_id=GUILD)

        assert (
            result.premium is None and result.lines == [] and result.paused_all is True
        )  # antes da 1ª leitura: pausado

    @pytest.mark.asyncio
    async def test_it_lists_the_radios_with_the_state_of_this_process_and_the_owner_pause(self, world):
        await premium(world)
        radio = await on(world)
        world.hub.service.publish(radio.id, RadioState(playing=True))
        await world.hub.control.set_paused_all(False, by=1)

        result = await actions.status(world.hub, guild_id=GUILD)

        assert result.premium.active == 1 and result.paused_all is False
        assert [(line.radio.id, line.state) for line in result.lines] == [(radio.id, RadioState(playing=True))]
