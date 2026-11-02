"""`utils/radio/setup.py` (contrato-nomes-radios §2 e §4.3): configurar e redefinir uma rádio, a única porta do Discord e do painel web.

Caminho de risco ALTO (autorização e apagar dados): premium, ``manage_guild`` de quem pede, bot reserva livre, uma rádio por bot, teto de 3, canal válido,
CAS em ``rev``, objeto de outro servidor e o backup antes de apagar.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from empiremuse.rules.radio import radio_id
from empiremuse.utils.radio.premium import RadioUnavailable
from empiremuse.utils.radio.setup import RadioForbidden
from empiremuse.utils.radio.store import RadioConflict, RadioInvalid, RadioNotFound
from tests.cogs.radio_config import configure
from tests.cogs.test_radio_comandos import CHANNEL, GUILD, build, fresh_cooldown, make_channel  # noqa: F401
from tests.cogs.test_radio_comandos import premium

pytestmark = pytest.mark.critical

ACTOR = 7
OTHER_CHANNEL = 901


async def setup_world(**kwargs):
    w = build(**kwargs)
    await premium(w)
    return w


async def new_radio(w, *, bot_id=2, channel_id=CHANNEL, kind="custom"):
    return await w.hub.setup.configure(
        GUILD, ACTOR, radio_id=None, channel_id=channel_id, bot_id=bot_id, kind=kind, rev=None
    )


def code_of(refused):
    return refused.value.error.code, refused.value.error.key


class TestConfigureCreates:
    @pytest.mark.asyncio
    async def test_it_creates_a_stopped_radio_without_a_station_for_the_chosen_bot(self):
        w = await setup_world()

        radio = await new_radio(w, bot_id=3, kind="live")

        assert (radio.id, radio.channel_id, radio.kind) == (radio_id(3, GUILD), CHANNEL, "live")
        assert radio.enabled is False and radio.source == {} and radio.rev == 0
        assert (await w.hub.store.get(radio.id)) == radio

    @pytest.mark.asyncio
    async def test_it_lists_the_free_reserves_and_the_valid_channels(self):
        w = await setup_world(request_channel=OTHER_CHANNEL)
        w.bots[2].get_guild(GUILD).me.voice = object()  # TOKEN_3 em call
        await new_radio(w, bot_id=2)

        options = await w.hub.setup.options(GUILD)

        assert options.bots == ()  # o 2 já tem rádio, o 3 está em call e o principal nunca entra
        assert [c[0] for c in options.channels] == [CHANNEL] and options.channels[0][2] == "voice"

    @pytest.mark.asyncio
    async def test_the_jukebox_channel_is_not_offered(self):
        w = await setup_world(request_channel=CHANNEL)

        options = await w.hub.setup.options(GUILD)

        assert options.channels == () and [b[0] for b in options.bots] == [2, 3]
        assert options.bots[0] == (2, "Bot 2")


class TestConfigureRefusals:
    @pytest.mark.asyncio
    async def test_without_premium_it_is_forbidden_and_nothing_is_written(self):
        w = build()
        await w.hub.control.load()

        with pytest.raises(RadioForbidden) as refused:
            await new_radio(w)

        assert refused.value.reason == "premium" and await w.hub.store.list_for_guild(GUILD) == []

    @pytest.mark.asyncio
    async def test_the_actor_needs_manage_guild_checked_now_in_the_guild_cache(self):
        w = await setup_world(manage_guild=False)

        with pytest.raises(RadioForbidden) as refused:
            await new_radio(w)

        assert refused.value.reason == "manage_guild" and await w.hub.store.list_for_guild(GUILD) == []

    @pytest.mark.asyncio
    async def test_an_actor_who_cannot_be_found_is_denied(self):
        w = await setup_world()
        for bot in w.bots:
            guild = bot.get_guild(GUILD)
            guild.get_member = lambda uid: None
            guild.fetch_member = AsyncMock(side_effect=disnake.HTTPException(MagicMock(status=404), "x"))

        with pytest.raises(RadioForbidden):
            await new_radio(w)

    @pytest.mark.asyncio
    async def test_the_guild_owner_and_an_administrator_count_as_managers(self):
        w = await setup_world(manage_guild=False)
        for bot in w.bots:
            bot.get_guild(GUILD).owner_id = ACTOR

        assert (await new_radio(w)).enabled is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("bot_id", "code"),
        [(1, "reserve_only"), (999, "reserve_only")],  # o principal e um bot que nem existe
    )
    async def test_only_a_reserve_of_the_pool_can_play(self, bot_id, code):
        w = await setup_world()

        with pytest.raises(RadioInvalid) as refused:
            await new_radio(w, bot_id=bot_id)

        assert code_of(refused) == (code, "bot_id")

    @pytest.mark.asyncio
    async def test_a_bot_that_is_not_ready_is_not_a_reserve_here(self):
        w = await setup_world()
        w.bots[1].bot_ready = False

        with pytest.raises(RadioInvalid) as refused:
            await new_radio(w, bot_id=2)

        assert code_of(refused) == ("reserve_only", "bot_id")

    @pytest.mark.asyncio
    async def test_a_bot_in_a_call_in_this_server_is_refused(self):
        w = await setup_world()
        w.bots[1].get_guild(GUILD).me.voice = object()

        with pytest.raises(RadioInvalid) as refused:
            await new_radio(w, bot_id=2)

        assert code_of(refused) == ("bot_in_call", "bot_id")

    @pytest.mark.asyncio
    async def test_one_radio_per_bot_per_server(self):
        w = await setup_world()
        await new_radio(w, bot_id=2)

        with pytest.raises(RadioInvalid) as refused:
            await new_radio(w, bot_id=2, channel_id=OTHER_CHANNEL)

        assert code_of(refused) == ("one_per_bot", "bot_id")

    @pytest.mark.asyncio
    async def test_at_most_three_radios_are_configured_even_when_stopped(self):
        w = await setup_world(free_bots=(2, 3, 4, 5))
        for n, bot_id in enumerate((2, 3, 4)):
            await new_radio(w, bot_id=bot_id, channel_id=800 + n)

        with pytest.raises(RadioInvalid) as refused:
            await new_radio(w, bot_id=5, channel_id=810)

        assert code_of(refused) == ("max_radios", "bot_id") and len(await w.hub.store.list_for_guild(GUILD)) == 3

    @pytest.mark.asyncio
    async def test_two_radios_never_share_a_channel(self):
        w = await setup_world()
        await new_radio(w, bot_id=2)

        with pytest.raises(RadioInvalid) as refused:
            await new_radio(w, bot_id=3)

        assert code_of(refused) == ("channel_taken", "channel_id")

    @pytest.mark.asyncio
    async def test_the_channel_must_be_a_voice_or_stage_channel_of_the_guild(self):
        w = await setup_world()
        for bot in w.bots:
            bot.get_guild(GUILD).get_channel = lambda cid: MagicMock(spec=disnake.TextChannel)

        with pytest.raises(RadioInvalid) as refused:
            await new_radio(w)

        assert code_of(refused) == ("channel_kind", "channel_id")

    @pytest.mark.asyncio
    async def test_the_jukebox_channel_is_refused(self):
        w = await setup_world(request_channel=CHANNEL)

        with pytest.raises(RadioInvalid) as refused:
            await new_radio(w)

        assert code_of(refused) == ("jukebox_channel", "channel_id")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("missing", ["view_channel", "connect", "speak"])
    async def test_the_bot_must_see_connect_and_speak_in_the_channel(self, missing):
        w = await setup_world()
        perms = dict(view_channel=True, connect=True, speak=True, manage_messages=True)
        perms[missing] = False
        w.channel.permissions_for.side_effect = lambda who: SimpleNamespace(**perms)

        with pytest.raises(RadioInvalid) as refused:
            await new_radio(w)

        assert code_of(refused) == ("cannot_connect", "channel_id")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["jukebox", "", None, "LIVE"])
    async def test_an_unknown_kind_is_refused(self, kind):
        w = await setup_world()

        with pytest.raises(RadioInvalid) as refused:
            await new_radio(w, kind=kind)

        assert refused.value.error.key == "kind"

    @pytest.mark.asyncio
    async def test_a_garbage_channel_id_is_an_invalid_field_not_a_crash(self):
        w = await setup_world()

        with pytest.raises(RadioInvalid) as refused:
            await w.hub.setup.configure(GUILD, ACTOR, radio_id=None, channel_id="x", bot_id=2, kind="custom", rev=None)

        assert refused.value.error.key == "channel_id"


class TestConfigureEdits:
    @pytest.mark.asyncio
    async def test_a_radio_of_another_server_or_a_broken_id_is_not_found(self):
        w = await setup_world()
        other = await configure(w.hub, guild_id=GUILD + 1, channel_id=CHANNEL, bot_id=2)

        for rid in (other.id, "lixo", "2:abc"):
            with pytest.raises(RadioNotFound):
                await w.hub.setup.configure(
                    GUILD, ACTOR, radio_id=rid, channel_id=CHANNEL, bot_id=2, kind="custom", rev=0
                )
        assert (await w.hub.store.get(other.id)).rev == 0  # o documento do outro servidor nem foi tocado

    @pytest.mark.asyncio
    async def test_a_stale_or_missing_rev_is_a_conflict_and_writes_nothing(self):
        w = await setup_world()
        radio = await new_radio(w)

        for rev in (radio.rev + 1, None):
            with pytest.raises(RadioConflict):
                await w.hub.setup.configure(
                    GUILD, ACTOR, radio_id=radio.id, channel_id=OTHER_CHANNEL, bot_id=2, kind="custom", rev=rev
                )
        assert (await w.hub.store.get(radio.id)).channel_id == CHANNEL

    @pytest.mark.asyncio
    async def test_changing_the_type_drops_the_station_and_a_new_channel_drops_the_panel(self):
        w = await setup_world()
        radio = await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)  # custom com estação
        await w.hub.store.set_panel_message(radio.id, 4242)
        radio = await w.hub.store.get(radio.id)

        changed = await w.hub.setup.configure(
            GUILD, ACTOR, radio_id=radio.id, channel_id=OTHER_CHANNEL, bot_id=2, kind="live", rev=radio.rev
        )

        assert (changed.kind, changed.channel_id, changed.source, changed.panel_message_id) == (
            "live",
            OTHER_CHANNEL,
            {},
            None,
        )
        assert changed.rev == radio.rev + 1 and changed.clock_started_at is None

    @pytest.mark.asyncio
    async def test_the_same_values_write_nothing(self):
        w = await setup_world()
        radio = await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)

        same = await w.hub.setup.configure(
            GUILD, ACTOR, radio_id=radio.id, channel_id=CHANNEL, bot_id=2, kind="custom", rev=radio.rev
        )

        assert same == radio and (await w.hub.store.get(radio.id)).rev == radio.rev

    @pytest.mark.asyncio
    async def test_a_running_radio_must_be_turned_off_before_changing_channel_bot_or_kind(self):
        w = await setup_world()
        radio = await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
        running = await w.hub.store.set_enabled(radio.id, True)

        for field, over in (
            ("channel_id", {"channel_id": OTHER_CHANNEL}),
            ("bot_id", {"bot_id": 3}),
            ("kind", {"kind": "live"}),
        ):
            args = {"channel_id": CHANNEL, "bot_id": 2, "kind": "custom", **over}
            with pytest.raises(RadioInvalid) as refused:
                await w.hub.setup.configure(GUILD, ACTOR, radio_id=radio.id, rev=running.rev, **args)
            assert code_of(refused) == ("radio_on", field)
        assert (await w.hub.store.get(radio.id)).enabled is True

    @pytest.mark.asyncio
    async def test_the_running_radio_keeps_its_own_bot_even_though_that_bot_is_in_a_call(self):
        w = await setup_world()
        radio = await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
        running = await w.hub.store.set_enabled(radio.id, True)
        w.bots[1].get_guild(GUILD).me.voice = object()  # o bot da rádio está na call DELA

        same = await w.hub.setup.configure(
            GUILD, ACTOR, radio_id=radio.id, channel_id=CHANNEL, bot_id=2, kind="custom", rev=running.rev
        )

        assert same.enabled is True

    @pytest.mark.asyncio
    async def test_a_new_bot_is_a_new_document_and_the_old_one_is_deleted_after_it(self):
        w = await setup_world()
        old = await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
        await w.hub.store.set_panel_message(old.id, 4242)
        old = await w.hub.store.get(old.id)
        deleted = []
        w.bots[1].get_guild(GUILD).get_channel = lambda cid: _channel_with_panel(w.channel, deleted)

        moved = await w.hub.setup.configure(
            GUILD, ACTOR, radio_id=old.id, channel_id=CHANNEL, bot_id=3, kind="custom", rev=old.rev
        )

        assert moved.id == radio_id(3, GUILD) and moved.source == old.source and moved.panel_message_id is None
        assert await w.hub.store.get(old.id) is None and await w.hub.store.get(moved.id) == moved
        assert len(await w.hub.store.list_for_guild(GUILD)) == 1
        assert deleted == [4242]  # o painel do bot que saiu foi apagado


def _channel_with_panel(channel, deleted):
    wrapper = MagicMock(wraps=channel, spec=disnake.VoiceChannel)
    wrapper.permissions_for = channel.permissions_for

    def partial(message_id):
        message = SimpleNamespace()

        async def delete():
            deleted.append(message_id)

        message.delete = delete
        return message

    wrapper.get_partial_message = partial
    return wrapper


class TestReset:
    @pytest.mark.asyncio
    async def test_it_backs_the_document_up_then_deletes_it_and_keeps_everything_else(self):
        w = await setup_world(free_bots=(2, 3))
        radio = await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
        other = await configure(w.hub, guild_id=GUILD, channel_id=OTHER_CHANNEL, bot_id=3)
        station = await w.hub.stations.create(GUILD, {"name": "Rock", "kind": "genre", "source": {"genre": "rock"}})

        await w.hub.setup.reset(GUILD, ACTOR, radio.id, radio.rev)

        assert await w.hub.store.get(radio.id) is None and await w.hub.store.get(other.id) == other
        assert (await w.hub.stations.get(GUILD, station.id)) is not None and await w.hub.premium.fresh(
            GUILD
        ) is not None
        backups = list(w.db.tables["config_backup"].values())
        assert len(backups) == 1 and backups[0]["radio"]["_id"] == radio.id and backups[0]["guild_id"] == str(GUILD)
        assert backups[0]["expire_at"] > __import__("datetime").datetime.now(__import__("datetime").timezone.utc)

    @pytest.mark.asyncio
    async def test_a_running_radio_is_turned_off_first_and_gives_the_slot_back(self):
        w = await setup_world()
        radio = await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
        running = await w.hub.store.set_enabled(radio.id, True)
        assert (await w.hub.premium.fresh(GUILD)).active == 1

        await w.hub.setup.reset(GUILD, ACTOR, radio.id, running.rev)

        assert await w.hub.store.get(radio.id) is None and (await w.hub.premium.fresh(GUILD)).active == 0

    @pytest.mark.asyncio
    async def test_the_panel_message_of_the_call_is_deleted_and_a_failure_there_never_blocks_the_reset(self):
        w = await setup_world()
        radio = await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
        await w.hub.store.set_panel_message(radio.id, 4242)
        radio = await w.hub.store.get(radio.id)

        async def boom():
            raise disnake.Forbidden(MagicMock(status=403), "x")

        broken = MagicMock(spec=disnake.VoiceChannel)
        broken.get_partial_message = lambda message_id: SimpleNamespace(delete=boom)
        w.bots[1].get_guild(GUILD).get_channel = lambda cid: broken

        await w.hub.setup.reset(GUILD, ACTOR, radio.id, radio.rev)

        assert await w.hub.store.get(radio.id) is None

    @pytest.mark.asyncio
    async def test_a_stale_rev_a_foreign_radio_no_premium_and_no_permission_delete_nothing(self):
        w = await setup_world()
        radio = await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
        foreign = await configure(w.hub, guild_id=GUILD + 1, channel_id=CHANNEL, bot_id=2)

        with pytest.raises(RadioConflict):
            await w.hub.setup.reset(GUILD, ACTOR, radio.id, radio.rev + 1)
        with pytest.raises(RadioNotFound):
            await w.hub.setup.reset(GUILD, ACTOR, foreign.id, 0)
        denied = build(manage_guild=False)
        await premium(denied)
        mine = await configure(denied.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
        with pytest.raises(RadioForbidden):
            await denied.hub.setup.reset(GUILD, ACTOR, mine.id, mine.rev)

        assert await w.hub.store.get(radio.id) == radio and await w.hub.store.get(foreign.id) == foreign
        assert await denied.hub.store.get(mine.id) == mine
        assert "config_backup" not in w.db.tables

    @pytest.mark.asyncio
    async def test_without_the_backup_the_document_is_not_deleted(self):
        w = await setup_world()
        radio = await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
        real = w.db.save_doc_cas

        async def save(id_, doc, *, expected_rev, db_name, collection):
            if db_name == "config_backup":
                raise ConnectionError("mongo fora")
            return await real(id_, doc, expected_rev=expected_rev, db_name=db_name, collection=collection)

        w.db.save_doc_cas = save

        with pytest.raises(RadioUnavailable):
            await w.hub.setup.reset(GUILD, ACTOR, radio.id, radio.rev)

        assert await w.hub.store.get(radio.id) is not None
