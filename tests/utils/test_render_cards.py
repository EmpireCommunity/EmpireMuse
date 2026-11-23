"""Os renderizadores das skins de imagem da Jukebox (``utils/render/cards``): fatos estruturais, nunca pixels de referência.

Confere tamanho e modo da imagem, quais chaves do catálogo são pedidas (tradutor que grava), que texto chega ao desenho
(``FontBook`` que grava cada texto passado ao ``runs``), a call privada, a linha do tempo, o teto do GIF, a projeção e os
ladrilhos do disco. Usa as fontes do repositório; sem rede, sem relógio.
"""

import pytest
from PIL import Image

from empiremuse.rules.card import (
    DISC_MAX_PX_1X,
    By,
    CardSpec,
    CardState,
    can_animate,
    make_item,
    make_radio,
    make_station,
    make_track,
    seal,
)
from empiremuse.utils.music import card_facts
from empiremuse.utils.render.cards import disc, expanded, slim, visor_jukebox
from empiremuse.utils.render.cards.layout import (
    COVER_SIDE,
    EXPANDED,
    LABEL_KEYS,
    SLIM,
    CardArt,
    FontSpec,
    fill,
    gif_fits,
    theme_for,
)
from empiremuse.utils.render.fonts import FIGTREE, FontBook
from empiremuse.utils.render.gif import encode_png

pytestmark = pytest.mark.critical

FAMILIES = {"expanded": expanded, "slim": slim, "visor": visor_jukebox}
SIZES = {"expanded": EXPANDED.size, "slim": SLIM.size, "visor": visor_jukebox.VISOR_FILE_SIZE}
SECRETS = ("Segredo Violeta", "Artista Oculta", "Pedinte Raro", "Sala Secreta", "Fila Escondida")


class RecordingBook(FontBook):
    """O ``FontBook`` de verdade, gravando todo texto que passa pelo ``runs`` (todo texto que o cartão desenha passa)."""

    def __init__(self) -> None:
        super().__init__(raqm=False)
        self.texts: list[str] = []

    def runs(self, text, name, weight, size):
        self.texts.append(text)
        return super().runs(text, name, weight, size)


class RecordingTr:
    def __init__(self) -> None:
        self.keys: list[str] = []

    def __call__(self, key: str) -> str:
        self.keys.append(key)
        return key


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
    return {key for key in keys if not key.startswith("visor.")} if family == "visor" else keys


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
@pytest.mark.parametrize("family", ["expanded", "slim"])
def test_a_private_call_never_draws_the_track_requester_or_voice_and_asks_the_hidden_title(family, sealed, book):
    tr = RecordingTr()

    FAMILIES[family].draw(hidden_spec(sealed), CardArt(cover_bytes(), logo_png()), tr, book)

    drawn = " ".join(book.texts)
    assert not [secret for secret in SECRETS if secret in drawn]
    assert "jbx.hidden_track" in tr.keys


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


@pytest.mark.parametrize("sealed", [True, False])
def test_a_private_call_puts_the_logo_on_the_label_even_with_a_cover(sealed):
    spec, theme = expanded.project(hidden_spec(sealed)), theme_for("black")
    logo = logo_png()

    with_cover = disc.label_image(spec, CardArt(cover_bytes(), logo), EXPANDED.label_side, theme)
    without = disc.label_image(spec, CardArt(None, logo), EXPANDED.label_side, theme)

    assert with_cover.tobytes() == without.tobytes()


@pytest.mark.parametrize("family", list(FAMILIES))
def test_private_projections_carry_no_track_data(family):
    projected = FAMILIES[family].project(hidden_spec(sealed=False))

    assert (projected.track, projected.cover, projected.voice_name, projected.queue) == (None, None, "", ())
    assert (projected.radio, projected.station) == (None, None)


@pytest.mark.parametrize("size", [0, COVER_SIDE * COVER_SIDE * 3 - 1, COVER_SIDE * COVER_SIDE * 4])
def test_a_cover_of_another_size_is_no_cover(size):
    assert disc.cover_image(bytes(size)) is None


# ---------------------------------------------------------------------- R e S


@pytest.mark.parametrize("family", ["expanded", "slim"])
def test_radio_live_draws_the_icy_title_and_station_with_on_air_instead_of_the_queue(family, book):
    tr = RecordingTr()

    FAMILIES[family].draw(STATES["radio_live"](), CardArt(cover_bytes(), logo_png()), tr, book)

    assert {"Céu de Neon", "Rádio Aurora FM"} <= set(book.texts)
    assert "jbx.on_air" in tr.keys
    assert "jbx.up_next" not in tr.keys


@pytest.mark.parametrize("family", ["expanded", "slim"])
def test_radio_never_uses_the_cover_on_the_label(family):
    spec = FAMILIES[family].project(STATES["radio_live"]()._replace(cover="https://covers.example/x.jpg"))

    assert not disc.uses_cover(spec)


@pytest.mark.parametrize("family", list(FAMILIES))
def test_radio_retry_shows_the_attempt(family, book):
    tr = RecordingTr()

    FAMILIES[family].draw(STATES["radio_retry"](), CardArt(None, logo_png()), tr, book)

    assert "jbx.retry" in tr.keys


@pytest.mark.parametrize("family", ["expanded", "slim"])
def test_station_mode_draws_the_station_line(family, book):
    tr = RecordingTr()

    FAMILIES[family].draw(STATES["station"](), CardArt(None, logo_png()), tr, book)

    assert {"jbx.then_station", "jbx.state.station"} <= set(tr.keys)


def test_the_radio_visor_in_r_is_the_station_visor(book):
    view = visor_jukebox.visor_view(STATES["radio_live"](), CardArt(None, logo_png()), RecordingTr(), book)

    assert (view.data.station_name, view.data.now_title, view.data.bitrate_kbps) == (
        "Rádio Aurora FM",
        "Céu de Neon",
        128,
    )


def test_the_radio_visor_in_m_shows_the_track_cover_and_session(book):
    view = visor_jukebox.visor_view(STATES["playing"](), CardArt(cover_bytes(), logo_png()), RecordingTr(), book)

    assert view.data.station_name == "Noite em Órbita"
    assert view.data.position == 4  # 3 já tocadas + a atual
    assert view.data.logo is not None and view.data.logo.startswith(b"\x89PNG")
    assert len(view.data.next_stations) == visor_jukebox.NEXT_MAX


# ---------------------------------------------------------------------- linha do tempo


@pytest.mark.parametrize(
    ("duration", "marks"),
    [
        (59, ()),
        (209, (60, 120, 180)),
        (600, tuple(range(60, 600, 60))),
        (601, (300, 600)),
        (3599, tuple(range(300, 3599, 300))),
        (3601, ()),
        (None, ()),
    ],
)
def test_ruler_marks(duration, marks):
    assert expanded.ruler_marks(duration) == marks


@pytest.mark.parametrize(
    ("spec", "state"),
    [
        (base_spec(), "ruler"),
        (base_spec(state=CardState.PAUSED, paused_at_s=72), "paused"),
        (base_spec(state=CardState.STREAM), "live"),
        (base_spec(track=make_track(title="x", artists="y", duration_s=None)), "live"),
        (STATES["radio_live"](), "live"),
        (STATES["radio_retry"](), "none"),
        (STATES["idle"](), "none"),
        (seal(base_spec(hidden=True)), "none"),
    ],
)
def test_timeline_state(spec, state):
    assert expanded.timeline_state(spec) == state


def test_paused_draws_the_exact_position_and_playing_draws_no_elapsed_time(book):
    expanded.draw(base_spec(state=CardState.PAUSED, paused_at_s=72), CardArt(None, None), RecordingTr(), book)
    paused = set(book.texts)
    book.texts.clear()
    expanded.draw(base_spec(paused_at_s=72), CardArt(None, None), RecordingTr(), book)

    assert "1:12" in paused
    assert "1:12" not in book.texts


def test_live_timeline_asks_the_live_word(book):
    tr = RecordingTr()

    expanded.draw(STATES["stream"](), CardArt(None, None), tr, book)

    assert "jbx.state.radio" in tr.keys


# ---------------------------------------------------------------------- orçamento do GIF


@pytest.mark.parametrize(
    ("card", "box", "scale", "fits"),
    [
        ((480, 840), (118, 126, 362, 370), 1, True),
        ((481, 840), (118, 126, 362, 370), 1, False),
        ((480, 841), (118, 126, 362, 370), 1, False),
        ((480, 840), (0, 0, DISC_MAX_PX_1X + 1, DISC_MAX_PX_1X + 1), 1, False),
        ((240, 420), (0, 0, 141, 141), 2, False),  # 141 × 2 passa do teto
        ((240, 420), (0, 0, 140, 140), 2, True),
        ((480, 840), None, 1, False),
    ],
)
def test_gif_fits(card, box, scale, fits):
    assert gif_fits(card, box, scale) is fits


@pytest.mark.parametrize(("module", "layout", "side"), [(expanded, EXPANDED, 244), (slim, SLIM, 196)])
def test_disc_boxes_animate_at_1x_and_fit_the_gif(module, layout, side):
    box = module.disc_box(base_spec())

    assert box is not None and (box[2] - box[0], box[3] - box[1]) == (side, side)
    assert can_animate(box, 1)
    assert gif_fits(layout.size, box, 1)


def test_the_radio_visor_has_no_disc():
    assert visor_jukebox.disc_box(base_spec()) is None
    with pytest.raises(ValueError):
        visor_jukebox.disc_tiles(base_spec(), CardArt(None, None), 3)


# ---------------------------------------------------------------------- ladrilhos do disco


@pytest.mark.parametrize("module", [expanded, slim])
def test_disc_tiles_turn_and_tile_zero_is_what_the_card_has(module, book):
    spec, art = base_spec(), CardArt(cover_bytes(), logo_png())
    box = module.disc_box(spec)

    tiles = module.disc_tiles(spec, art, 4)
    card = module.draw(spec, art, RecordingTr(), book)

    assert [tile.size for tile in tiles] == [(box[2] - box[0], box[3] - box[1])] * 4
    assert {tile.mode for tile in tiles} == {"RGB"}
    assert tiles[0].tobytes() == card.crop(box).tobytes()
    assert tiles[1].tobytes() != tiles[0].tobytes()


@pytest.mark.parametrize("frames", [0, -1])
def test_disc_tiles_need_a_frame(frames):
    with pytest.raises(ValueError):
        expanded.disc_tiles(base_spec(), CardArt(None, None), frames)


# ---------------------------------------------------------------------- projeção


def unseen_changes(spec: CardSpec) -> CardSpec:
    """Mudanças que a Slim não desenha: posição da pausa, call, sessão, fila além das linhas, flag desconhecida."""
    longer = spec.queue[:3] + (make_item(title="Outra", artists="Outro", duration_s=99),) * 5
    return spec._replace(
        paused_at_s=99, voice_name="Outra sala", session_played=1, queue=longer, flags=spec.flags | {"x"}, scale=2
    )


def test_slim_projection_ignores_what_it_does_not_draw():
    spec = base_spec(state=CardState.PAUSED, paused_at_s=72)

    assert slim.project(spec) == slim.project(unseen_changes(spec))


def test_expanded_projection_keeps_the_pause_position_and_the_voice():
    spec = base_spec(state=CardState.PAUSED, paused_at_s=72)

    assert expanded.project(spec) != expanded.project(spec._replace(paused_at_s=99))
    assert expanded.project(spec) != expanded.project(spec._replace(voice_name="Outra sala"))


@pytest.mark.parametrize("family", list(FAMILIES))
@pytest.mark.parametrize("state", list(STATES))
def test_projection_is_idempotent(family, state):
    module = FAMILIES[family]
    once = module.project(STATES[state]())

    assert module.project(once) == once


@pytest.mark.parametrize(("total", "shown", "more"), [(2, 2, 0), (4, 4, 0), (5, 3, 2), (12, 3, 9)])
def test_queue_plan_lists_four_lines_at_most(total, shown, more):
    assert expanded.queue_plan(base_spec(queue_total=total, queue=queue(min(total, 8))), 4) == (shown, more)


# ---------------------------------------------------------------------- flags e chips


@pytest.mark.parametrize(
    ("flag", "key"),
    [
        (card_facts.FLAG_AUTOPLAY, "jbx.chip.autoplay"),
        (card_facts.FLAG_NIGHTCORE, "jbx.chip.nightcore"),
        (card_facts.FLAG_ALWAYS_ON, "jbx.chip.always_on"),
        (card_facts.FLAG_RESTRICTED, "jbx.chip.restricted"),
        (card_facts.FLAG_REPEAT_TRACK, "jbx.repeat.track"),
        (card_facts.FLAG_REPEAT_QUEUE, "jbx.repeat.queue"),
    ],
)
def test_every_card_facts_flag_becomes_its_chip(flag, key):
    tr = RecordingTr()

    chips = expanded.chip_items(expanded.project(base_spec(flags=frozenset({flag}), repeat_left=2)), tr)

    assert key in [chip.text for chip in chips]


def test_recommended_track_shows_the_recommended_chip_instead_of_the_requester():
    track = make_track(title="x", artists="y", duration_s=100, by=By.RECOMMENDED)

    chips = expanded.chip_items(base_spec(track=track, flags=frozenset()), RecordingTr())

    assert [chip.text for chip in chips][1] == "jbx.recommended"


# ---------------------------------------------------------------------- texto


def test_a_title_that_cannot_be_drawn_asks_the_title_in_text_key(book):
    tr = RecordingTr()
    rtl = "שלום עולם"

    expanded.draw(base_spec(track=make_track(title=rtl, artists="y", duration_s=100)), CardArt(None, None), tr, book)

    assert "jbx.title_in_text" in tr.keys


@pytest.mark.parametrize(("layout", "lines"), [(EXPANDED, 2), (SLIM, 1)])
def test_a_long_title_is_cut_to_the_layout_lines_with_an_ellipsis(layout, lines, book):
    font = FontSpec(FIGTREE, 700, layout.title_size)
    width = layout.x1 - layout.x0
    title = "Uma faixa com um título muito comprido " * 6

    wrapped = expanded.wrap(book, title, font, width, layout.title_lines)

    assert len(wrapped) == lines
    assert wrapped[-1].endswith("…")
    assert all(expanded.text_length(book.runs(line, *font)) <= width for line in wrapped)


def test_fill_never_reads_a_value_as_a_template():
    assert fill("e mais {n} de {nome}", n="{nome}", nome="Ana") == "e mais {nome} de Ana"
