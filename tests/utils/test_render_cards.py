"""Os renderizadores das skins de imagem da Jukebox (``utils/render/cards``): fatos estruturais, nunca pixels de referência.

Confere tamanho e modo da imagem, quais chaves do catálogo são pedidas (tradutor que grava), que texto chega ao desenho
(``FontBook`` que grava cada texto passado ao ``runs``), a call privada, a linha do tempo, o teto do GIF, a projeção e os
ladrilhos do disco. Usa as fontes do repositório; sem rede, sem relógio.
"""

import sys
import unicodedata

import pytest
from PIL import Image

from empiremuse.rules.card import (
    By,
    CardSpec,
    CardState,
    make_item,
    make_radio,
    make_station,
    make_track,
    card_text,
    seal,
)
from empiremuse.utils.music import card_facts
from empiremuse.utils.radio.visor import inter_points, inter_text
from empiremuse.utils.render.cards import disc, radio_portrait, visor_jukebox
from empiremuse.utils.render.cards.layout import (
    COVER_SIDE,
    LABEL_KEYS,
    CardArt,
    FontSpec,
    theme_for,
)
from empiremuse.rules.i18n import Translator
from empiremuse.utils.render.fonts import FIGTREE, MANIFEST, FontBook
from empiremuse.utils.render.gif import encode_png

pytestmark = pytest.mark.critical

FAMILIES = {"visor": visor_jukebox, "portrait": radio_portrait}
SIZES = {
    "visor": (
        visor_jukebox.visor.WIDTH * visor_jukebox.visor.SCALE,
        visor_jukebox.visor.HEIGHT * visor_jukebox.visor.SCALE,
    ),
    "portrait": (radio_portrait.WIDTH * radio_portrait.SCALE, radio_portrait.HEIGHT * radio_portrait.SCALE),
}
SECRETS = ("Segredo Violeta", "Artista Oculta", "Pedinte Raro", "Sala Secreta", "Fila Escondida")


class RecordingBook(FontBook):
    """O ``FontBook`` de verdade, gravando todo texto que passa pelo ``runs`` (todo texto que o cartão desenha passa)."""

    def __init__(self) -> None:
        super().__init__(raqm=False)
        self.texts: list[str] = []

    def runs(self, text, name, weight, size):
        self.texts.append(text)
        return super().runs(text, name, weight, size)


# Os textos com marcadores como o catálogo de verdade os terá; as outras chaves viram "T:chave".
TEMPLATES = {
    "jbx.requested_by": "Pedida por {nome}",
    "jbx.from_station": "estação {nome}",
    "jbx.then_station": "Depois: estação {nome}",
    "jbx.up_next": "A seguir · {n}",
    "jbx.and_more": "e mais {n}",
    "jbx.retry": "Tentativa {n}",
    "jbx.vote": "{votos}/{quorum}",
    "jbx.repeat.track": "Repetir a faixa ({n})",
}
CATALOG = {**{key: f"T:{key}" for key in LABEL_KEYS}, **TEMPLATES}


class RecordingTr:
    """O ``Translator`` de verdade (``rules/i18n.py``) com um catálogo de teste, gravando as chaves pedidas. Como o real, uma
    chave com marcador pedida sem os valores devolve a PRÓPRIA chave: o teste de marcadores pega isso."""

    def __init__(self) -> None:
        self.keys: list[str] = []
        self._real = Translator("pt-BR", CATALOG, CATALOG)

    def __call__(self, key: str, **values) -> str:
        self.keys.append(key)
        return self._real(key, **values)


@pytest.fixture(scope="module")
def shared_book():
    return RecordingBook()


@pytest.fixture
def book(shared_book):
    shared_book.texts.clear()
    return shared_book


def cover_bytes() -> bytes:
    return Image.new("RGB", (COVER_SIDE, COVER_SIDE), (200, 60, 120)).tobytes()


def logo_png() -> bytes:
    return encode_png(Image.new("RGB", (32, 32), (10, 200, 30)))


def queue(n: int = 4):
    return tuple(
        make_item(title=f"Faixa {i}", artists=f"Banda {i}", duration_s=180 + i, requester=f"Membro {i}")
        for i in range(1, n + 1)
    )


def base_spec(**changes) -> CardSpec:
    spec = CardSpec(
        skin="muse_expanded_black",
        theme="black",
        scale=1,
        render_version=1,
        locale="pt-BR",
        state=CardState.PLAYING,
        hidden=False,
        track=make_track(
            title="Noite em Órbita", artists="Lua Violeta", origin="Órbita Lenta", duration_s=209, requester="Alex"
        ),
        cover="https://covers.example/x.jpg",
        paused_at_s=None,
        queue_total=12,
        queue=queue(8),
        queue_kind="queue",
        session_played=3,
        flags=frozenset({card_facts.FLAG_AUTOPLAY}),
        repeat_left=0,
        volume=80,
        bot_name="Muse 2",
        voice_name="Lounge",
        radio=None,
        station=None,
        motion=True,
    )
    return spec._replace(**changes)


def radio_facts(**changes):
    values = dict(
        name="Rádio Aurora FM", title="Céu de Neon", country="BR", place="São Paulo", codec="mp3", bitrate=128
    )
    return make_radio(**(values | changes))


STATES = {
    "playing": lambda: base_spec(),
    "paused": lambda: base_spec(state=CardState.PAUSED, paused_at_s=72),
    "stream": lambda: base_spec(
        state=CardState.STREAM, track=make_track(title="Live Set", artists="DJ Nebula", duration_s=None)
    ),
    "radio_live": lambda: base_spec(
        state=CardState.RADIO_LIVE, track=None, cover=None, queue=(), queue_total=0, radio=radio_facts(on_air=True)
    ),
    "radio_retry": lambda: base_spec(
        state=CardState.RADIO_RETRY, track=None, cover=None, queue=(), queue_total=0, radio=radio_facts(attempt=2)
    ),
    "idle": lambda: base_spec(state=CardState.IDLE, track=None, cover=None, queue=(), queue_total=0),
    "station": lambda: base_spec(
        station=make_station(name="Lo-fi da Madrugada", kind="Gênero"), queue=queue(2), queue_total=2
    ),
}


def asked(tr: RecordingTr, family: str) -> set[str]:
    keys = set(tr.keys)
    return {key for key in keys if not key.startswith("visor.")} if family in ("visor", "portrait") else keys


# ---------------------------------------------------------------------- imagem por família, tema e estado


@pytest.mark.parametrize("state", list(STATES))
@pytest.mark.parametrize("theme", ["black", "white"])
@pytest.mark.parametrize("family", list(FAMILIES))
def test_every_family_draws_an_opaque_rgb_card_of_its_size_asking_only_listed_keys(family, theme, state, book):
    tr = RecordingTr()

    image = FAMILIES[family].draw(STATES[state]()._replace(theme=theme), CardArt(cover_bytes(), logo_png()), tr, book)

    assert image.size == SIZES[family]
    assert image.mode == "RGB"
    assert asked(tr, family) <= set(LABEL_KEYS)
    assert not any(ord(char) >= 0x1F000 for text in book.texts for char in text)  # nenhum emoji desenhado


def test_label_keys_has_no_duplicates():
    assert len(LABEL_KEYS) == len(set(LABEL_KEYS))


# ---------------------------------------------------------------------- call privada


def hidden_spec(sealed: bool) -> CardSpec:
    secret = base_spec(
        hidden=True,
        track=make_track(title=SECRETS[0], artists=SECRETS[1], origin=SECRETS[4], requester=SECRETS[2]),
        voice_name=SECRETS[3],
        queue=(make_item(title=SECRETS[4], artists=SECRETS[1], requester=SECRETS[2]),),
        queue_total=3,
        station=make_station(name=SECRETS[4], kind="x"),
    )
    return seal(secret) if sealed else secret


@pytest.mark.parametrize("sealed", [True, False])
def test_a_private_call_never_reaches_the_radio_visor_data(sealed, book):
    tr = RecordingTr()
    logo = logo_png()

    view = visor_jukebox.visor_view(hidden_spec(sealed), CardArt(cover_bytes(), logo), tr, book)
    visor_jukebox.draw(hidden_spec(sealed), CardArt(cover_bytes(), logo), tr, book)

    shown = repr(view.data._replace(logo=None)) + repr(view.labels)
    assert not [secret for secret in SECRETS if secret in shown]
    assert view.data.logo == logo
    assert "jbx.hidden_track" in tr.keys


@pytest.mark.parametrize("family", list(FAMILIES))
def test_private_projections_carry_no_track_data(family):
    projected = FAMILIES[family].project(hidden_spec(sealed=False))

    assert (projected.track, projected.cover, projected.voice_name, projected.queue) == (None, None, "", ())
    assert (projected.radio, projected.station) == (None, None)


@pytest.mark.parametrize("size", [0, COVER_SIDE * COVER_SIDE * 3 - 1, COVER_SIDE * COVER_SIDE * 4])
def test_a_cover_of_another_size_is_no_cover(size):
    assert disc.cover_image(bytes(size)) is None


# ---------------------------------------------------------------------- R e S


@pytest.mark.parametrize("family", list(FAMILIES))
def test_radio_retry_shows_the_attempt(family, book):
    tr = RecordingTr()

    FAMILIES[family].draw(STATES["radio_retry"](), CardArt(None, logo_png()), tr, book)

    assert "jbx.retry" in tr.keys


def test_the_radio_visor_in_r_is_the_station_visor(book):
    view = visor_jukebox.visor_view(STATES["radio_live"](), CardArt(None, logo_png()), RecordingTr(), book)

    assert (view.data.station_name, view.data.now_title) == ("Rádio Aurora FM", "Céu de Neon")
    assert (view.data.codec, view.data.bitrate_kbps) == ("", 0)  # nada técnico no cartão da rádio ao vivo


def test_the_radio_visor_in_m_shows_the_track_cover_and_session(book):
    view = visor_jukebox.visor_view(STATES["playing"](), CardArt(cover_bytes(), logo_png()), RecordingTr(), book)

    assert view.data.station_name == "Noite em Órbita"
    assert view.data.position == 4  # 3 já tocadas + a atual
    assert view.data.logo is not None and view.data.logo.startswith(b"\x89PNG")
    assert len(view.data.next_stations) == visor_jukebox.NEXT_MAX


# ---------------------------------------------------------------------- linha do tempo


# ---------------------------------------------------------------------- orçamento do GIF


def test_the_radio_visor_has_no_disc():
    assert visor_jukebox.disc_box(base_spec()) is None
    with pytest.raises(ValueError):
        visor_jukebox.disc_tiles(base_spec(), CardArt(None, None), 3)


# ---------------------------------------------------------------------- ladrilhos do disco


# ---------------------------------------------------------------------- projeção


@pytest.mark.parametrize("family", list(FAMILIES))
@pytest.mark.parametrize("state", list(STATES))
def test_projection_is_idempotent(family, state):
    module = FAMILIES[family]
    once = module.project(STATES[state]())

    assert module.project(once) == once


# ---------------------------------------------------------------------- flags e chips


# ---------------------------------------------------------------------- texto


@pytest.mark.regression
@pytest.mark.parametrize("family", list(FAMILIES))
@pytest.mark.parametrize("state", ["playing", "radio_retry", "station", "paused"])
def test_texts_with_placeholders_are_filled_never_drawn_as_the_key(family, state, book):
    spec = STATES[state]()
    spec = spec._replace(
        flags=spec.flags | {card_facts.FLAG_REPEAT_TRACK},
        repeat_left=2,
        queue=queue(6),
        queue_total=6,
        queue_kind="queue",
    )

    FAMILIES[family].draw(spec, CardArt(cover_bytes(), logo_png()), RecordingTr(), book)

    drawn = " ".join(book.texts)
    assert not any(key in drawn for key in TEMPLATES) and "{" not in drawn


# ---------------------------------------------------------------------- visor da Rádio da Jukebox: só o que a Inter desenha

GLYPHLESS = "Canção 🎵 夜に駆ける"


def _visor_texts(view) -> list[str]:
    data = view.data
    return [
        data.station_name,
        data.now_title,
        data.now_artist,
        data.codec,
        *data.filter_path,
        *(s.name for s in data.next_stations),
    ]


@pytest.mark.regression
@pytest.mark.parametrize("state", ["playing", "radio_live"])
def test_the_radio_visor_never_draws_a_character_inter_lacks(book, state):
    track = make_track(title=GLYPHLESS, artists=GLYPHLESS, origin=GLYPHLESS, duration_s=209, requester=GLYPHLESS)
    items = tuple(make_item(title=GLYPHLESS, artists=GLYPHLESS) for _ in range(3))
    spec = (
        base_spec(track=track, queue=items, queue_total=3)
        if state == "playing"
        else STATES["radio_live"]()._replace(
            radio=radio_facts(name=GLYPHLESS, title=GLYPHLESS, place=GLYPHLESS, codec="🎵夜A")
        )
    )

    view = visor_jukebox.visor_view(spec, CardArt(cover_bytes(), logo_png()), RecordingTr(), book)

    points = inter_points()
    texts = _visor_texts(view)
    assert all(ord(char) in points for text in texts for char in text if not char.isspace())
    assert any("Canção" in text for text in texts)
    if state == "playing":
        assert view.data.station_name == "T:jbx.title_in_text"  # o título real vai no autor do embed


RIGHT_TO_LEFT = frozenset({"R", "AL", "AN", "RLE", "RLO", "RLI", "LRE", "LRO", "LRI", "FSI", "PDF", "PDI"})


@pytest.mark.critical
def test_no_right_to_left_character_reaches_a_drawing():
    """Rótulos com um nome de fora (``jbx.requested_by``, ``jbx.from_station``) saem num só ``draw.text``. Com ``raqm`` um nome
    da direita para a esquerda inverteria o rótulo, então isso só é seguro enquanto nenhum caractere assim chega ao desenho:
    nenhuma fonte do repositório tem o glifo e o texto do cartão tira os controles. Uma fonte nova com RTL pede o nome à parte."""
    fonts = FontBook()
    rtl = tuple(chr(c) for c in range(sys.maxunicode + 1) if unicodedata.bidirectional(chr(c)) in RIGHT_TO_LEFT)

    kept = {d for c in rtl for d in card_text(c, 8) if unicodedata.bidirectional(d) in RIGHT_TO_LEFT}  # depois do NFKC
    cards = sorted(d for d in kept if any(fonts.has_glyph(name, d) for name in MANIFEST))
    visor = sorted(d for d in kept if inter_text(d)[0])  # o visor escreve só em Inter

    assert (cards, visor) == ([], [])


# ---------------------------------------------------------------------- Retrato da Jukebox (D14): o renderizador do Retrato


@pytest.fixture
def portrait_texts(monkeypatch):
    """Todo texto que o Retrato desenha (o ``_Canvas.text`` dele; o ``FontBook`` não passa por lá)."""
    seen: list[str] = []
    original = radio_portrait._Canvas.text

    def spy(self, xy, text, *args, **kwargs):
        seen.append(text)
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(radio_portrait._Canvas, "text", spy)
    return seen


@pytest.mark.parametrize("sealed", [True, False])
def test_a_private_call_never_reaches_the_jukebox_portrait(sealed, portrait_texts, book):
    radio_portrait.draw(hidden_spec(sealed), CardArt(cover_bytes(), logo_png()), RecordingTr(), book)

    drawn = " ".join(portrait_texts)
    assert not [secret for secret in SECRETS if secret in drawn]
    assert "T:jbx.hidden_track" in portrait_texts


def test_the_jukebox_portrait_shows_four_queued_tracks_where_the_visor_shows_two(portrait_texts, book):
    spec = base_spec(skin="muse_radio_portrait_white", theme="white")

    radio_portrait.draw(spec, CardArt(cover_bytes(), logo_png()), RecordingTr(), book)

    assert len([text for text in portrait_texts if "Faixa " in text]) == radio_portrait.NEXT_MAX == 4
    assert len(visor_jukebox.project(spec).queue) == visor_jukebox.NEXT_MAX == 2
