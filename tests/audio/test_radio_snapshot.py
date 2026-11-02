"""R6 (lado do bot): skin e último filtro gravados na rádio, o recuo visível e o retrato imutável do painel."""

import dataclasses
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.rules.radio import DEFAULT_SKIN, SKINS, check_radio, radio_id, to_document
from empiremuse.rules.radio_stations import Station
from empiremuse.utils.radio.panel import LiveView
from empiremuse.utils.radio.picker import FAVORITES_ID
from empiremuse.utils.radio.runner import Phase
from empiremuse.utils.radio.snapshot import WINDOW, StationOption, _window, live_snapshot
from empiremuse.utils.radio.store import RadioConflict, RadioInvalid
from tests.cogs.radio_config import LIVE_STATION, configure
from tests.cogs.test_radio_comandos import GUILD, CHANNEL, build, premium
from tests.cogs.test_radio_runner import FakePorts, make


def uuid_of(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


class TestSkinAndFilterInTheDocument:
    def test_an_old_document_without_them_gets_the_defaults_and_they_round_trip(self):
        radio = check_radio({"_id": radio_id(2, GUILD), "channel_id": str(CHANNEL), "kind": "live", "source": {}})

        assert radio.skin == DEFAULT_SKIN and radio.filter is None
        again = check_radio(to_document(dataclasses.replace(radio, skin="white", filter="f:fav")))
        assert (again.skin, again.filter) == ("white", "f:fav")

    @pytest.mark.parametrize("skin", ["", "blue", None, 3])
    def test_only_the_known_skins_are_accepted(self, skin):
        raw = {"_id": radio_id(2, GUILD), "channel_id": str(CHANNEL), "kind": "live", "source": {}, "skin": skin}

        assert not hasattr(check_radio(raw), "skin") and SKINS == ("black", "white")

    @pytest.mark.parametrize("value", ["", "a b", "x" * 60, 5])
    def test_a_filter_must_look_like_a_tree_node_id(self, value):
        raw = {"_id": radio_id(2, GUILD), "channel_id": str(CHANNEL), "kind": "live", "source": {}, "filter": value}

        assert not hasattr(check_radio(raw), "filter")

    @pytest.mark.asyncio
    async def test_the_store_saves_both_with_the_rev_and_refuses_the_rest(self):
        w = build()
        await premium(w)
        radio = await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2, kind="live")

        saved = await w.hub.store.set_skin(radio.id, "white", expected_rev=radio.rev)
        saved = await w.hub.store.set_filter(radio.id, "br:sp", expected_rev=saved.rev)

        assert (saved.skin, saved.filter) == ("white", "br:sp")
        with pytest.raises(RadioInvalid):
            await w.hub.store.set_skin(radio.id, "neon", expected_rev=saved.rev)
        with pytest.raises(RadioConflict):
            await w.hub.store.set_filter(radio.id, "br", expected_rev=radio.rev)  # rev velho


class TestRetryAndOnAir:
    @pytest.mark.asyncio
    async def test_while_backing_off_the_runner_says_how_long_is_left_and_nothing_when_playing(self):
        ports = FakePorts()
        ports.connect_results.extend([False])
        runner, ports, _ = make(ports)
        seen = []
        original = ports.sleep

        async def spy(seconds):
            seen.append(runner.retry_seconds())
            await original(seconds)

        ports.sleep = spy

        await runner.start()

        assert seen and seen[0] is not None and seen[0] > 0
        assert runner.phase is Phase.PLAYING and runner.retry_seconds() is None and runner.on_air_since is not None


class TestWindow:
    def test_the_window_holds_the_active_station_and_never_exceeds_the_select_limit(self):
        ids = [uuid_of(n) for n in range(100)]

        position, window = _window(ids, ids[60])

        assert position == 61 and ids[60] in window and len(window) == WINDOW

    def test_near_the_edges_it_stays_inside_the_list(self):
        ids = [uuid_of(n) for n in range(40)]

        assert _window(ids, ids[0])[1][0] == ids[0] and _window(ids, ids[39])[1][-1] == ids[39]

    def test_an_active_station_outside_the_list_is_position_zero(self):
        ids = [uuid_of(n) for n in range(5)]

        assert _window(ids, uuid_of(99)) == (0, ids)


class TestSnapshot:
    @staticmethod
    def world(*, filter_id=None, view=None):
        ids = [uuid_of(n) for n in range(1, 31)]
        stations = {u: Station(u, f"Rádio {i}", "http://x", "MP3", 128, "Brasil") for i, u in enumerate(ids, 1)}
        hub = SimpleNamespace(
            directory=SimpleNamespace(by_uuids=AsyncMock(return_value=stations)),
            health=SimpleNamespace(unavailable=lambda u: u == ids[1]),
            favorites=SimpleNamespace(list=AsyncMock(return_value=[SimpleNamespace(uuid=ids[0])])),
            locations=None,
        )
        radio = check_radio(
            {
                "_id": radio_id(2, GUILD),
                "channel_id": str(CHANNEL),
                "kind": "live",
                "source": {"station": ids[0], "reserve": None, "list": ids},
                "skin": "white",
                "filter": filter_id,
            }
        )
        ports = SimpleNamespace(
            active=ids[0],
            station_list=tuple(ids),
            _view=view or LiveView(title="Artista - Música"),
            _names={},
            volume=120,
            wall=lambda: 10_000.0,
            on_reserve=lambda: False,
        )
        runner = SimpleNamespace(on_air_since=10_000.0 - 150, retry_seconds=lambda: None)
        return hub, radio, ports, runner, ids

    @pytest.mark.asyncio
    async def test_it_carries_everything_the_panel_draws(self):
        hub, radio, ports, runner, ids = self.world()

        snap = await live_snapshot(hub, radio, ports, runner, locale="pt-BR")

        assert (snap.skin, snap.station_name, snap.bitrate_kbps, snap.quality) == ("white", "Rádio 1", 128, "ok")
        assert (snap.now_title, snap.now_artist, snap.state, snap.volume) == ("Música", "Artista", "live", 120)
        assert (snap.position, snap.total, snap.on_air_minutes, snap.favorite) == (1, 30, 2, True)
        assert len(snap.stations) == WINDOW and snap.current_index == 0 and snap.list_kind == "stations"
        assert isinstance(snap.stations[0], StationOption) and snap.stations[1].quality == "failing"

    @pytest.mark.asyncio
    async def test_muted_offline_and_the_favorites_list_are_reported(self):
        hub, radio, ports, runner, _ = self.world(filter_id=FAVORITES_ID, view=LiveView(muted=True))
        assert (await live_snapshot(hub, radio, ports, runner, locale="pt-BR")).state == "muted"
        ports._view = LiveView(offline=True)
        runner.retry_seconds = lambda: 12

        snap = await live_snapshot(hub, radio, ports, runner, locale="pt-BR")

        assert (snap.state, snap.retry_seconds, snap.list_kind) == ("offline", 12, "favorites")

    @pytest.mark.asyncio
    async def test_a_failing_directory_still_gives_a_snapshot(self):
        hub, radio, ports, runner, _ = self.world()
        hub.directory.by_uuids = AsyncMock(side_effect=OSError)
        ports._names = {ports.active: "Nome em cache"}

        snap = await live_snapshot(hub, radio, ports, runner, locale="pt-BR")

        assert snap.station_name == "Nome em cache" and snap.bitrate_kbps == 0


class TestPanelHandlers:
    """Os cliques novos do painel (reconectar, aleatória, selects): o valor do cliente nunca é confiado."""

    @staticmethod
    def cog_with(ids, *, current=None):
        from empiremuse.utils.music.errors import GenericError  # noqa: F401

        changed = []
        cog = SimpleNamespace(
            hub=SimpleNamespace(
                health=SimpleNamespace(unavailable=lambda u: u == ids[2]),
                directory=SimpleNamespace(
                    by_uuid=AsyncMock(side_effect=lambda u: Station(u, "N", "http://x", "MP3", 128, "BR"))
                ),
            ),
            _too_soon=lambda *a: False,
            _authorized=lambda i: SimpleNamespace(user_id=1, manage_guild=True),
            changed=changed,
        )
        radio = SimpleNamespace(id="2:5", source={"station": current or ids[0], "list": ids}, guild_id=5)
        return cog, radio

    @pytest.mark.asyncio
    async def test_the_station_select_only_accepts_a_station_of_the_radios_list(self, monkeypatch):
        from empiremuse.utils.music.errors import GenericError
        from empiremuse.utils.radio import actions, panel_handlers

        ids = [uuid_of(n) for n in range(1, 5)]
        cog, radio = self.cog_with(ids)
        applied = AsyncMock()
        monkeypatch.setattr(actions, "change_station", applied)
        tr = lambda key, **kw: key  # noqa: E731

        with pytest.raises(GenericError):
            await panel_handlers.pick_station(cog, SimpleNamespace(), tr, radio, uuid_of(99))  # fora da lista
        with pytest.raises(GenericError):
            await panel_handlers.pick_station(cog, SimpleNamespace(), tr, radio, None)
        await panel_handlers.pick_station(cog, SimpleNamespace(), tr, radio, ids[1])

        applied.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_random_never_picks_the_current_or_an_unavailable_station(self, monkeypatch):
        from empiremuse.utils.radio import actions, panel_handlers

        ids = [uuid_of(n) for n in range(1, 4)]  # ids[2] indisponível, ids[0] é a atual
        cog, radio = self.cog_with(ids)
        applied = AsyncMock()
        monkeypatch.setattr(actions, "change_station", applied)

        for _ in range(20):
            await panel_handlers.random_station(cog, SimpleNamespace(), lambda k, **kw: k, radio)

        assert {call.kwargs["station"].uuid for call in applied.await_args_list} == {ids[1]}

    @pytest.mark.asyncio
    async def test_random_with_nothing_else_says_so(self):
        from empiremuse.utils.radio import panel_handlers

        ids = [uuid_of(1), uuid_of(2), uuid_of(3)]
        cog, radio = self.cog_with(ids)
        radio.source["list"] = [ids[0], ids[2]]  # a outra está indisponível

        assert (
            await panel_handlers.random_station(cog, SimpleNamespace(), lambda k, **kw: k, radio)
            == "radio.panel.no_other_station"
        )

    @pytest.mark.asyncio
    async def test_the_filter_select_saves_list_and_filter_without_changing_the_station(self):
        from empiremuse.utils.music.errors import GenericError
        from empiremuse.utils.radio import panel_handlers

        w = build()
        await premium(w)
        ids = [uuid_of(n) for n in range(1, 6)]
        radio = await configure(
            w.hub,
            guild_id=GUILD,
            channel_id=CHANNEL,
            bot_id=2,
            kind="live",
            source={"station": ids[0], "reserve": None},
        )
        hits = tuple(SimpleNamespace(station=SimpleNamespace(uuid=u), clickcount=n) for n, u in enumerate(ids))
        w.hub.directory.region = AsyncMock(return_value=SimpleNamespace(hits=hits))
        w.hub.locations = SimpleNamespace(node=lambda n: SimpleNamespace(id=n) if n == "br" else None)
        interaction = SimpleNamespace(author=SimpleNamespace(id=7))
        tr = lambda key, **kw: key  # noqa: E731
        cog = SimpleNamespace(hub=w.hub)

        text = await panel_handlers.pick_filter(cog, interaction, tr, radio, "br")
        saved = await w.hub.store.get(radio.id)

        assert text == "radio.panel.filter_applied"
        assert saved.filter == "br" and saved.source["station"] == ids[0]  # a estação no ar não mudou
        assert saved.source["list"] == list(reversed(ids))  # as mais ouvidas primeiro
        with pytest.raises(GenericError):
            await panel_handlers.pick_filter(cog, interaction, tr, saved, "inexistente")
