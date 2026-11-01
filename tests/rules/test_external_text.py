"""R-11 (item 8): a função única do texto externo publicado. Um caso por padrão e por destino; nunca sai menção viva nem link clicável."""

import re

import pytest

from empiremuse.rules.text import external_text

pytestmark = pytest.mark.critical

# destino: (limite em UTF-16, markdown?) — embed/título, rótulo de select, status de voz e tópico do palco
DESTINATIONS = {"embed": (256, True), "select": (100, True), "voice_status": (500, False), "stage_topic": (120, False)}


def clean(text, destination="embed"):
    limit, markdown = DESTINATIONS[destination]
    return external_text(text, limit, markdown=markdown)


@pytest.mark.parametrize("destination", DESTINATIONS)
class TestEveryDestination:
    @pytest.mark.parametrize("hostile", ["@everyone", "@here", "@EVERYONE", "x @everyone y", "@​everyone", "@e​veryone"])
    def test_mass_mentions_never_survive(self, hostile, destination):
        out = clean(hostile, destination)

        assert "@everyone" not in out.lower().replace("․", "") or "․" in out
        assert not re.search(r"@(everyone|here)", out, re.IGNORECASE)

    @pytest.mark.parametrize(
        "hostile",
        ["<@123456789>", "<@!123>", "<@&456>", "<#789>", "</cmd:1>", "<id:customize>", "<:emoji:1>", "<a:dance:2>"],
    )
    def test_discord_markup_is_defused(self, hostile, destination):
        out = clean(hostile, destination)

        assert not re.search(r"<(?:@|#|/|id:|a?:)", out), out

    @pytest.mark.parametrize(
        "hostile",
        [
            "https://evil.example/x",
            "http://evil.example",
            "www.evil.example",
            "[click](http://x.y)",
            "discord.gg/abc",
            "discord.com/invite/abc",
            "dsc.gg/abc",
            "evil.example",
            "sub.evil.co.uk/path",
        ],
    )
    def test_links_and_bare_domains_stop_being_links(self, hostile, destination):
        out = clean(hostile, destination)

        assert "://" not in out and "](" not in out
        assert not re.search(r"[a-z0-9-]\.[a-z]{2,24}\b", out, re.IGNORECASE), out
        assert "www." not in out.lower()

    def test_fullwidth_and_compatibility_forms_are_normalized_before_detection(self, destination):
        out = clean("ｄｉｓｃｏｒｄ．ｇｇ/x ｈｔｔｐ：//ａ．ｂ ＠everyone", destination)

        assert "discord.gg" not in out and "://" not in out and not re.search(r"@everyone", out)

    def test_controls_newlines_bidi_and_invisibles_are_gone(self, destination):
        out = clean("a\nb\r\nc\td‮e⁦f​g⁠h﻿i­j\x00k\x1b[31ml", destination)

        assert "\n" not in out and "\t" not in out
        assert not any(ch in out for ch in "‮⁦​⁠﻿­\x00\x1b")

    def test_a_pile_of_combining_marks_is_cut_to_four(self, destination):
        out = clean("e" + "́" * 40, destination)

        assert out.count("́") == 4

    def test_the_limit_counts_utf16_units_not_characters(self, destination):
        limit = DESTINATIONS[destination][0]
        out = clean("😀" * 400, destination)

        assert sum(2 if ord(c) > 0xFFFF else 1 for c in out) <= limit and out.endswith("…")

    def test_a_cut_never_leaves_a_dangling_backslash(self, destination):
        limit, markdown = DESTINATIONS[destination]
        for pad in range(0, 3):
            out = clean("a" * (limit - 3 - pad) + "***********", destination)
            assert not out.removesuffix("…").endswith("\\") or not markdown

    def test_none_and_numbers_do_not_crash(self, destination):
        assert clean(None, destination) == "" and clean(123, destination) == "123"


class TestMarkdown:
    def test_markdown_is_escaped_for_chat_destinations(self):
        out = clean("*b* _i_ `c` ||s|| > q # h", "embed")

        assert "\\*" in out and "\\_" in out and "\\`" in out and "\\|" in out and "\\>" in out

    def test_plain_text_destinations_keep_the_text_readable(self):
        assert clean("Rock *live* now", "voice_status") == "Rock *live* now"

    def test_ordinary_titles_survive_nearly_untouched(self):
        assert clean("Radio Gaúcha - Ao Vivo", "stage_topic") == "Radio Gaúcha - Ao Vivo"


def test_nothing_hostile_slips_through_even_when_the_pieces_are_combined():
    hostile = "‮@everyone <@&1> https://x.y/z discord.gg/a ](u) ​" * 3

    for destination in DESTINATIONS:
        out = clean(hostile, destination)
        assert "://" not in out and "discord.gg" not in out and not re.search(r"@(everyone|here)", out)
