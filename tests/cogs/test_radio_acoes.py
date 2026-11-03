"""As ações de `/radio on|off|status` (item 8): a matriz de autorização (R-4), o premium, o teto de 3 (R-2) e o fato de só ligar rádio já configurada."""

import asyncio
from types import SimpleNamespace

import pytest

from empiremuse.rules.radio import radio_id
from empiremuse.utils.radio import RadioHub, actions
from empiremuse.utils.radio.actions import Actor, Refused
from empiremuse.utils.radio.service import RadioState
from tests.cogs.radio_config import CUSTOM_STATION, configure
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


async def on(world, **over):
    """Configura a rádio (``/radio setup``) e liga (``/radio on``). ``bot`` escolhe o bot fixo da rádio; ``source`` põe (ou tira, com ``{}``) a estação."""
    guild_id, channel_id = over.get("guild_id", GUILD), over.get("channel_id", CHANNEL)
    if over.pop("configured", True):
        await configure(
            world.hub,
            guild_id=guild_id,
            channel_id=channel_id,
            bot_id=over.pop("bot", 2),
            source=over.pop("source", None),
        )
    over.pop("bot", None), over.pop("source", None)
    args = dict(guild_id=guild_id, channel_id=channel_id, actor=ADMIN, bot_can_join=True)
    return await actions.turn_on(world.hub, **{**args, **over})


async def premium(world, guild_id=GUILD):
    await world.hub.control.load()  # a pausa do dono lida do banco: sem documento, não pausado
    await world.hub.premium.grant(guild_id, by=1)


class TestTurnOn:
    @pytest.mark.asyncio
    async def test_the_jukebox_channel_is_refused_and_nothing_is_reserved(
        self, world
    ):  # V181-1: o chat dele é da Jukebox
        await premium(world)

        with pytest.raises(Refused) as refusal:
            await on(world, jukebox_channel_id=CHANNEL)

        assert refusal.value.code == actions.JUKEBOX_CHANNEL
        assert (await world.hub.premium.fresh(GUILD)).active == 0

    @pytest.mark.asyncio
    async def test_another_channel_is_fine_even_with_a_jukebox_channel_set(self, world):
        await premium(world)

        radio = await on(world, jukebox_channel_id=OTHER_CHANNEL)

        assert radio.enabled and radio.channel_id == CHANNEL

    @pytest.mark.asyncio
    async def test_it_turns_on_the_configured_radio_with_its_own_bot_and_counts_the_slot(self, world):
        await premium(world)

        radio = await on(world, bot=4)

        assert radio.enabled and radio.id == radio_id(4, GUILD) and radio.channel_id == CHANNEL
        assert radio.source == {"station": CUSTOM_STATION} and (await world.hub.premium.fresh(GUILD)).active == 1

    @pytest.mark.asyncio
    async def test_without_a_configured_radio_in_the_channel_the_hint_is_to_run_setup(self, world):
        await premium(world)

        with pytest.raises(Refused) as refusal:
            await on(world, configured=False)

        assert refusal.value.code == actions.NOT_CONFIGURED and await world.hub.store.list_for_guild(GUILD) == []

    @pytest.mark.asyncio
    async def test_a_radio_configured_without_a_station_is_refused_with_the_setup_hint(self, world):
        await premium(world)

        with pytest.raises(Refused) as refusal:
            await on(world, source={})

        assert refusal.value.code == actions.NO_STATION
        assert not (await world.hub.store.list_for_guild(GUILD))[0].enabled
        assert (await world.hub.premium.fresh(GUILD)).active == 0

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
        assert (await world.hub.premium.fresh(GUILD)).active == 0

    @pytest.mark.asyncio
    async def test_without_premium_nothing_is_turned_on(self, world):
        await world.hub.control.load()
        with pytest.raises(Refused) as refusal:
            await on(world)

        assert refusal.value.code == actions.NO_PREMIUM
        assert not any(r.enabled for r in await world.hub.store.list_for_guild(GUILD))

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("over", "code"),
        [
            ({"bot_can_join": False}, actions.BOT_CANNOT_JOIN),
            ({"bot_free": False}, actions.NO_FREE_BOT),
        ],
    )
    async def test_every_refusal_has_its_code_and_leaves_the_radio_off(self, world, over, code):
        await premium(world)

        with pytest.raises(Refused) as refusal:
            await on(world, **over)

        assert refusal.value.code == code
        assert not any(r.enabled for r in await world.hub.store.list_for_guild(GUILD))

    @pytest.mark.asyncio
    async def test_turning_on_a_radio_that_is_already_on_changes_nothing(self, world):
        await premium(world)
        first = await on(world)

        again = await on(world, configured=False)

        assert again.id == first.id and (await world.hub.premium.fresh(GUILD)).active == 1

    @pytest.mark.asyncio
    async def test_a_second_radio_runs_on_its_own_bot(self, world):
        await premium(world)
        first = await on(world)

        second = await on(world, channel_id=OTHER_CHANNEL, bot=3)

        assert first.bot_id == 2 and second.bot_id == 3

    @pytest.mark.asyncio
    async def test_the_fourth_radio_is_refused_by_the_cap_and_stays_off(self, world):
        await premium(world)
        for channel, bot in ((900, 2), (901, 3), (902, 4)):
            await on(world, channel_id=channel, bot=bot)

        with pytest.raises(Refused) as refusal:
            await on(world, channel_id=903, bot=5)

        assert refusal.value.code == actions.CAP_REACHED
        assert sum(r.enabled for r in await world.hub.store.list_for_guild(GUILD)) == 3
        assert (await world.hub.premium.fresh(GUILD)).active == 3

    @pytest.mark.asyncio
    async def test_ten_simultaneous_turn_ons_with_two_already_on_let_exactly_one_through(self, world):
        await premium(world)
        await on(world, channel_id=1, bot=2)
        await on(world, channel_id=2, bot=3)
        for number in range(10):  # dez rádios configuradas e desligadas (a configuração em si não tem teto de ligadas)
            await configure(world.hub, channel_id=100 + number, bot_id=10 + number)

        results = await asyncio.gather(
            *(
                actions.turn_on(world.hub, guild_id=GUILD, channel_id=100 + n, actor=ADMIN, bot_can_join=True)
                for n in range(10)
            ),
            return_exceptions=True,
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


class TestPausedAllBlocksTurnOn:
    """Keystone (contrato §2): com a pausa do dono ligada, ``ligar`` é recusado ANTES de gravar, de ocupar vaga ou de o bot entrar na call."""

    @pytest.mark.asyncio
    async def test_turn_on_is_refused_while_the_owner_pause_is_on_and_nothing_is_written(self, world):
        await premium(world)
        await world.hub.control.set_paused_all(True, by=1)

        with pytest.raises(Refused) as refusal:
            await on(world)

        assert refusal.value.code == actions.PAUSED_ALL
        assert not any(r.enabled for r in await world.hub.store.list_for_guild(GUILD))
        assert (await world.hub.premium.fresh(GUILD)).active == 0

    @pytest.mark.asyncio
    async def test_it_works_again_after_the_owner_resumes(self, world):
        await premium(world)
        await world.hub.control.set_paused_all(True, by=1)
        await world.hub.control.set_paused_all(False, by=1)

        assert (await on(world)).enabled

    @pytest.mark.asyncio
    async def test_before_the_pause_flag_is_read_from_the_database_the_valve_stays_closed(self, world):
        await world.hub.premium.grant(GUILD, by=1)  # sem control.load(): começa fechado

        with pytest.raises(Refused) as refusal:
            await on(world)

        assert refusal.value.code == actions.PAUSED_ALL

    def test_the_refusal_has_a_translated_text(self):
        from empiremuse.i18n import get_i18n

        for locale, snippet in (("pt-BR", "pausadas pelo dono"), ("en-US", "paused by the bot owner")):
            assert snippet in get_i18n().for_locale(locale)("radio.refused.paused_all")
