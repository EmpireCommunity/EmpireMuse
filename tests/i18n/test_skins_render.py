"""Skins do player (item 21): o pt-BR é o texto de hoje palavra por palavra e o en-US cabe nos limites do Discord."""

import json
from pathlib import Path

import pytest

from empiremuse.i18n import get_i18n
from tests.i18n.skins_harness import render

pytestmark = pytest.mark.critical

GOLDEN = json.loads((Path(__file__).parent / "golden_skins_pt_br.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def pt_br():
    return render(get_i18n().for_locale("pt-BR"))


@pytest.fixture(scope="module")
def en_us():
    return render(get_i18n().for_locale("en-US"))


def test_the_pt_br_render_of_every_skin_and_variant_is_todays_text(pt_br):
    assert set(pt_br) == set(GOLDEN)
    for skin, variants in GOLDEN.items():
        for variant, expected in variants.items():
            assert pt_br[skin][variant] == expected, (skin, variant)


def texts(data):
    """Todo texto de uma renderização com o limite do Discord que vale para ele."""
    for embed in data.get("embeds", []) + ([data["embed"]] if "embed" in data else []):
        yield "embed.title", embed.get("title", ""), 256
        yield "embed.description", embed.get("description", ""), 4096
        yield "embed.footer", embed.get("footer", {}).get("text", ""), 2048
        for field in embed.get("fields", []):
            yield "embed.field.name", field["name"], 256
            yield "embed.field.value", field["value"], 1024
    yield "content", data.get("content") or "", 2000
    for row in data.get("components", []):
        for component in row.get("components", [row]):
            yield "label", component.get("label") or "", 80
            yield "placeholder", component.get("placeholder") or "", 150
            for option in component.get("options", []):
                yield "option.label", option.get("label", ""), 100
                yield "option.description", option.get("description") or "", 100


def test_the_en_us_render_fits_the_discord_limits(en_us):
    for skin, variants in en_us.items():
        for variant, data in variants.items():
            assert isinstance(data, dict), (skin, variant, data)
            for kind, text, limit in texts(data):
                assert len(text) <= limit, (skin, variant, kind, len(text))
            embeds = data.get("embeds", []) + ([data["embed"]] if "embed" in data else [])
            total = sum(len(json.dumps(e, ensure_ascii=False)) for e in embeds)
            assert total <= 6000 * 3, (skin, variant)


def test_the_en_us_render_is_translated_and_differs_from_pt_br(pt_br, en_us):
    assert pt_br != en_us
    flat = json.dumps(en_us, ensure_ascii=False)
    # os blocos ANSI e as frases montadas em pedaços ainda não têm chave (pendências da F2 parte 1, listadas no PR)
    for portuguese in (
        "Mais opções",
        "Adicionar música",
        "Duração:",
        "Pedido por",
        "Tocar do inicio",
        "Uploader/Artista",
    ):
        assert portuguese not in flat, portuguese
    assert "More options" in flat and "Add song" in flat
