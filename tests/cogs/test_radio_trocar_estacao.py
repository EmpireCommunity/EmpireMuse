"""Trocar a estação principal de uma rádio ao vivo LIGADA (item 8): a ação confere direito, premium e saúde, grava com ``rev`` e a rádio troca ao vivo; o
anterior e o próxima andam pela lista de onde a estação foi escolhida; o volume e as favoritas passam pelo painel só para quem está na call."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.i18n import get_i18n
from empiremuse.rules.radio import (
    KIND_LIVE,
    PANEL_MENU_ID,
    PANEL_MUTE_ID,
    PANEL_NEXT_ID,
    PANEL_PREVIOUS_ID,
    PANEL_VOLUME_UP_ID,
    radio_id,
)
from empiremuse.rules.radio_stations import Station
from empiremuse.utils.radio import RadioHub, actions
from empiremuse.utils.radio.actions import Actor, Refused
from empiremuse.utils.radio.favorites import MAX_FAVORITES
from empiremuse.utils.radio.runner import Phase
from tests.cogs.test_radio_ao_vivo import (
    CHANNEL,
    GUILD,
    MAIN,
    RESERVE,
    LivePorts,
    bots,
    make_live,
    premium,
    turn_on,
)
from tests.cogs.test_radio_comandos import build as build_world, fresh_cooldown  # noqa: F401 - autouse
from tests.cogs.test_radio_painel import click, said
from tests.persistence.test_radio_store import FakeDb

pytestmark = pytest.mark.critical


@pytest.fixture
def world():
    db = FakeDb()
    hub = RadioHub(SimpleNamespace(database=db))
    return SimpleNamespace(db=db, hub=hub)


THIRD = "33333333-3333-3333-3333-333333333333"
LIST = [MAIN, RESERVE, THIRD]


def station(uuid, name):
    return Station(uuid, name, "http://stream.example:8000/x", "MP3", 128, "Brasil")


class SwitchingPorts(LivePorts):
    """O dublê ao vivo que também aprende a trocar de estação."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.applied: list = []

    async def apply_station(self, radio):
        self.applied.append(radio)
        self.primary = self.active = radio.source["station"]

    async def set_volume(self, volume):
        self.volume = volume


class TestChangeStationAction:
    @pytest.mark.asyncio
    async def test_it_saves_the_new_main_station_with_the_list_and_swaps_it_live(self, world):
        await premium(world)
        radio = await turn_on(world, station_list=LIST)
        ports = SwitchingPorts()
        world.hub.manager.ports_factory = lambda r: ports
        await world.hub.manager.activate(radio, wait=True)

        updated = await actions.change_station(
            world.hub,
            radio_id=radio.id,
            station=station(RESERVE, "Rádio B"),
            actor=Actor(7, True),
            station_list=[RESERVE, THIRD],
        )
        await asyncio.gather(*list(world.hub.manager._tasks))  # a volta pelo recuo segue em segundo plano

        assert updated.source["station"] == RESERVE and updated.source["list"] == [RESERVE, THIRD]
        assert (await world.hub.store.get(radio.id)).source["station"] == RESERVE
        assert [r.source["station"] for r in ports.applied] == [RESERVE] and ports.live_plays[-1] == RESERVE

    @pytest.mark.asyncio
    async def test_without_a_new_list_the_old_one_stays(self, world):
        await premium(world)
        radio = await turn_on(world, station_list=LIST)

        updated = await actions.change_station(
            world.hub, radio_id=radio.id, station=station(RESERVE, "B"), actor=Actor(7, True)
        )

        assert updated.source["list"] == LIST

    @pytest.mark.asyncio
    async def test_it_refuses_without_the_right_without_premium_for_a_down_station_and_for_a_radio_that_is_off(
        self, world
    ):
        await premium(world)
        radio = await turn_on(world)

        with pytest.raises(Refused) as no_right:
            await actions.change_station(
                world.hub, radio_id=radio.id, station=station(RESERVE, "B"), actor=Actor(7, False)
            )
        world.hub.health._until[RESERVE] = 10**12
        with pytest.raises(Refused) as down:
            await actions.change_station(
                world.hub, radio_id=radio.id, station=station(RESERVE, "B"), actor=Actor(7, True)
            )
        with pytest.raises(Refused) as missing:
            await actions.change_station(world.hub, radio_id="9:9", station=station(THIRD, "C"), actor=Actor(7, True))
        await world.hub.store.set_enabled(radio.id, False)
        with pytest.raises(Refused) as off:
            await actions.change_station(
                world.hub, radio_id=radio.id, station=station(THIRD, "C"), actor=Actor(7, True)
            )

        assert no_right.value.code == actions.NO_MANAGE_GUILD and down.value.code == actions.STATION_DOWN
        assert missing.value.code == actions.NOT_FOUND and off.value.code == actions.NOT_FOUND
        assert (await world.hub.store.get(radio.id)).source["station"] == MAIN

    @pytest.mark.asyncio
    async def test_a_server_without_premium_anymore_cannot_change_station(self, world):
        await premium(world)
        radio = await turn_on(world)
        await world.hub.premium.revoke(GUILD)

        with pytest.raises(Refused) as refused:
            await actions.change_station(
                world.hub, radio_id=radio.id, station=station(RESERVE, "B"), actor=Actor(7, True)
            )

        assert refused.value.code in (actions.NO_PREMIUM, actions.NOT_FOUND)


class TestManagerChangeStation:
    @pytest.mark.asyncio
    async def test_it_is_a_noop_for_a_radio_that_is_not_running_or_not_live(self, world):
        await premium(world)
        radio = await turn_on(world)

        assert await world.hub.manager.change_station(radio) is False  # nada rodando neste processo

    @pytest.mark.asyncio
    async def test_a_runner_without_the_ports_for_it_does_nothing(self, world):
        await premium(world)
        radio = await turn_on(world)
        world.hub.manager.ports_factory = lambda r: LivePorts()  # sem apply_station
        await world.hub.manager.activate(radio, wait=True)

        assert await world.hub.manager.change_station(radio) is False


class TestWalking:
    """Anterior e próxima pelo painel ao vivo, com um mundo de comando de verdade e uma rádio ao vivo no ar."""

    @staticmethod
    async def live_world(list_=LIST, volume=None):
        w = build_world()
        await w.hub.control.load()
        await w.hub.premium.grant(GUILD, by=1)
        ports = SwitchingPorts()
        w.hub.manager.ports_factory = lambda radio: ports
        w.cog.bot.user = SimpleNamespace(id=2)
        picked = {}

        async def by_uuid(uuid):
            return station(uuid, f"Rádio {uuid[:2]}")

        w.hub.directory = SimpleNamespace(by_uuid=by_uuid, logo_png=AsyncMock(), favicon_of=lambda u: None)
        radio = await actions.turn_on(
            w.hub,
            guild_id=GUILD,
            channel_id=CHANNEL,
            playlist=None,
            actor=Actor(7, True, True),
            candidates=bots(2, 3),
            playlist_exists=False,
            bot_can_join=True,
            station=station(MAIN, "Rádio 11"),
            station_list=list_,
        )
        if volume:
            radio = await w.hub.store.set_volume(radio.id, volume, expected_rev=radio.rev)
        await w.hub.manager.activate(radio, wait=True)
        w.ports, w.radio = ports, radio
        assert radio.kind == KIND_LIVE and picked == {}
        return w

    @pytest.mark.asyncio
    async def test_next_and_previous_walk_the_list_and_wrap(self):
        w = await self.live_world()
        seen = []

        for custom_id in (PANEL_NEXT_ID, PANEL_NEXT_ID, PANEL_NEXT_ID, PANEL_PREVIOUS_ID):
            press = click(custom_id)
            await w.cog.radio_panel_click(press)
            seen.append((await w.hub.store.get(w.radio.id)).source["station"])

        assert seen == [RESERVE, THIRD, MAIN, THIRD]

    @pytest.mark.asyncio
    async def test_a_radio_without_a_list_says_there_is_no_other_station(self):
        w = await self.live_world(list_=None)
        press = click(PANEL_NEXT_ID)

        await w.cog.radio_panel_click(press)

        assert "Não há outra estação" in said(press)
        assert (await w.hub.store.get(w.radio.id)).source["station"] == MAIN

    @pytest.mark.asyncio
    async def test_unavailable_stations_in_the_list_are_skipped(self):
        w = await self.live_world()
        w.hub.health._until[RESERVE] = 10**12
        press = click(PANEL_NEXT_ID)

        await w.cog.radio_panel_click(press)

        assert (await w.hub.store.get(w.radio.id)).source["station"] == THIRD

    @pytest.mark.asyncio
    async def test_someone_outside_the_call_cannot_walk_mute_or_change_the_volume(self):
        w = await self.live_world()

        for custom_id in (PANEL_NEXT_ID, PANEL_PREVIOUS_ID, PANEL_MUTE_ID, PANEL_VOLUME_UP_ID):
            press = click(custom_id, in_call=None)
            await w.cog.radio_panel_click(press)
            assert "Só quem está na call" in said(press)

        stored = await w.hub.store.get(w.radio.id)
        assert stored.source["station"] == MAIN and "volume" not in stored.source

    @pytest.mark.asyncio
    async def test_mute_stops_the_stream_and_pressing_again_rejoins_live(self):
        w = await self.live_world()
        runner = w.hub.manager.runner(w.radio.id)
        plays = len(w.ports.live_plays)

        await w.cog.radio_panel_click(click(PANEL_MUTE_ID))
        assert runner.phase is Phase.PAUSED_ADMIN and w.ports.stops >= 1
        await w.cog.radio_panel_click(click(PANEL_MUTE_ID))

        assert (
            runner.phase is Phase.PLAYING and len(w.ports.live_plays) == plays + 1
        )  # um fluxo NOVO: o ponto ao vivo, não o que ficou guardado

    @pytest.mark.asyncio
    async def test_volume_buttons_step_by_10_save_and_apply_and_wait_a_second(self):
        w = await self.live_world()
        first, second = click(PANEL_VOLUME_UP_ID), click(PANEL_VOLUME_UP_ID)

        await w.cog.radio_panel_click(first)
        await w.cog.radio_panel_click(second)  # dentro do segundo seguinte

        stored = await w.hub.store.get(w.radio.id)
        assert "110%" in said(first) and stored.source["volume"] == 110 and w.ports.volume == 110
        assert "Aguarde" in said(second)

    @pytest.mark.asyncio
    async def test_the_volume_never_leaves_10_to_150(self):
        w = await self.live_world(volume=150)

        press = click(PANEL_VOLUME_UP_ID)
        await w.cog.radio_panel_click(press)

        assert (await w.hub.store.get(w.radio.id)).source["volume"] == 150 and "150%" in said(press)


def menu(value, **kwargs):
    press = click(PANEL_MENU_ID, **kwargs)
    press.data = SimpleNamespace(custom_id=PANEL_MENU_ID, values=[value])
    return press


class TestMenu:
    @pytest.mark.asyncio
    async def test_favorite_adds_the_station_to_the_server_list_once(self):
        w = await TestWalking.live_world()
        first, second = menu("favorite"), menu("favorite")

        await w.cog.radio_panel_menu(first)
        await w.cog.radio_panel_menu(second)

        assert "entrou nas favoritas" in said(first) and "já está nas favoritas" in said(second)
        assert [f.uuid for f in await w.hub.favorites.list(GUILD)] == [MAIN]

    @pytest.mark.asyncio
    async def test_the_favorites_list_has_a_limit(self):
        w = await TestWalking.live_world()
        for n in range(MAX_FAVORITES):
            await w.hub.favorites.add(GUILD, station(f"{n:08x}-0000-0000-0000-000000000000", f"R{n}"))
        press = menu("favorite")

        await w.cog.radio_panel_menu(press)

        assert "limite" in said(press)

    @pytest.mark.asyncio
    async def test_info_shows_codec_bitrate_and_country_without_mentions(self):
        w = await TestWalking.live_world()
        press = menu("info")

        await w.cog.radio_panel_menu(press)

        text = said(press)
        assert "MP3" in text and "128" in text and "Brasil" in text
        assert press.edit_original_message.await_args.kwargs["allowed_mentions"].everyone is False

    @pytest.mark.asyncio
    async def test_stop_turns_the_radio_off(self):
        w = await TestWalking.live_world()
        press = menu("stop")

        await w.cog.radio_panel_menu(press)

        assert not w.hub.manager.is_running(w.radio.id) and "desligada" in said(press)

    @pytest.mark.asyncio
    async def test_everything_in_the_menu_is_for_members_in_the_call_only(self):
        w = await TestWalking.live_world()

        for value in ("stations", "favorite", "volume", "info", "stop"):
            press = menu(value, in_call=None)
            await w.cog.radio_panel_menu(press)
            assert "Só quem está na call" in said(press)

        assert w.hub.manager.is_running(w.radio.id) and await w.hub.favorites.list(GUILD) == []

    @pytest.mark.asyncio
    async def test_a_forged_menu_value_and_a_click_on_another_select_are_ignored(self):
        w = await TestWalking.live_world()
        forged = menu("../x")
        await w.cog.radio_panel_menu(forged)
        assert "outro tipo de rádio" in said(forged)

        other = menu("stop")
        other.data = SimpleNamespace(custom_id="player_guild_pin", values=["stop"])
        await w.cog.radio_panel_menu(other)

        other.response.defer.assert_not_awaited()
        assert w.hub.manager.is_running(w.radio.id)

    @pytest.mark.asyncio
    async def test_the_menu_does_nothing_on_a_custom_radio(self):
        w = build_world()
        await w.hub.control.load()
        await w.hub.premium.grant(GUILD, by=1)
        w.cog.bot.user = SimpleNamespace(id=2)
        from tests.cogs.test_radio_comandos import run_on

        await run_on(w, channel=w.channel, playlist="Rock")
        press = menu("stop")

        await w.cog.radio_panel_menu(press)

        assert "outro tipo de rádio" in said(press) and w.hub.manager.is_running(radio_id(2, GUILD))


class TestPickerWiring:
    """O seletor de estação aberto pelo cog (comando e painel) até o Confirmar: a ligação entre ``_picker``, o seletor e a ação (CodeRabbit/revisão: antes quebrava com TypeError)."""

    @pytest.mark.asyncio
    async def test_the_menu_option_opens_a_picker_for_members_in_the_call_that_changes_the_station(self):
        w = await TestWalking.live_world(list_=[MAIN, RESERVE])
        press = menu("stations")

        await w.cog.radio_panel_menu(press)
        view = press.edit_original_message.await_args.kwargs["view"]
        view.chosen = station(THIRD, "Rádio 33")
        confirm = click(PANEL_MENU_ID)
        await view._on_confirm(confirm)

        assert view._deny_key == "radio.panel.call_only"
        assert view._check(click(PANEL_MENU_ID, manage_guild=False)) and not view._check(
            click(PANEL_MENU_ID, in_call=None)
        )
        assert (await w.hub.store.get(w.radio.id)).source["station"] == THIRD
        assert "agora tocando" in said(confirm).lower()

    @pytest.mark.asyncio
    async def test_the_command_picker_turns_the_radio_on_with_the_chosen_station_and_list(self):
        from tests.cogs.test_radio_comandos import settle

        w = build_world()
        await w.hub.control.load()
        await w.hub.premium.grant(GUILD, by=1)
        w.cog.bot.user = SimpleNamespace(id=2)
        w.hub.manager.ports_factory = lambda radio: SwitchingPorts()
        w.hub.directory = SimpleNamespace(by_uuid=AsyncMock(), logo_png=AsyncMock(), favicon_of=lambda u: None)
        tr = await w.cog.bot.pool.i18n.for_guild(w.inter.guild)

        await w.cog._open_start_picker(w.inter, tr, w.channel)
        view = w.inter.edit_original_message.await_args.kwargs["view"]
        view.chosen = station(THIRD, "Rádio 33")
        confirm = click(PANEL_MENU_ID)
        await view._on_confirm(confirm)
        await settle(w)

        radio = await w.hub.store.get(radio_id(2, GUILD))
        assert radio is not None and radio.enabled and radio.source["station"] == THIRD
        assert "ligada" in said(confirm).lower() or "tocando" in said(confirm).lower()
