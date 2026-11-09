"""O visor do painel de rádio (R7): PNG gerado nas duas skins, em todos os estados, com texto de fora limpo e cortado, nunca quebrando."""

import io

import pytest
from PIL import Image

from empiremuse.i18n import get_i18n
from empiremuse.utils.music.skins.radio_player import SKIN_NAMES, TOKEN_NAMES, load_radio_skin, load_radio_skins
from empiremuse.utils.radio.visor import HEIGHT, SCALE, WIDTH, NextStation, VisorData, clean_text, render_visor

pytestmark = pytest.mark.critical

PT = get_i18n().for_locale("pt-BR")
EN = get_i18n().for_locale("en-US")

NEXT = (NextStation(4, "Kiss FM", 192, "good"), NextStation(5, "Nova Brasil", 64, "weak"))
LIVE = VisorData(
    station_name="Jovem Pan",
    codec="mp3",
    bitrate_kbps=128,
    position=3,
    total=12,
    filter_path=("Brasil", "São Paulo"),
    reserve_number=2,
    now_title="Evidências",
    now_artist="Chitãozinho & Xororó",
    on_air_minutes=45,
    volume=80,
    next_stations=NEXT,
)


def png(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


@pytest.mark.parametrize("skin", SKIN_NAMES)
@pytest.mark.parametrize("state", ["live", "muted", "offline"])
def test_every_skin_renders_every_state_as_a_png_of_the_fixed_size(skin, state):
    image = png(render_visor(LIVE._replace(state=state, retry_seconds=20), PT, skin))

    assert image.format == "PNG" and image.size == (WIDTH * SCALE, HEIGHT * SCALE)


def test_the_two_skins_define_every_token_and_differ():
    skins = load_radio_skins()

    assert set(skins) == set(SKIN_NAMES)
    for skin in skins.values():
        assert set(skin.tokens) == set(TOKEN_NAMES), skin.name
    assert skins["black"].tokens["card"] != skins["white"].tokens["card"]
    assert load_radio_skin("nao-existe").name == "black"  # valor estranho do documento cai na padrão


def test_outside_text_is_cleaned_and_long_text_is_cut_without_breaking_the_drawing():
    hostile = VisorData(
        station_name="Rádio ‮**muito** `longa` " * 6,
        now_title="\x00Título​ com controle\n e quebra " * 4,
        now_artist="Artista " * 30,
        filter_path=("País " * 12, "Estado"),
        next_stations=(NextStation(99, "Nome enorme de estação " * 5, 999999, "good"),),
        total=400,
        position=250,
        volume=999,
    )

    assert clean_text("a\x00b‮c\nd") == "a b c d"
    assert png(render_visor(hostile, EN, "white")).size == (WIDTH * SCALE, HEIGHT * SCALE)


def test_favorites_mode_and_the_locale_change_the_drawing():
    favorites = LIVE._replace(list_kind="favorites", favorite=True, total=5, position=2, filter_path=())

    assert render_visor(favorites, PT, "black") != render_visor(LIVE, PT, "black")
    assert render_visor(LIVE, PT, "black") != render_visor(LIVE, EN, "black")
    assert render_visor(LIVE, PT, "black") == render_visor(LIVE, PT, "black")  # determinístico


def test_a_logo_that_is_not_an_image_does_not_break_the_visor():
    assert png(render_visor(LIVE._replace(logo=b"not a png"), PT, "white")).size[0] == WIDTH * SCALE


# ============================================================================ adaptador do snapshot do Turbine


def snapshot(**overrides):
    from empiremuse.utils.radio.snapshot import RadioPanelSnapshot, StationOption

    window = tuple(StationOption(i, f"id{i}", f"Estação {i}", 64 * i, "mp3", "good") for i in range(1, 6))
    base = dict(
        radio_id="1:100",
        skin="white",
        station_name="Estação 3",
        country="Brasil",
        codec="mp3",
        bitrate_kbps=192,
        logo=None,
        quality="ok",
        now_title="Música",
        now_artist=None,
        state="live",
        reserve_number=None,
        volume=150,
        started_at=None,
        on_air_minutes=7,
        filter_path=("América do Sul", "Brasil", "SP", "Campinas"),
        list_kind="stations",
        position=3,
        total=40,
        stations=window,
        current_index=2,
        favorite=True,
        favorites_full=False,
        retry_seconds=None,
    )
    base.update(overrides)
    return RadioPanelSnapshot(**base)


def test_the_snapshot_adapter_maps_every_field_and_takes_the_two_stations_after_the_current_one():
    from empiremuse.utils.radio.visor import from_snapshot, render_snapshot, volume_level

    data = from_snapshot(snapshot())

    assert (data.station_name, data.codec, data.bitrate_kbps, data.quality) == ("Estação 3", "mp3", 192, "ok")
    assert (data.position, data.total, data.list_kind, data.favorite) == (3, 40, "stations", True)
    assert data.filter_path == ("América do Sul", "Brasil", "SP", "Campinas") and data.reserve_number is None
    assert (data.now_title, data.now_artist, data.retry_seconds, data.on_air_minutes) == ("Música", "", 0, 7)
    assert [(s.position, s.name, s.bitrate_kbps) for s in data.next_stations] == [
        (4, "Estação 4", 256),
        (5, "Estação 5", 320),
    ]
    assert volume_level(150) == 1.0 and volume_level(100) == pytest.approx(100 / 150) and volume_level(-5) == 0.0
    assert png(render_snapshot(snapshot(), PT)).size == (WIDTH * SCALE, HEIGHT * SCALE)


def test_a_station_outside_the_list_has_no_next_stations_and_still_renders():
    from empiremuse.utils.radio.visor import from_snapshot, render_snapshot

    outside = snapshot(position=0, current_index=None, stations=(), total=0, state="offline", retry_seconds=15)
    data = from_snapshot(outside)

    assert data.next_stations == () and data.position == 0 and data.retry_seconds == 15
    assert render_snapshot(outside, EN) != render_snapshot(snapshot(), EN)


# ============================================================================ o payload do painel novo (embed só com o visor + 2x5 + 2 selects)


class FakeTree:
    """Árvore mínima: topo = regiões; 'r:sa' → países; 'c:BR' → estados; 's:SP' é final."""

    def __init__(self):
        from empiremuse.utils.radio.locations import LocNode, LocOption

        self._nodes = {
            "r:sa": LocNode("r:sa", "region", "south-america", None, False),
            "c:BR": LocNode("c:BR", "country", "BR", "r:sa", False),
            "s:SP": LocNode("s:SP", "state", "São Paulo", "c:BR", True),
        }
        self._children = {
            None: [LocOption("r:sa", "América do Sul", "12 países", 900)],
            "r:sa": [LocOption("c:BR", "Brasil", "", 500), LocOption("c:AR", "Argentina", "", 100)],
            "c:BR": [LocOption("s:SP", "São Paulo", "", 80)],
            "s:SP": [],
        }

    def node(self, node_id):
        return self._nodes.get(node_id)

    def children(self, node_id, locale, *, guild_locale=None):
        return list(self._children.get(node_id, []))

    def label(self, node, locale):
        return {"r:sa": "América do Sul", "c:BR": "Brasil", "s:SP": "São Paulo"}[node.id]


def ids_of(row):
    return [child.custom_id for child in row.children]


def test_the_visor_payload_has_only_the_image_embed_two_button_rows_and_the_two_selects():
    from empiremuse.rules.radio import PANEL_FILTER_SELECT_ID, PANEL_MUTE_ID, PANEL_STATION_SELECT_ID, PANEL_STOP_ID
    from empiremuse.utils.radio.panel import filter_select_options, visor_payload

    snap = snapshot(skin="black")
    options = filter_select_options(PT, FakeTree(), "c:BR", "pt-BR", favorites_total=5)
    payload = visor_payload(PT, snap, filter_options=options, color=0x8B5CF6)

    embed = payload["embeds"][0]
    assert embed.description is None and embed.title is None and not embed.fields, "nada repetido em texto (P1)"
    assert embed.image.url == "attachment://visor.png" and payload["file"].filename == "visor.png"
    rows = payload["components"]
    assert len(rows) == 4 and len(rows[0].children) == 5 and len(rows[1].children) == 5
    assert all(child.label is None for row in rows[:2] for child in row.children), "só emoji (R6)"
    assert PANEL_MUTE_ID in ids_of(rows[0]) and PANEL_STOP_ID in ids_of(rows[1])
    station_select = rows[2].children[0]
    assert station_select.custom_id == PANEL_STATION_SELECT_ID
    assert [o.value for o in station_select.options] == [f"id{i}" for i in range(1, 6)]
    assert [o.default for o in station_select.options] == [False, False, True, False, False]
    assert station_select.options[2].label.startswith("3. Estação 3")
    filter_select = rows[3].children[0]
    assert filter_select.custom_id == PANEL_FILTER_SELECT_ID
    assert [o.value for o in filter_select.options] == ["f:fav", "r:sa", "s:SP"]
    assert "5" in filter_select.options[0].label and "Voltar" in filter_select.options[1].label


def test_a_final_node_lists_its_siblings_and_an_empty_station_list_still_builds():
    from empiremuse.utils.radio.panel import filter_select_options, visor_payload

    options = filter_select_options(PT, FakeTree(), "s:SP", "pt-BR", favorites_total=0)
    assert [o.value for o in options] == ["f:fav", "c:BR", "s:SP"]  # favoritas, voltar ao país, os irmãos do estado
    assert options[2].default is True  # o filtro atual marcado

    top = filter_select_options(PT, FakeTree(), None, "pt-BR", favorites_total=0)
    assert [o.value for o in top] == ["f:fav", "r:sa"]

    empty = snapshot(stations=(), position=0, current_index=None, total=0)
    payload = visor_payload(PT, empty, filter_options=top, color=0)
    station_select = payload["components"][2].children[0]
    assert station_select.disabled and len(station_select.options) == 1


def test_muted_shows_the_unmute_emoji_in_danger_style_and_the_mapping_of_the_buttons_is_symmetric():
    import disnake

    from empiremuse.rules.radio import PANEL_MUTE_ID
    from empiremuse.utils.radio.panel import visor_payload

    live = visor_payload(PT, snapshot(), filter_options=[], color=0)
    muted = visor_payload(PT, snapshot(state="muted"), filter_options=[], color=0)
    mute_live = next(c for c in live["components"][0].children if c.custom_id == PANEL_MUTE_ID)
    mute_muted = next(c for c in muted["components"][0].children if c.custom_id == PANEL_MUTE_ID)
    assert mute_live.style != disnake.ButtonStyle.danger and mute_muted.style == disnake.ButtonStyle.danger
    assert str(mute_live.emoji) != str(mute_muted.emoji)
    assert live["components"][0].children[2].custom_id == PANEL_MUTE_ID, "mudo no centro da linha 1"
    assert live["components"][1].children[2].custom_id == "radio_panel_reconnect", "reconectar no centro da linha 2"


@pytest.mark.asyncio
async def test_the_async_payload_renders_in_a_thread_and_equals_the_sync_one():
    from empiremuse.utils.radio.panel import visor_payload, visor_payload_async
    from empiremuse.utils.radio.visor import render_snapshot

    snap = snapshot()
    payload = await visor_payload_async(PT, snap, filter_options=[], color=1)

    assert payload["file"].fp.read() == render_snapshot(snap, PT)
    sync = visor_payload(PT, snap, filter_options=[], color=1, picture=b"x")
    assert sync["file"].fp.read() == b"x" and len(sync["components"]) == len(payload["components"]) == 4


# ============================================================================ a ligação: as portas da rádio ao vivo mandam o visor; a personalizada segue no painel antigo


def live_world():
    from unittest.mock import AsyncMock

    from empiremuse.rules.radio import check_radio, radio_id
    from empiremuse.utils.radio.live_ports import LiveRadioPorts
    from tests.cogs.test_radio_portas import BOT_ID, CHANNEL, GUILD, build

    w = build()
    w.bot.get_color = lambda member: 0x8B5CF6
    w.bot.pool.i18n.for_guild = AsyncMock(return_value=PT)
    w.hub.directory.by_uuids = AsyncMock(return_value={})
    w.hub.directory.logo_png = AsyncMock(return_value=type("Logo", (), {"png": None})())
    ids = [f"{n:08x}-0000-4000-8000-000000000000" for n in range(1, 4)]
    radio = check_radio(
        {
            "_id": radio_id(BOT_ID, GUILD),
            "channel_id": str(CHANNEL),
            "kind": "live",
            "source": {"station": ids[0], "reserve": None, "list": ids},
            "skin": "white",
        }
    )
    return w, LiveRadioPorts(w.ports.pool, radio), radio


@pytest.mark.asyncio
async def test_the_live_ports_send_the_visor_panel_and_the_custom_ports_keep_the_old_one():
    from empiremuse.rules.radio import PANEL_FILTER_SELECT_ID, PANEL_STATION_SELECT_ID

    w, ports, radio = live_world()

    payload = await ports._panel_payload(w.bot, w.guild, radio)

    assert payload["file"].filename == "visor.png" and payload["embeds"][0].image.url == "attachment://visor.png"
    assert [row.children[0].custom_id for row in payload["components"][2:]] == [
        PANEL_STATION_SELECT_ID,
        PANEL_FILTER_SELECT_ID,
    ]
    assert png(payload["file"].fp.read()).size == (WIDTH * SCALE, HEIGHT * SCALE)
    edit = ports.edit_kwargs(payload)
    assert edit["attachments"] == [] and edit["file"] is payload["file"]

    custom = await w.ports._panel_payload(w.bot, w.guild, w.ports.radio)
    assert custom["embeds"][0].image.url != "attachment://visor.png"


# ============================================================================ a rádio PERSONALIZADA no mesmo visor (tipo no lugar da reserva, select 2 por tipo)


def custom_snapshot(**overrides):
    base = dict(
        skin="black",
        station_name="Rock Clássico",
        codec="",
        bitrate_kbps=0,
        quality="good",
        now_title="Bohemian Rhapsody",
        now_artist="Queen",
        reserve_number=None,
        filter_path=("Gênero",),
        stations=tuple(
            __import__("empiremuse.utils.radio.snapshot", fromlist=["StationOption"]).StationOption(
                i, f"{i:024x}", f"Estação {i}", 0, "", "good"
            )
            for i in range(1, 4)
        ),
        current_index=0,
        position=1,
        total=3,
        favorite=False,
    )
    base.update(overrides)
    return snapshot(**base)


@pytest.mark.parametrize("skin", SKIN_NAMES)
def test_the_custom_visor_renders_in_both_skins_and_differs_from_the_live_drawing_of_the_same_data(skin):
    from empiremuse.utils.radio.visor import render_snapshot

    snap = custom_snapshot(skin=skin)

    custom = render_snapshot(snap, PT, custom=True)
    assert png(custom).size == (WIDTH * SCALE, HEIGHT * SCALE)
    assert custom != render_snapshot(snap, PT)  # badge WEB, "Estação do servidor" e o tipo no lugar da reserva
    assert render_snapshot(custom_snapshot(skin=skin, filter_path=()), EN, custom=True) != custom  # "ALL TYPES"


def test_the_type_select_marks_the_current_filter_and_all_stations_when_there_is_none():
    from empiremuse.utils.radio.panel import ALL_TYPES_FILTER, type_select_options

    none = type_select_options(PT, None)
    assert [o.value for o in none] == [ALL_TYPES_FILTER, "f:international", "f:brazilian", "f:artists", "f:playlists"]
    assert [o.default for o in none] == [True, False, False, False, False]
    assert (
        none[1].label == "Gêneros internacionais" and type_select_options(EN, None)[1].label == "International genres"
    )

    genre = type_select_options(PT, "f:international")
    assert [o.default for o in genre] == [False, True, False, False, False]


@pytest.mark.asyncio
async def test_the_custom_payload_has_two_short_button_rows_and_uses_the_type_select():
    from empiremuse.rules.radio import PANEL_FILTER_SELECT_ID, PANEL_STATION_SELECT_ID
    from empiremuse.utils.radio.panel import type_select_options, visor_payload_async
    from empiremuse.utils.radio.visor import render_snapshot

    snap = custom_snapshot()
    payload = await visor_payload_async(
        PT, snap, filter_options=type_select_options(PT, "f:international"), custom=True
    )

    rows = payload["components"]
    assert (
        len(rows) == 4 and len(rows[0].children) == 2 and len(rows[1].children) == 3
    )  # só o que o cog atende na personalizada
    assert rows[2].children[0].custom_id == PANEL_STATION_SELECT_ID
    assert [o.value for o in rows[2].children[0].options] == [f"{i:024x}" for i in range(1, 4)]
    filter_select = rows[3].children[0]
    assert filter_select.custom_id == PANEL_FILTER_SELECT_ID and filter_select.placeholder.startswith("Tipo")
    assert payload["file"].fp.read() == render_snapshot(snap, PT, custom=True)


def test_the_custom_visor_only_has_buttons_the_cog_answers_and_walks_the_stations():
    """CodeRabbit #199: 6 dos 10 botões do visor da personalizada só devolviam erro, e anterior/próxima ESTAÇÃO nem apareciam."""
    from empiremuse.cogs.radio import CUSTOM_ACTIONS, LIVE_ACTIONS
    from empiremuse.rules.radio import panel_action
    from empiremuse.utils.radio.panel import visor_payload

    payload = visor_payload(PT, custom_snapshot(), filter_options=[], color=0, custom=True)

    ids = [c.custom_id for row in payload["components"][:2] for c in row.children]
    actions = {panel_action(i) for i in ids}
    assert not (actions & LIVE_ACTIONS) and "next" not in actions
    assert CUSTOM_ACTIONS <= actions and {"favorite", "random", "stop"} <= actions
