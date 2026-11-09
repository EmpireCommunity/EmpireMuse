"""Painel da rádio personalizada (item 4): lista das estações do servidor, filtro por TIPO lembrado por rádio e andar/sortear dentro do filtro."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.rules.radio_station import KIND_ARTISTS, KIND_GENRE, KIND_PLAYLISTS, CustomStation
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.radio import custom_panel
from empiremuse.utils.radio.stations import StationStore

pytestmark = pytest.mark.critical


def tr(key, **kw):
    return key + ("" if not kw else str(sorted(kw.items())))


def st(n, kind):
    return CustomStation(id=f"{n:024x}", guild_id=1, name=f"E{n}", kind=kind, source={})


STATIONS = [st(1, KIND_ARTISTS), st(2, KIND_PLAYLISTS), st(3, KIND_ARTISTS), st(4, KIND_PLAYLISTS)]


def world(filter_id=None, active=1):
    radio = SimpleNamespace(
        id="r", guild_id=1, filter=filter_id, source={"station": STATIONS[active - 1].id}, skin="black", rev=3
    )
    calls = []

    async def change(hub, *, radio_id, station_id, actor):
        calls.append(station_id)

    cog = SimpleNamespace(
        hub=SimpleNamespace(
            stations=SimpleNamespace(
                list_for_guild=AsyncMock(return_value=STATIONS),
                list_builtin=lambda gid: StationStore.list_builtin(gid),
                get=AsyncMock(
                    side_effect=lambda gid, sid: next((s for s in STATIONS if s.id == sid and s.guild_id == gid), None)
                ),
            ),
            store=SimpleNamespace(get=AsyncMock(return_value=radio), set_filter=AsyncMock()),
        ),
        _too_soon=lambda *a: False,
        _authorized=lambda i: "actor",
    )
    return radio, cog, calls, change


def inter():
    return SimpleNamespace(author=SimpleNamespace(id=9))


def test_filtered_keeps_only_the_type_and_an_unknown_filter_means_all():
    assert [s.name for s in custom_panel.filtered(STATIONS, "f:artists")] == ["E1", "E3"]
    assert len(custom_panel.filtered(STATIONS, None)) == 4 and len(custom_panel.filtered(STATIONS, "x")) == 4


@pytest.mark.asyncio
async def test_walking_stays_inside_the_filter_and_wraps(monkeypatch):
    radio, cog, calls, change = world("f:artists", active=1)
    monkeypatch.setattr(custom_panel.actions, "change_custom_station", change)
    await custom_panel.walk(cog, inter(), tr, radio, 1)
    await custom_panel.walk(cog, inter(), tr, radio, -1)
    assert calls == [
        STATIONS[2].id,
        STATIONS[2].id,
    ]  # E1 -> E3 (pula E2, de outro tipo) e, voltando, dá a volta para E3


@pytest.mark.asyncio
async def test_a_station_outside_the_filter_or_forged_is_refused(monkeypatch):
    radio, cog, calls, change = world("f:artists")
    monkeypatch.setattr(custom_panel.actions, "change_custom_station", change)
    for bad in (STATIONS[1].id, "forjado", None):
        with pytest.raises(GenericError):
            await custom_panel.pick_station(cog, inter(), tr, radio, bad)
    await custom_panel.pick_station(cog, inter(), tr, radio, STATIONS[2].id)
    assert calls == [STATIONS[2].id]


@pytest.mark.asyncio
async def test_random_never_repeats_the_current_and_a_single_station_has_nowhere_to_go(monkeypatch):
    radio, cog, calls, change = world("f:playlists", active=4)
    cog.hub.stations.list_for_guild.return_value = [STATIONS[3]]
    monkeypatch.setattr(custom_panel.actions, "change_custom_station", change)
    assert await custom_panel.random_station(cog, inter(), tr, radio) == "radio.panel.no_other_station"
    radio, cog, calls, change = world("f:artists", active=1)
    monkeypatch.setattr(custom_panel.actions, "change_custom_station", change)
    await custom_panel.random_station(cog, inter(), tr, radio)
    assert calls == [STATIONS[2].id]


@pytest.mark.asyncio
async def test_the_type_filter_is_saved_without_changing_the_station_and_a_forged_one_is_refused():
    radio, cog, *_ = world()
    await custom_panel.pick_filter(cog, inter(), tr, radio, "f:artists")
    cog.hub.store.set_filter.assert_awaited_once_with("r", "f:artists", expected_rev=3, updated_by="9")
    await custom_panel.pick_filter(cog, inter(), tr, radio, "f:all")  # volta a 'todas'
    cog.hub.store.set_filter.assert_awaited_with("r", None, expected_rev=3, updated_by="9")
    for bad in ("f:fav:9", "c:BR", None):
        with pytest.raises(GenericError):
            await custom_panel.pick_filter(cog, inter(), tr, radio, bad)


@pytest.mark.asyncio
async def test_the_snapshot_lists_the_filtered_stations_with_the_station_name_and_track():
    radio, cog, *_ = world("f:artists", active=3)
    ports = SimpleNamespace(
        station=STATIONS[2],
        current=SimpleNamespace(title="Faixa", artist="Cantor"),
        no_tracks=False,
        wall=lambda: 1000.0,
    )
    snap = await custom_panel.custom_snapshot(cog.hub, radio, ports, None, locale="pt-BR", tr=tr)
    assert (snap.station_name, snap.now_title, snap.now_artist, snap.total, snap.position) == (
        "E3",
        "Faixa",
        "Cantor",
        2,
        2,
    )
    assert [o.name for o in snap.stations] == ["E1", "E3"] and snap.current_index == 1
    assert snap.filter_path == ("radio.station.kind.artists",) and snap.state == "live"


class FakeFavs:
    """``hub.custom_favorites`` em memória: o mesmo contrato do CustomFavorites, só para provar o uso por membro."""

    def __init__(self):
        self.data = {}

    async def list(self, guild_id, user_id):
        return list(self.data.get((guild_id, user_id), []))

    async def toggle(self, guild_id, user_id, station_id):
        from empiremuse.utils.radio.custom_favorites import Toggled

        current = self.data.setdefault((guild_id, user_id), [])
        if station_id in current:
            current.remove(station_id)
            return Toggled.REMOVED
        current.append(station_id)
        return Toggled.ADDED


@pytest.mark.asyncio
async def test_the_star_toggles_for_the_member_who_clicked_and_not_for_the_others():
    radio, cog, *_ = world(active=2)
    cog.hub.custom_favorites = FakeFavs()
    ana, bia = SimpleNamespace(author=SimpleNamespace(id=1)), SimpleNamespace(author=SimpleNamespace(id=2))
    await custom_panel.favorite(cog, ana, lambda k, **kw: k, radio)
    assert cog.hub.custom_favorites.data == {(1, 1): [STATIONS[1].id]}
    await custom_panel.favorite(cog, bia, lambda k, **kw: k, radio)
    await custom_panel.favorite(cog, ana, lambda k, **kw: k, radio)  # desliga só a dela
    assert cog.hub.custom_favorites.data == {(1, 1): [], (1, 2): [STATIONS[1].id]}


@pytest.mark.asyncio
async def test_the_favorites_filter_uses_the_list_of_who_picked_it_and_walking_stays_in_it(monkeypatch):
    radio, cog, calls, change = world(active=1)
    monkeypatch.setattr(custom_panel.actions, "change_custom_station", change)
    cog.hub.custom_favorites = FakeFavs()
    cog.hub.custom_favorites.data = {(1, 9): [STATIONS[0].id, STATIONS[3].id]}
    assert (
        await custom_panel.pick_filter(cog, inter(), tr, radio, "f:fav") == "radio.panel.filter_applied[('total', 2)]"
    )
    cog.hub.store.set_filter.assert_awaited_once_with("r", "f:fav:9", expected_rev=3, updated_by="9")
    radio.filter = "f:fav:9"
    await custom_panel.walk(cog, inter(), tr, radio, 1)
    assert calls == [STATIONS[3].id]  # E1 -> E4 (as únicas favoritas de 9)
    other = SimpleNamespace(author=SimpleNamespace(id=5))  # quem não tem favoritas recebe o aviso e o filtro não muda
    assert await custom_panel.pick_filter(cog, other, tr, radio, "f:fav") == "radio.panel.favorites_empty"


@pytest.mark.asyncio
async def test_the_custom_panel_uses_the_visor_with_types_and_the_favorites_entry(monkeypatch):
    from empiremuse.i18n import get_i18n
    from empiremuse.utils.radio.discord_ports import DiscordRadioPorts

    radio, cog, *_ = world("f:fav:9", active=1)
    cog.hub.custom_favorites = FakeFavs()
    cog.hub.custom_favorites.data = {(1, 9): [STATIONS[0].id]}
    cog.hub.manager = SimpleNamespace(runner=lambda _id: None)
    ports = SimpleNamespace(
        pool=SimpleNamespace(radio=cog.hub),
        station=STATIONS[0],
        current=None,
        no_tracks=False,
        wall=lambda: 1.0,
    )
    tr_real = await get_i18n().for_guild(None)
    bot = SimpleNamespace(get_color=lambda me: 0)
    payload = await DiscordRadioPorts._custom_visor_payload(ports, bot, SimpleNamespace(me=None), radio, tr_real)
    select = payload["components"][-1].children[0]
    assert [o.value for o in select.options][-1] == "f:fav" and select.options[-1].default
    assert not any(o.default for o in select.options[:-1]) and payload["file"].filename.endswith(".png")


@pytest.mark.asyncio
async def test_the_reconnect_button_wakes_the_custom_radio_wait():
    """CodeRabbit #199: ``wake_retry`` herdado sinalizava ``_retry_wake`` e o ``sleep`` da personalizada esperava ``_wake`` (recuo de até 10 min)."""
    import asyncio

    from empiremuse.utils.radio.custom_ports import CustomRadioPorts

    ports = SimpleNamespace(_wake=asyncio.Event())
    sleeper = asyncio.ensure_future(CustomRadioPorts.sleep(ports, 30))
    await asyncio.sleep(0)

    CustomRadioPorts.wake_retry(ports)

    await asyncio.wait_for(sleeper, 1)


@pytest.mark.asyncio
async def test_genre_groups_are_selectable_favoritable_and_excluded_from_walk(monkeypatch):
    from empiremuse.utils.radio.stations import StationStore

    radio, cog, calls, change = world("f:international")
    monkeypatch.setattr(custom_panel.actions, "change_custom_station", change)
    assert len(await custom_panel.listed(cog.hub, radio)) == 21
    await custom_panel.pick_station(cog, inter(), tr, radio, "g:rock")
    assert calls == ["g:rock"]
    calls.clear()
    assert await custom_panel.walk(cog, inter(), tr, radio, 1) == "radio.panel.no_other_station"
    assert await custom_panel.random_station(cog, inter(), tr, radio) == "radio.panel.no_other_station"
    await custom_panel.pick_filter(cog, inter(), tr, radio, "f:brazilian")
    cog.hub.store.set_filter.assert_awaited_with("r", "f:brazilian", expected_rev=3, updated_by="9")
    radio.filter, radio.source = "f:fav:9", {"station": "g:rock"}
    cog.hub.custom_favorites = FakeFavs()
    cog.hub.stations.get = AsyncMock(side_effect=StationStore.builtin)
    await custom_panel.favorite(cog, inter(), tr, radio)
    assert cog.hub.custom_favorites.data[(1, 9)] == ["g:rock"]
    assert [s.id for s in await custom_panel.listed(cog.hub, radio)] == ["g:rock"]
    assert await custom_panel.walk(cog, inter(), tr, radio, 1) == "radio.panel.no_other_station"


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["pt-BR", "en-US"])
@pytest.mark.parametrize("action", ["choose", "favorite"])
async def test_builtin_messages_use_the_translated_genre_name(monkeypatch, locale, action):
    from empiremuse.i18n import get_i18n

    radio, cog, calls, change = world("f:brazilian")
    monkeypatch.setattr(custom_panel.actions, "change_custom_station", change)
    tr_real = get_i18n().for_locale(locale)
    if action == "choose":
        message = await custom_panel.pick_station(cog, inter(), tr_real, radio, "g:sertanejo_uni")
    else:
        radio.source = {"station": "g:sertanejo_uni"}
        cog.hub.custom_favorites = FakeFavs()
        cog.hub.stations.get = AsyncMock(side_effect=StationStore.builtin)
        message = await custom_panel.favorite(cog, inter(), tr_real, radio)
    assert tr_real("radio.genre.sertanejo_uni") in message
    assert "sertanejo_uni" not in message


@pytest.mark.asyncio
async def test_walking_mixed_favorites_never_selects_a_builtin(monkeypatch):
    radio, cog, calls, change = world("f:fav:9", active=1)
    monkeypatch.setattr(custom_panel.actions, "change_custom_station", change)
    cog.hub.custom_favorites = FakeFavs()
    cog.hub.custom_favorites.data[(1, 9)] = [STATIONS[0].id, "g:rock", STATIONS[3].id]
    await custom_panel.walk(cog, inter(), tr, radio, 1)
    await custom_panel.walk(cog, inter(), tr, radio, -1)
    assert calls == [STATIONS[3].id, STATIONS[3].id]


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["pt-BR", "en-US"])
async def test_custom_ports_translate_builtin_labels(locale):
    from empiremuse.i18n import get_i18n
    from empiremuse.utils.radio.custom_ports import CustomRadioPorts

    ports = object.__new__(CustomRadioPorts)
    ports.station = StationStore.builtin(1, "g:sertanejo_uni")
    tr_real = get_i18n().for_locale(locale)
    ports.pool = SimpleNamespace(
        i18n=SimpleNamespace(for_guild=AsyncMock(return_value=tr_real)),
        radio=SimpleNamespace(stations=SimpleNamespace(list_for_guild=AsyncMock(return_value=[]))),
    )
    ports._guild = lambda: None
    ports.radio = SimpleNamespace(guild_id=1)
    ports.current, ports.no_tracks, ports.searching, ports.unknown = None, False, False, ()
    assert await ports._panel_name(ports.radio) == tr_real("radio.genre.sertanejo_uni")
    assert (await ports._custom_view()).station == tr_real("radio.genre.sertanejo_uni")
