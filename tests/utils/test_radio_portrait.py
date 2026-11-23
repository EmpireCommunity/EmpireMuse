"""Retrato da rádio 24 h, v2 (``utils/render/cards/radio_portrait.py``): estados, call privada, texto hostil e o teto do arquivo.

Sem snapshot de pixels (regra de testes): confere o arquivo (PNG, tamanho, teto) e QUAIS textos o cartão desenha, espiando o
``_Canvas.text``. ``tr`` devolve a chave.
"""

from __future__ import annotations

import io
import unicodedata
from types import SimpleNamespace as NS

import pytest
from PIL import Image

from empiremuse.utils.radio.visor import NextStation, VisorData
from empiremuse.utils.render.cards import radio_portrait
from empiremuse.utils.render.cards.radio_portrait import (
    NEXT_MAX,
    PNG_MAX_BYTES,
    PortraitCard,
    from_radio_snapshot,
    icy_text,
    render_portrait,
    seal,
)


def tr(key, **fields):
    return key


def next_stations(count=NEXT_MAX):
    names = ("Onda Sul FM", "Litoral Hits", "Serra Azul FM", "Vale Verde FM", "Quinta FM")
    return tuple(NextStation(4 + i, names[i], 128, "good") for i in range(count))


def data(**overrides):
    base = dict(
        station_name="Rádio Horizonte FM",
        codec="MP3",
        bitrate_kbps=128,
        state="live",
        position=3,
        total=12,
        filter_path=("Brasil", "São Paulo"),
        reserve_number=2,
        now_title="Garota de Ipanema",
        now_artist="Tom Jobim",
        on_air_minutes=45,
        volume=80,
        next_stations=next_stations(),
    )
    return VisorData(**{**base, **overrides})


@pytest.fixture
def drawn(monkeypatch):
    """Os textos que o cartão desenha, na ordem."""
    texts: list[str] = []
    original = radio_portrait._Canvas.text

    def spy(self, xy, text, *args, **kwargs):
        texts.append(text)
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(radio_portrait._Canvas, "text", spy)
    return texts


def decoded(png: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(png), formats=["PNG"])
    image.load()
    return image


def snapshot(index, position):
    stations = tuple(NS(position=i, name=f"E{i}", bitrate_kbps=64, quality="ok") for i in range(1, 10))
    return NS(
        stations=stations,
        current_index=index,
        position=position,
        station_name=f"E{position}",
        codec="AAC",
        bitrate_kbps=64,
        logo=None,
        state="live",
        total=9,
        list_kind="stations",
        favorite=False,
        filter_path=(),
        reserve_number=None,
        now_title="",
        now_artist="",
        on_air_minutes=0,
        volume=100,
        quality="ok",
        retry_seconds=0,
    )


@pytest.mark.critical
@pytest.mark.parametrize("skin", ["portrait_black", "portrait_white", "desconhecida"])
@pytest.mark.parametrize(
    ("state", "keys"),
    [
        ("live", {"visor.live", "visor.now_playing"}),
        ("muted", {"visor.muted", "visor.silenced", "visor.silenced_title"}),
        ("offline", {"visor.offline", "visor.no_signal", "visor.reconnecting", "visor.retry"}),
    ],
)
def test_each_state_renders_a_png_within_the_budget(drawn, skin, state, keys):
    png = render_portrait(PortraitCard(data(state=state, retry_seconds=8), skin), tr)
    image = decoded(png)
    assert image.format == "PNG" and image.size == (960, 1680) and len(png) <= PNG_MAX_BYTES
    assert keys <= set(drawn)


@pytest.mark.critical
def test_the_heaviest_case_stays_within_the_budget():
    """Nomes longos em todo lugar, escala cheia, 4 seguintes e um logo: o teto do arquivo em 2× vale também aqui."""
    logo = io.BytesIO()
    Image.effect_noise((64, 64), 90).convert("RGB").save(logo, format="PNG")
    heavy = data(
        station_name="Ç" * 80,
        now_title="Ж" * 120,
        now_artist="ア" * 120,
        filter_path=("Brasil", "São Paulo", "Região Metropolitana"),
        total=25,
        position=25,
        logo=logo.getvalue(),
        next_stations=tuple(NextStation(i, "Ø" * 60, 320, "weak") for i in range(1, 5)),
    )
    assert len(render_portrait(PortraitCard(heavy, "portrait_white"), tr)) <= PNG_MAX_BYTES


@pytest.mark.critical
@pytest.mark.regression
def test_a_private_call_draws_nothing_about_what_or_where(drawn):
    secret = data(
        station_name="Rádio Secreta",
        now_title="Faixa Secreta",
        now_artist="Artista Secreto",
        filter_path=("País Secreto",),
        next_stations=(NextStation(4, "Próxima Secreta", 128, "good"),),
    )
    render_portrait(PortraitCard(secret, hidden=True), tr)
    assert not [text for text in drawn if "Secret" in text]
    assert "4." in drawn  # a posição fica (contagem), o nome não


@pytest.mark.critical
def test_seal_keeps_counts_and_controls_but_no_names():
    sealed = seal(PortraitCard(data(logo=b"\x89PNG"), hidden=True)).data
    names = (sealed.station_name, sealed.now_title, sealed.now_artist, sealed.logo, sealed.filter_path)
    assert names == ("", "", "", None, ())
    assert [s.name for s in sealed.next_stations] == [""] * NEXT_MAX
    assert (sealed.total, sealed.position, sealed.volume, sealed.state) == (12, 3, 80, "live")
    assert seal(PortraitCard(data())).data == data()  # fora da call privada nada muda


@pytest.mark.critical
@pytest.mark.regression
def test_hostile_names_are_drawn_without_controls_escapes_or_length(drawn):
    hostile = "‮FM​ \x07@everyone <@&1> **negrito**" + "x" * 300
    hostile_data = data(
        station_name=hostile,
        now_title="Faixa \\*com\\* escape",  # como o ``clean_title`` guarda
        now_artist=hostile,
        filter_path=(hostile,),
        next_stations=(NextStation(4, hostile, 128, "good"),),
    )
    render_portrait(PortraitCard(hostile_data), tr)
    for text in drawn:
        assert not any(unicodedata.category(char) in ("Cc", "Cf") for char in text)
        assert len(text) <= 120
    assert "Faixa *com* escape" in drawn  # o escape do ICY sai: nenhuma barra visível na imagem


@pytest.mark.critical
def test_a_character_inter_lacks_is_never_drawn(drawn):
    from empiremuse.utils.radio.visor import inter_points

    glyphless = "Canção 🎵 夜に駆ける أغنية"
    names = dict(station_name=glyphless, now_title=glyphless, now_artist=glyphless, filter_path=(glyphless,))
    png = render_portrait(PortraitCard(data(**names, next_stations=(NextStation(4, glyphless, 128, "good"),))), tr)

    points = inter_points()
    assert all(ord(char) in points for text in drawn for char in text if not char.isspace())
    assert any("Canção" in text for text in drawn)
    assert decoded(png).size == (960, 1680)


@pytest.mark.critical
def test_the_icy_title_loses_one_escape_level():
    assert icy_text("AC\\/DC \\- Back in Black") == "AC/DC - Back in Black"
    assert icy_text("\x07") == ""


@pytest.mark.critical
@pytest.mark.parametrize(("index", "position", "following"), [(2, 3, [4, 5, 6, 7]), (7, 8, [9]), (None, 0, [])])
def test_the_snapshot_gives_up_to_four_following_stations(index, position, following):
    """Até 4 à frente; com menos, as que faltam somem (posições fixas); fora da lista, nenhuma."""
    assert [s.position for s in from_radio_snapshot(snapshot(index, position)).next_stations] == following


@pytest.mark.critical
def test_the_next_list_shows_four_stations(drawn):
    render_portrait(PortraitCard(data()), tr)
    assert [text for text in drawn if text[:2] in ("4.", "5.", "6.", "7.")] == [
        "4. Onda Sul FM",
        "5. Litoral Hits",
        "6. Serra Azul FM",
        "7. Vale Verde FM",
    ]
