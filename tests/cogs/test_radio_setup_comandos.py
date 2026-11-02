"""`/radio setup`, `/radio reset` e o prefixo `radio` (contrato-nomes-radios §2): a camada do Discord em cima de ``pool.radio.setup``."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.cogs import radio as radio_cog
from empiremuse.rules.radio import radio_id
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.radio import setup_flow
from tests.cogs.radio_config import CUSTOM_STATION, configure
from tests.cogs.test_radio_comandos import CHANNEL, GUILD, build, fresh_cooldown, premium, reply, settle  # noqa: F401

pytestmark = pytest.mark.critical


async def world_with_station(**kwargs):
    w = build(**kwargs)
    await premium(w)
    station = await w.hub.stations.create(GUILD, {"name": "Rock", "kind": "genre", "source": {"genre": "rock"}})
    return w, station


def run_setup(w, *, bot="2", kind="custom"):
    return radio_cog.Radio.radio_setup.callback(w.cog, w.inter, channel=w.channel, bot=bot, kind=kind)


def click_of(user_id=7, values=None):
    return SimpleNamespace(
        author=SimpleNamespace(id=user_id, guild_permissions=SimpleNamespace(manage_guild=True)),
        guild_id=GUILD,
        values=values or [],
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), edit_message=AsyncMock()),
        edit_original_message=AsyncMock(),
    )


class TestSetupCommand:
    @pytest.mark.asyncio
    async def test_a_custom_radio_is_configured_and_asks_for_one_of_the_server_stations(self):
        w, station = await world_with_station()

        await run_setup(w)

        radio = await w.hub.store.get(radio_id(2, GUILD))
        assert radio.kind == "custom" and radio.source == {} and radio.enabled is False
        sent = w.inter.edit_original_message.await_args.kwargs
        assert (
            f"<#{CHANNEL}>" in sent["content"]
            and "<@2>" in sent["content"]
            and "/radio station create" in sent["content"]
        )
        assert sent["allowed_mentions"].everyone is False
        options = sent["view"].children[0].options
        assert [o.value for o in options] == [station.id]

    @pytest.mark.asyncio
    async def test_picking_the_station_saves_it_and_leaves_the_radio_off(self):
        w, station = await world_with_station()
        await run_setup(w)
        view = w.inter.edit_original_message.await_args.kwargs["view"]
        choice = click_of(values=[station.id])

        await view.picked(choice)

        radio = await w.hub.store.get(radio_id(2, GUILD))
        assert radio.source == {"station": station.id} and radio.enabled is False
        assert "/radio on" in choice.edit_original_message.await_args.kwargs["content"]

    @pytest.mark.asyncio
    async def test_a_server_without_stations_is_told_where_to_create_one(self):
        w = build()
        await premium(w)

        await run_setup(w)

        assert "/radio station create" in reply(w)

    @pytest.mark.asyncio
    async def test_running_setup_again_on_the_same_channel_edits_that_radio(self):
        w, _ = await world_with_station()
        await run_setup(w)
        radio_cog.RADIO_COOLDOWN._cache.clear()

        await radio_cog.Radio.radio_setup.callback(w.cog, w.inter, channel=w.channel, bot="2", kind="live")

        radios = await w.hub.store.list_for_guild(GUILD)
        assert [(r.id, r.kind, r.rev) for r in radios] == [(radio_id(2, GUILD), "live", 1)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("setup_world", "snippet"),
        [
            ({"premium": False}, "premium"),
            ({"manage_guild": False}, "Gerenciar Servidor"),
        ],
    )
    async def test_the_refusals_of_the_shared_function_become_translated_messages(self, setup_world, snippet):
        w = build(manage_guild=setup_world.get("manage_guild", True))
        if setup_world.get("premium", True):
            await premium(w)
        else:
            await w.hub.control.load()

        with pytest.raises(GenericError) as refused:
            await run_setup(w)

        assert snippet in str(refused.value) and await w.hub.store.list_for_guild(GUILD) == []

    @pytest.mark.asyncio
    async def test_a_bot_already_in_a_call_says_so(self):
        w, _ = await world_with_station()
        w.bots[1].get_guild(GUILD).me.voice = object()

        with pytest.raises(GenericError) as refused:
            await run_setup(w)

        assert "call" in str(refused.value)

    @pytest.mark.asyncio
    async def test_a_bot_that_is_not_an_id_is_an_error_not_a_crash(self):
        w, _ = await world_with_station()

        with pytest.raises(GenericError):
            await run_setup(w, bot="TOKEN_2")


class TestStationSelectIsOnlyForWhoOpenedIt:
    @pytest.mark.asyncio
    async def test_another_user_is_refused_and_the_radio_is_untouched(self):
        w, station = await world_with_station()
        await run_setup(w)
        view = w.inter.edit_original_message.await_args.kwargs["view"]
        stranger = click_of(user_id=99)

        assert await view.interaction_check(stranger) is False

        assert (await w.hub.store.get(radio_id(2, GUILD))).source == {}

    @pytest.mark.asyncio
    async def test_a_station_deleted_meanwhile_is_not_saved(self):
        w, station = await world_with_station()
        await run_setup(w)
        view = w.inter.edit_original_message.await_args.kwargs["view"]

        with pytest.raises(GenericError):
            await view.picked(click_of(values=["0" * 24]))  # um id que o select nunca ofereceu

        assert (await w.hub.store.get(radio_id(2, GUILD))).source == {}

    @pytest.mark.asyncio
    async def test_premium_revoked_while_the_select_was_open_stops_the_save(self):
        w, station = await world_with_station()
        await run_setup(w)
        view = w.inter.edit_original_message.await_args.kwargs["view"]
        await w.hub.store.revoke_premium(GUILD)

        with pytest.raises(GenericError) as refused:
            await view.picked(click_of(values=[station.id]))

        assert "premium" in str(refused.value) and (await w.hub.store.get(radio_id(2, GUILD))).source == {}


class TestResetCommand:
    @pytest.mark.asyncio
    async def test_it_asks_for_confirmation_before_touching_anything(self):
        w = build()
        await premium(w)
        await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)

        await radio_cog.Radio.radio_reset.callback(w.cog, w.inter, channel=w.channel)

        sent = w.inter.edit_original_message.await_args.kwargs
        assert "<@2>" in sent["content"] and [c.label for c in sent["view"].children] == ["Redefinir", "Cancelar"]
        assert await w.hub.store.get(radio_id(2, GUILD)) is not None

    @pytest.mark.asyncio
    async def test_confirming_deletes_it_and_cancelling_does_not(self):
        w = build()
        await premium(w)
        await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
        await radio_cog.Radio.radio_reset.callback(w.cog, w.inter, channel=w.channel)
        view = w.inter.edit_original_message.await_args.kwargs["view"]

        cancel = click_of()
        await view.cancelled(cancel)
        assert await w.hub.store.get(radio_id(2, GUILD)) is not None

        confirm = click_of()
        await view.confirmed(confirm)
        assert await w.hub.store.get(radio_id(2, GUILD)) is None
        assert "redefinida" in confirm.edit_original_message.await_args.kwargs["content"]

    @pytest.mark.asyncio
    async def test_a_changed_radio_between_the_question_and_the_click_is_a_conflict_message(self):
        w = build()
        await premium(w)
        radio = await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
        await radio_cog.Radio.radio_reset.callback(w.cog, w.inter, channel=w.channel)
        view = w.inter.edit_original_message.await_args.kwargs["view"]
        await w.hub.store.set_panel_message(radio.id, 1)  # alguém mexeu na rádio: o rev andou

        confirm = click_of()
        await view.confirmed(confirm)

        assert await w.hub.store.get(radio.id) is not None
        assert "mudou" in confirm.edit_original_message.await_args.kwargs["content"]

    @pytest.mark.asyncio
    async def test_only_who_asked_can_confirm_and_a_channel_without_radio_says_so(self):
        w = build()
        await premium(w)
        await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
        await radio_cog.Radio.radio_reset.callback(w.cog, w.inter, channel=w.channel)
        view = w.inter.edit_original_message.await_args.kwargs["view"]

        assert await view.interaction_check(click_of(user_id=99)) is False

        radio_cog.RADIO_COOLDOWN._cache.clear()
        other = SimpleNamespace(id=555, mention="<#555>")
        with pytest.raises(GenericError) as refused:
            await radio_cog.Radio.radio_reset.callback(w.cog, w.inter, channel=other)
        assert "<#555>" in str(refused.value)

    @pytest.mark.asyncio
    async def test_without_manage_guild_the_question_is_not_even_asked(self):
        w = build(manage_guild=False)
        await premium(w)

        with pytest.raises(GenericError):
            await radio_cog.Radio.radio_reset.callback(w.cog, w.inter, channel=w.channel)


class TestBotAutocomplete:
    @pytest.mark.asyncio
    async def test_it_offers_the_free_reserves_as_name_to_id_only_to_a_premium_manager(self):
        w = build()
        await premium(w)
        inter = SimpleNamespace(
            guild_id=GUILD,
            author=SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=True)),
            bot=SimpleNamespace(pool=w.bots[0].pool if hasattr(w.bots[0], "pool") else w.cog.bot.pool),
        )

        assert await radio_cog.bot_autocomplete(inter, "") == {"Bot 2": "2", "Bot 3": "3"}
        assert await radio_cog.bot_autocomplete(inter, "3") == {"Bot 3": "3"}
        inter.author.guild_permissions.manage_guild = False
        assert await radio_cog.bot_autocomplete(inter, "") == {}

    @pytest.mark.asyncio
    async def test_without_premium_nothing_is_offered(self):
        w = build()
        await w.hub.control.load()
        inter = SimpleNamespace(
            guild_id=GUILD,
            author=SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=True)),
            bot=w.cog.bot,
        )

        assert await radio_cog.bot_autocomplete(inter, "") == {}


def ctx_for(w, *, channel=None, manage_guild=True):
    voice = SimpleNamespace(channel=channel) if channel is not None else None
    return SimpleNamespace(
        author=SimpleNamespace(id=7, voice=voice, guild_permissions=SimpleNamespace(manage_guild=manage_guild)),
        guild=SimpleNamespace(id=GUILD, preferred_locale="pt-BR"),
        message=w.inter,
        send=AsyncMock(),
    )


class TestPrefixSubcommands:
    @pytest.mark.asyncio
    async def test_on_uses_the_channel_given_or_the_one_the_author_is_in(self):
        w = build()
        await premium(w)
        await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
        ctx = ctx_for(w, channel=w.channel)

        await w.cog.prefix_on.callback(w.cog, ctx)
        await settle(w)

        assert w.hub.manager.is_running(radio_id(2, GUILD)) and "<@2>" in ctx.send.await_args.args[0]

    @pytest.mark.asyncio
    async def test_off_and_status_answer_in_the_channel(self):
        w = build()
        await premium(w)
        await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
        await w.cog.prefix_on.callback(w.cog, ctx_for(w, channel=w.channel))
        await settle(w)
        radio_cog.RADIO_COOLDOWN._cache.clear()

        status = ctx_for(w)
        await w.cog.prefix_status.callback(w.cog, status)
        radio_cog.RADIO_COOLDOWN._cache.clear()
        off = ctx_for(w)
        await w.cog.prefix_off.callback(w.cog, off, w.channel)

        assert f"<#{CHANNEL}>" in status.send.await_args.args[0]
        assert "desligada" in off.send.await_args.args[0] and not w.hub.manager.is_running(radio_id(2, GUILD))

    @pytest.mark.asyncio
    async def test_without_a_channel_and_outside_a_call_it_asks_for_one(self):
        w = build()
        await premium(w)

        with pytest.raises(GenericError) as refused:
            await w.cog.prefix_on.callback(w.cog, ctx_for(w))

        assert "canal" in str(refused.value)

    @pytest.mark.asyncio
    async def test_the_prefix_applies_the_same_ten_second_cooldown(self):
        w = build()
        await premium(w)
        ctx = ctx_for(w)
        await w.cog.prefix_status.callback(w.cog, ctx)

        with pytest.raises(GenericError) as refused:
            await w.cog.prefix_status.callback(w.cog, ctx)

        assert "Aguarde" in str(refused.value)

    @pytest.mark.asyncio
    async def test_reset_by_prefix_asks_and_setup_points_to_the_slash_command(self):
        w = build()
        await premium(w)
        await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
        ctx = ctx_for(w, channel=w.channel)

        await w.cog.prefix_reset.callback(w.cog, ctx)
        await w.cog.prefix_setup.callback(w.cog, ctx)

        assert ctx.send.await_args_list[0].kwargs["view"] is not None
        assert "/radio setup" in ctx.send.await_args_list[1].args[0]
        assert await w.hub.store.get(radio_id(2, GUILD)) is not None
