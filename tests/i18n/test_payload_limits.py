"""Limites do Discord depois da expansão (item 21, I-6e): o pior caso permitido pelo validador cabe em toda skin e em todo componente."""

import ast
import logging
from pathlib import Path

import disnake
import pytest

from empiremuse.i18n import get_i18n
from empiremuse.i18n.payload import fit_payload
from empiremuse.rules import i18n as rules
from tests.i18n.skins_harness import render
from tests.i18n.test_skins_render import embed_chars, texts

pytestmark = pytest.mark.critical

ROOT = Path(__file__).resolve().parents[2] / "empiremuse"
COMPONENTS = {
    ("SelectOption", "label"),
    ("SelectOption", "description"),
    ("Button", "label"),
    ("Select", "placeholder"),
}


def worst_case_translator():
    """Um ``Translator`` em que TODA chave de ``bot.json`` está no teto que o validador aceita (o texto da referência mais ``x``)."""
    i18n = get_i18n()
    reference = i18n.reference
    padded = {}
    for key, template in reference.items():
        if key.startswith("cmd."):
            continue
        ceiling = rules._limit_for(key, template)
        padded[key] = template + "x" * max(0, ceiling - rules.utf16_length(template))
    return rules.Translator("xx", padded, reference)


def test_the_worst_case_text_fits_every_discord_limit_in_every_skin():
    data = render(worst_case_translator(), post=fit_payload)

    checked = 0
    for skin, variants in data.items():
        for variant, payload in variants.items():
            assert isinstance(payload, dict), (skin, variant, payload)
            for kind, text, limit in texts(payload):
                assert rules.utf16_length(text) <= limit, (skin, variant, kind, rules.utf16_length(text))
                checked += 1
            assert embed_chars(payload) <= 6000, (skin, variant)
    assert checked > 1000


def call_name(call: ast.Call) -> str:
    return ast.unparse(call.func).split(".")[-1]


def has_tr(node: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "tr" for n in ast.walk(node)
    )


def test_every_component_text_that_comes_from_a_catalog_goes_through_fit():
    """Quem acrescentar um rótulo traduzido sem ``fit`` quebra aqui (a chave com tipo errado já foi o furo da revisão)."""
    offenders = []
    for path in sorted(ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            for keyword in call.keywords:
                if (call_name(call), keyword.arg) in COMPONENTS and has_tr(keyword.value):
                    wrapped = isinstance(keyword.value, ast.Call) and call_name(keyword.value) == "fit"
                    if not wrapped:
                        offenders.append(
                            f"{path.relative_to(ROOT.parent)}:{keyword.value.lineno} {call_name(call)}.{keyword.arg}"
                        )
    assert not offenders, offenders


class TestFitPayload:
    def test_embed_fields_and_footer_are_cut_in_the_middle(self):
        embed = disnake.Embed(title="t" * 400, description="d" * 5000)
        embed.add_field(name="n" * 300, value="v" * 2000)
        embed.set_footer(text="f" * 3000)

        fit_payload({"embeds": [embed]})

        assert len(embed.title) <= 256 and len(embed.description) <= 4096
        assert (
            len(embed.fields[0].name) <= 256 and len(embed.fields[0].value) <= 1024 and len(embed.footer.text) <= 2048
        )

    def test_the_whole_message_stays_under_6000(self):
        embeds = [disnake.Embed(description="a" * 4000), disnake.Embed(description="b" * 4000)]

        fit_payload({"embeds": embeds})

        assert sum(len(e.description) for e in embeds) <= 6000

    def test_components_are_cut_and_never_split_a_mention(self):
        button = disnake.ui.Button(label="x" * 200)
        select = disnake.ui.Select(
            placeholder="p" * 300,
            options=[disnake.SelectOption(label="l" * 200, description="d" * 200, value="v")],
        )
        data = {"components": [button, select], "content": "<@123456789012345678>" + "c" * 3000}

        fit_payload(data)

        assert len(button.label) <= 80 and len(select.placeholder) <= 150
        assert len(select.options[0].label) <= 100 and len(select.options[0].description) <= 100
        assert len(data["content"]) <= 2000 and data["content"].count("<") == data["content"].count(">")

    def test_it_never_raises_on_unexpected_input(self):
        odd = {"embeds": [object()], "components": [object()]}

        assert fit_payload(odd) is odd and fit_payload(None) is None


class TestBudget:
    def total(self, embeds):
        return sum(
            len(e.title or "")
            + len(e.description or "")
            + len(e.footer.text or "")
            + sum(len(f.name) + len(f.value) for f in e.fields)
            for e in embeds
        )

    def test_three_valid_descriptions_are_shared_without_emptying_anything(self):
        embeds = [disnake.Embed(title="t", description=c * 4000) for c in "abc"]

        fit_payload({"embeds": embeds})

        assert self.total(embeds) <= 6000 and all(e.description and e.title for e in embeds)

    def test_a_skin_without_description_shares_the_cut_among_the_fields_and_keeps_every_required_text(self):
        embed = disnake.Embed(title="t" * 200)
        embed.set_author(name="a" * 200)
        embed.set_footer(text="f" * 2000)
        for n in range(25):
            embed.add_field(name=f"nome{n}" * 20, value="v" * 1000, inline=False)

        fit_payload({"embeds": [embed]})

        assert self.total([embed]) + len(embed.author.name) <= 6000
        assert embed.author.name and embed.title and len(embed.fields) == 25
        assert all(f.name and len(f.value) > 20 for f in embed.fields)  # dividido por igual, não zerado

    def test_the_cut_is_even_not_taken_from_the_first_text(self):
        embeds = [disnake.Embed(description=c * 4000) for c in "abc"]

        fit_payload({"embeds": embeds})

        sizes = [len(e.description) for e in embeds]
        assert max(sizes) - min(sizes) <= 2 and min(sizes) > 1900

    def test_the_first_embed_is_never_dropped_and_extra_embeds_go_last(self):
        embeds = [disnake.Embed(title="x" * 250, description="d" * 10) for _ in range(30)]
        data = {"embeds": embeds}

        fit_payload(data)

        assert data["embeds"][0] is embeds[0] and len(data["embeds"]) < 30

    def test_a_failure_is_logged_with_the_type_only_and_the_data_comes_back(self, monkeypatch, caplog):
        import empiremuse.i18n.payload as payload

        monkeypatch.setattr(payload, "_fit_embed", lambda embed: (_ for _ in ()).throw(RuntimeError("segredo")))
        data = {"embeds": [disnake.Embed(description="x")]}

        with caplog.at_level(logging.WARNING):
            assert fit_payload(data) is data

        assert "RuntimeError" in caplog.text and "segredo" not in caplog.text
