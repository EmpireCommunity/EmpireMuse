"""As fontes das skins (``utils/render/fonts.py``): só arquivos com o hash registrado, peso pelo nome do eixo, reserva por
caractere com teto de trechos, caractere sem glifo fora do texto e nada de ``raqm`` presumido. Usa as fontes do repositório."""

import shutil

import pytest
from PIL import Image, ImageDraw, ImageFont

from empiremuse.utils.render import fonts
from empiremuse.utils.render.fonts import (
    FIGTREE,
    FONTS_DIR,
    MANIFEST,
    NOTO,
    RUNS_MAX,
    UNBOUNDED,
    WEIGHT_AXIS,
    FontBook,
    FontIntegrityError,
    draw_runs,
    text_length,
)

pytestmark = pytest.mark.critical


def book(**kwargs):
    return FontBook(raqm=False, **kwargs)


def owners(result):
    families = {"Figtree": FIGTREE, "Noto Sans": NOTO, "Unbounded": UNBOUNDED}
    return [(run.text, families[run.font.getname()[0]]) for run in result.runs]


# ---------------------------------------------------------------------- integridade


@pytest.mark.parametrize("name", list(MANIFEST))
def test_every_registered_file_loads_with_the_recorded_hash_and_has_a_weight_axis(name):
    font = book().font(name, 600, 20)

    assert WEIGHT_AXIS in [axis["name"] for axis in font.get_variation_axes()]


def test_a_file_swapped_after_the_first_load_never_changes_what_loads(tmp_path):
    shutil.copy(FONTS_DIR / FIGTREE, tmp_path / FIGTREE)
    shelf = book(directory=tmp_path)
    shelf.font(FIGTREE, 600, 20)
    shutil.copy(FONTS_DIR / NOTO, tmp_path / FIGTREE)  # outro conteúdo no mesmo caminho

    assert shelf.font(FIGTREE, 700, 31).getname()[0] == "Figtree"  # novo peso e tamanho: ainda os bytes conferidos


@pytest.mark.parametrize("tamper", ["byte", "unlisted"])
def test_a_font_with_another_hash_or_outside_the_list_is_refused(tmp_path, tamper):
    shutil.copy(FONTS_DIR / FIGTREE, tmp_path / FIGTREE)
    if tamper == "byte":
        data = bytearray((tmp_path / FIGTREE).read_bytes())
        data[-1] ^= 0xFF
        (tmp_path / FIGTREE).write_bytes(bytes(data))
        manifest = MANIFEST
    else:
        manifest = {NOTO: MANIFEST[NOTO]}
    shelf = book(directory=tmp_path, manifest=manifest)

    with pytest.raises(FontIntegrityError):
        shelf.font(FIGTREE, 600, 20)
    with pytest.raises(FontIntegrityError):
        shelf.runs("abc", FIGTREE, 600, 20)


# ---------------------------------------------------------------------- pesos e motor de layout


def test_the_weight_goes_by_axis_name_is_clamped_and_each_size_is_its_own_cached_font():
    shelf = book()
    regular, bold = shelf.font(NOTO, 400, 30), shelf.font(NOTO, 800, 30)

    assert bold.getlength("Hello") > regular.getlength("Hello"), "o peso entrou no eixo Weight, não no Width"
    assert shelf.font(NOTO, 800, 30) is bold and shelf.font(NOTO, 800, 31) is not bold
    assert shelf.font(FIGTREE, 50, 30).getlength("Hi") == shelf.font(FIGTREE, 300, 30).getlength("Hi")  # piso do eixo


@pytest.mark.parametrize(("raqm", "engine"), [(False, ImageFont.Layout.BASIC), (True, ImageFont.Layout.RAQM)])
def test_the_layout_engine_follows_the_raqm_check_never_an_assumption(monkeypatch, raqm, engine):
    monkeypatch.setattr(fonts.features, "check", lambda name: raqm if name == "raqm" else False)
    shelf = FontBook()

    assert shelf.raqm is raqm and shelf.layout == engine
    if not raqm:
        assert shelf.font(FIGTREE, 600, 20).layout_engine == ImageFont.Layout.BASIC


# ---------------------------------------------------------------------- reserva por caractere


def test_the_chain_is_the_skin_font_then_noto_and_neutral_characters_stay_in_the_current_run():
    result = book().runs("Ação Ωμέγα, ok", FIGTREE, 600, 20)

    assert owners(result) == [("Ação ", FIGTREE), ("Ωμέγα, ", NOTO), ("ok", FIGTREE)]
    assert not result.lossy and not result.in_message


def test_a_font_that_covers_the_text_needs_no_fallback():
    assert owners(book().runs("Жар-птица", UNBOUNDED, 700, 20)) == [("Жар-птица", UNBOUNDED)]


@pytest.mark.parametrize(
    ("text", "drawn", "in_message"),
    [
        ("Hit 日本語", "Hit ", False),  # sem glifo nas duas: o caractere sai, o resto fica
        ("日本語", "", True),  # nada sobra: título na mensagem
        ("🎵🎶", "", True),
        ("", "", False),
    ],
)
def test_characters_without_a_glyph_leave_the_text_and_an_empty_result_goes_to_the_message(text, drawn, in_message):
    result = book().runs(text, FIGTREE, 600, 20)

    assert "".join(r.text for r in result.runs) == drawn
    assert result.in_message is in_message and result.lossy is (text != drawn)


@pytest.mark.parametrize("text", ["שלום עולם", "Song مرحبا", "abc‮def"])
def test_right_to_left_text_without_raqm_goes_to_the_message(text):
    result = book().runs(text, FIGTREE, 600, 20)

    assert result.runs == () and result.in_message and result.lossy


def test_at_most_32_runs_and_the_rest_becomes_an_ellipsis():
    result = book().runs("aΩ" * 100, FIGTREE, 600, 20)

    assert len(result.runs) == RUNS_MAX and result.runs[-1].text.endswith("…") and result.lossy


def test_runs_draw_side_by_side_on_one_baseline():
    result = book().runs("Ação Ωμέγα", FIGTREE, 600, 20)
    image = Image.new("L", (int(text_length(result)) + 10, 40))
    draw_runs(ImageDraw.Draw(image), (5, 30), result, 255)

    assert image.getbbox() is not None and text_length(result) > 0
