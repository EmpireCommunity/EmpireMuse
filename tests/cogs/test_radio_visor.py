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
