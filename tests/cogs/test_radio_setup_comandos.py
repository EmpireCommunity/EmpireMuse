"""`/radio setup`, `/radio reset` e o prefixo `radio` (contrato-nomes-radios §2): a camada do Discord em cima de ``pool.radio.setup``."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.cogs import radio as radio_cog
from empiremuse.rules.radio import radio_id
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.radio import radio_commands, setup_flow
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
    async def test_setup_turns_the_radio_on_without_a_station_and_asks_for_none(self):
        """R1: o setup liga a rádio (o bot entra na call sozinho), sem seletor de estação; a estação é escolhida no painel."""
        w, _ = await world_with_station()

        await run_setup(w)
        await settle(w)

        radio = await w.hub.store.get(radio_id(2, GUILD))
        assert radio.kind == "custom" and radio.source == {} and radio.enabled is True
        sent = w.inter.edit_original_message.await_args.kwargs
        assert f"<#{CHANNEL}>" in sent["content"] and "<@2>" in sent["content"] and "painel" in sent["content"]
        assert "view" not in sent  # nenhum seletor dentro do setup
        assert sent["allowed_mentions"].everyone is False
        assert w.hub.manager.is_running(radio_id(2, GUILD)) and w.ports.connects == 1  # entrou na call na hora

    @pytest.mark.asyncio
    async def test_the_arrival_purges_the_voice_chat_once_before_the_panel(self):
        w, _ = await world_with_station()

        await run_setup(w)
        await settle(w)

        assert w.ports.purge_on_arrival is True  # o gerente marcou a chegada para limpar o chat antes do 1º painel

    @pytest.mark.asyncio
    async def test_a_server_without_stations_still_gets_the_radio_and_the_panel_flow(self):
        w = build()
        await premium(w)

        await run_setup(w)

        assert "painel" in reply(w) and (await w.hub.store.get(radio_id(2, GUILD))).enabled is True

    @pytest.mark.asyncio
    async def test_without_manage_messages_the_configuration_stays_and_the_reply_says_why_it_did_not_start(self):
        w = build(manage_messages=False)
        await premium(w)

        await run_setup(w)

        radio = await w.hub.store.get(radio_id(2, GUILD))
        assert radio is not None and radio.enabled is False and "Gerenciar Mensagens" in reply(w)

    @pytest.mark.asyncio
    async def test_running_setup_again_on_the_same_channel_edits_that_radio(self):
        w, _ = await world_with_station()
        await run_setup(w)
        await w.hub.manager.stop_radios(w.hub.manager.running_in(GUILD, CHANNEL))
        await w.hub.store.set_enabled(radio_id(2, GUILD), False)
        radio_cog.RADIO_COOLDOWN._cache.clear()

        await radio_cog.Radio.radio_setup.callback(w.cog, w.inter, channel=w.channel, bot="2", kind="live")

        radios = await w.hub.store.list_for_guild(GUILD)
        assert [(r.id, r.kind) for r in radios] == [(radio_id(2, GUILD), "live")]

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
        await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2, source={})
        view = setup_flow.StationSelect(
            w.cog, await w.cog.bot.pool.i18n.for_guild(w.inter.guild), radio_id(2, GUILD), 7, [station]
        )
        stranger = click_of(user_id=99)

        assert await view.interaction_check(stranger) is False

        assert (await w.hub.store.get(radio_id(2, GUILD))).source == {}

    @pytest.mark.asyncio
    async def test_a_station_deleted_meanwhile_is_not_saved(self):
        w, station = await world_with_station()
        await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2, source={})
        view = setup_flow.StationSelect(
            w.cog, await w.cog.bot.pool.i18n.for_guild(w.inter.guild), radio_id(2, GUILD), 7, [station]
        )

        with pytest.raises(GenericError):
            await view.picked(click_of(values=["0" * 24]))  # um id que o select nunca ofereceu

        assert (await w.hub.store.get(radio_id(2, GUILD))).source == {}

    @pytest.mark.asyncio
    async def test_premium_revoked_while_the_select_was_open_stops_the_save(self):
        w, station = await world_with_station()
        await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2, source={})
        view = setup_flow.StationSelect(
            w.cog, await w.cog.bot.pool.i18n.for_guild(w.inter.guild), radio_id(2, GUILD), 7, [station]
        )
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

        await radio_cog.Radio.radio_reset.callback(w.cog, w.inter, channel=None, bot=None)

        sent = w.inter.edit_original_message.await_args.kwargs
        assert "<@2>" in sent["content"] and [c.label for c in sent["view"].children] == ["Redefinir", "Cancelar"]
        assert await w.hub.store.get(radio_id(2, GUILD)) is not None

    @pytest.mark.asyncio
    async def test_confirming_deletes_it_and_cancelling_does_not(self):
        w = build()
        await premium(w)
        await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
        await radio_cog.Radio.radio_reset.callback(w.cog, w.inter, channel=None, bot=None)
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
        await radio_cog.Radio.radio_reset.callback(w.cog, w.inter, channel=None, bot=None)
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
        await radio_cog.Radio.radio_reset.callback(w.cog, w.inter, channel=None, bot=None)
        view = w.inter.edit_original_message.await_args.kwargs["view"]

        assert await view.interaction_check(click_of(user_id=99)) is False

        radio_cog.RADIO_COOLDOWN._cache.clear()
        with pytest.raises(
            GenericError
        ) as refused:  # um bot que não serve rádio nenhuma (digitado à mão) é recusado com o motivo
            await radio_cog.Radio.radio_reset.callback(w.cog, w.inter, channel=None, bot="555")
        assert "/radio setup" in str(refused.value)

    @pytest.mark.asyncio
    async def test_channel_alone_points_at_that_radio_and_none_resets_all(self):
        """Dono: /radio reset aceita canal E bot, os dois opcionais; sem nenhum, todas as rádios do servidor."""
        w = build()
        await premium(w)
        await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
        await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL + 1, bot_id=3)

        await radio_cog.Radio.radio_reset.callback(w.cog, w.inter, channel=str(CHANNEL + 1), bot=None)
        one = w.inter.edit_original_message.await_args.kwargs
        assert [r.channel_id for r in one["view"].radios] == [CHANNEL + 1] and f"<#{CHANNEL + 1}>" in one["content"]

        radio_cog.RADIO_COOLDOWN._cache.clear()
        await radio_cog.Radio.radio_reset.callback(w.cog, w.inter, channel=None, bot=None)
        assert len(w.inter.edit_original_message.await_args.kwargs["view"].radios) == 2

    @pytest.mark.asyncio
    async def test_a_server_without_premium_can_still_ask_and_confirm_the_reset(self):
        w = build()
        await w.hub.control.load()  # sem premium
        await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
        await radio_cog.Radio.radio_reset.callback(w.cog, w.inter, channel=None, bot=None)
        view = w.inter.edit_original_message.await_args.kwargs["view"]

        confirm = click_of()
        await view.confirmed(confirm)

        assert await w.hub.store.get(radio_id(2, GUILD)) is None
        assert "redefinida" in confirm.edit_original_message.await_args.kwargs["content"]

    @pytest.mark.asyncio
    async def test_without_manage_guild_the_question_is_not_even_asked(self):
        w = build(manage_guild=False)
        await premium(w)

        with pytest.raises(GenericError):
            await radio_cog.Radio.radio_reset.callback(w.cog, w.inter, channel=None, bot=None)


class TestBotAutocomplete:
    @pytest.mark.asyncio
    async def test_it_offers_the_free_reserves_as_name_to_id_only_to_a_premium_manager(self):
        w = build()
        await premium(w)
        inter = SimpleNamespace(
            guild_id=GUILD,
            author=SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=True)),
            guild=w.inter.guild,
            bot=SimpleNamespace(pool=w.bots[0].pool if hasattr(w.bots[0], "pool") else w.cog.bot.pool),
        )

        assert await radio_cog.bot_autocomplete(inter, "") == {"Bot 2": "2", "Bot 3": "3"}
        assert await radio_cog.bot_autocomplete(inter, "3") == {"Bot 3": "3"}
        inter.author.guild_permissions.manage_guild = False
        assert list(await radio_cog.bot_autocomplete(inter, "")) == [
            "Você precisa da permissão Gerenciar Servidor para configurar uma rádio."
        ]

    @pytest.mark.asyncio
    async def test_without_premium_one_entry_says_why_instead_of_an_empty_list(self):
        w = build()
        await w.hub.control.load()
        inter = SimpleNamespace(
            guild_id=GUILD,
            guild=w.inter.guild,
            author=SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=True)),
            bot=w.cog.bot,
        )

        offered = await radio_cog.bot_autocomplete(inter, "")

        assert offered == {"Este servidor não tem premium: as rádios exigem.": radio_cog.NO_CHOICE}

    @pytest.mark.asyncio
    async def test_with_every_reserve_taken_the_entry_and_the_command_name_each_channel_and_point_to_reset(self):
        w = build()
        await premium(w)
        await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
        await configure(w.hub, guild_id=GUILD, channel_id=901, bot_id=3)
        inter = SimpleNamespace(
            guild_id=GUILD,
            guild=w.inter.guild,
            author=SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=True)),
            bot=w.cog.bot,
        )

        offered = await radio_cog.bot_autocomplete(inter, "")
        with pytest.raises(GenericError) as refused:
            await run_setup(w, bot=radio_cog.NO_CHOICE)

        assert list(offered.values()) == [radio_cog.NO_CHOICE] and "/radio reset" in next(iter(offered))
        text = str(refused.value)
        assert "Bot 2" in text and f"<#{CHANNEL}>" in text and "Bot 3" in text and "<#901>" in text
        assert "/radio reset" in text


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


class TestPickingTheRadio:
    """R2, R2b e R4: canal OU bot, só de rádios que existem; o reset sem bot apaga todas (com a confirmação)."""

    @staticmethod
    def inter_of(w):
        return SimpleNamespace(
            guild_id=GUILD,
            guild=w.inter.guild,
            author=SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=True)),
            bot=w.cog.bot,
        )

    @pytest.mark.asyncio
    async def test_the_lists_offer_only_channels_and_bots_that_have_a_radio(self):
        w = build()
        await premium(w)
        await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
        inter = self.inter_of(w)

        channels = await radio_commands.channel_autocomplete(inter, "")
        bots = await radio_commands.bot_autocomplete(inter, "")

        assert list(channels.values()) == [str(CHANNEL)] and bots == {"Bot 2": "2"}  # o Bot 3 (livre) não aparece
        assert await radio_commands.bot_autocomplete(inter, "zzz") == {}

    @pytest.mark.asyncio
    async def test_on_and_off_need_a_channel_or_a_bot(self):
        w = build()
        await premium(w)

        for command in (radio_cog.Radio.radio_on, radio_cog.Radio.radio_off):
            radio_cog.RADIO_COOLDOWN._cache.clear()
            with pytest.raises(GenericError) as refused:
                await command.callback(w.cog, w.inter, channel=None, bot=None)
            assert "canal OU um bot" in str(refused.value)

    @pytest.mark.asyncio
    async def test_a_bot_alone_is_enough_and_a_channel_and_bot_that_disagree_are_refused(self):
        w = build()
        await premium(w)
        await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)

        await radio_cog.Radio.radio_on.callback(w.cog, w.inter, channel=None, bot="2")
        radio_cog.RADIO_COOLDOWN._cache.clear()
        with pytest.raises(GenericError):
            await radio_cog.Radio.radio_on.callback(w.cog, w.inter, channel=str(CHANNEL), bot="3")

        assert w.hub.manager.is_running(radio_id(2, GUILD))

    @pytest.mark.asyncio
    async def test_reset_without_a_bot_resets_every_radio_of_the_server_after_the_confirmation(self):
        w = build()
        await premium(w)
        await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
        await configure(w.hub, guild_id=GUILD, channel_id=901, bot_id=3)

        await radio_cog.Radio.radio_reset.callback(w.cog, w.inter, channel=None, bot=None)
        sent = w.inter.edit_original_message.await_args.kwargs
        assert "TODAS" in sent["content"] and len(await w.hub.store.list_for_guild(GUILD)) == 2  # nada antes do botão

        await sent["view"].confirmed(click_of())

        assert await w.hub.store.list_for_guild(GUILD) == []

    @pytest.mark.asyncio
    async def test_reset_with_a_bot_touches_only_that_radio(self):
        w = build()
        await premium(w)
        await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
        await configure(w.hub, guild_id=GUILD, channel_id=901, bot_id=3)

        await radio_cog.Radio.radio_reset.callback(w.cog, w.inter, channel=None, bot="3")
        await w.inter.edit_original_message.await_args.kwargs["view"].confirmed(click_of())

        assert [r.bot_id for r in await w.hub.store.list_for_guild(GUILD)] == [2]


class TestStationChosenInThePanelAfterSetup:
    """B4: a estação escolhida no painel de uma rádio recém-configurada (ligada, sem estação) é gravada e a rádio toca."""

    @pytest.mark.asyncio
    async def test_a_live_radio_waiting_for_its_station_saves_and_plays_the_one_picked(self):
        from empiremuse.rules.radio_stations import Station
        from empiremuse.utils.radio import actions
        from empiremuse.utils.radio.actions import Actor
        from tests.cogs.radio_config import LIVE_STATION

        from tests.cogs.test_radio_ao_vivo import LivePorts

        w, _ = await world_with_station()
        live_ports = LivePorts()
        live_ports.source = False  # sem estação ainda
        live_ports.apply_station = AsyncMock()
        w.hub.manager.ports_factory = lambda radio: live_ports
        await run_setup(w, kind="live")
        await settle(w)
        assert w.hub.manager.runner(radio_id(2, GUILD)).phase.value == "standby"
        live_ports.source = True  # o painel grava a estação: as portas passam a ter fonte
        wanted = Station(LIVE_STATION, "Rock FM", "http://stream.example:8000/x", "MP3", 128, "Brasil")

        saved = await actions.change_station(
            w.hub, radio_id=radio_id(2, GUILD), station=wanted, actor=Actor(user_id=7, manage_guild=True)
        )
        await settle(w)

        assert (
            saved.source["station"] == LIVE_STATION
            and w.hub.manager.runner(radio_id(2, GUILD)).phase.value == "playing"
        )
        assert (await w.hub.store.get(radio_id(2, GUILD))).source["station"] == LIVE_STATION
