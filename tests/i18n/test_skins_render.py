"""Skins do player (item 21): o en-US cabe nos limites do Discord e está traduzido. (O pt-BR = main foi provado uma vez, no corpo da PR.)"""

import json

import pytest

from empiremuse.i18n import get_i18n
from tests.i18n.skins_harness import render

pytestmark = pytest.mark.critical


@pytest.fixture(scope="module")
def pt_br():
    return render(get_i18n().for_locale("pt-BR"))


@pytest.fixture(scope="module")
def en_us():
    return render(get_i18n().for_locale("en-US"))


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


def embed_chars(data):
    """O que o Discord soma contra o teto de 6000: título, descrição, rodapé, autor e campos de TODOS os embeds da mensagem."""
    total = 0
    for embed in data.get("embeds", []) + ([data["embed"]] if "embed" in data else []):
        total += len(embed.get("title", "")) + len(embed.get("description", ""))
        total += len(embed.get("footer", {}).get("text", "")) + len(embed.get("author", {}).get("name", ""))
        total += sum(len(f["name"]) + len(f["value"]) for f in embed.get("fields", []))
    return total


def test_the_en_us_render_fits_the_discord_limits(en_us):
    for skin, variants in en_us.items():
        for variant, data in variants.items():
            assert isinstance(data, dict), (skin, variant, data)
            for kind, text, limit in texts(data):
                assert len(text) <= limit, (skin, variant, kind, len(text))
            assert embed_chars(data) <= 6000, (skin, variant)


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
