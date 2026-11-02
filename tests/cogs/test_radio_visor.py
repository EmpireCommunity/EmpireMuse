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
