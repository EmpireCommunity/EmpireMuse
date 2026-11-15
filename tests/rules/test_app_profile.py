"""Regras puras do perfil da aplicação: modelo da descrição, tags válidas e a diferença para o que já está no Discord."""

import pytest

from empiremuse.rules import app_profile
from empiremuse.rules.app_profile import (
    DEFAULT_DESCRIPTION,
    DEFAULT_TAGS,
    DESCRIPTION_LIMIT,
    DesiredProfile,
    build_profile,
    panel_url_of,
    parse_tags,
    plan_changes,
    render_description,
    truncate_description,
    utf16_length,
)

pytestmark = pytest.mark.critical

PANEL = "https://muse.empirecommunity.org/panel"
SUPPORT = "https://empirecommunity.org/discord"


def render(template=DEFAULT_DESCRIPTION, **values):
    values = {"prefix": "!", "bots": 5, "panel_url": PANEL, "support_url": SUPPORT} | values
    return render_description(template, **values)


# ---------------------------------------------------------------------- descrição


def test_default_description_with_everything_set_fits_the_discord_limit():
    text, truncated = render()
    assert truncated is False
    assert utf16_length(text) <= DESCRIPTION_LIMIT
    assert "!play <nome ou link>. Ajuda: !help" in text
    assert "Somos 5 bots" in text
    assert text.endswith(f"💬 Suporte: {SUPPORT}")


def test_bots_line_is_dropped_with_a_single_bot():
    text, _ = render(bots=1)
    assert "bots" not in text
    assert len(text.splitlines()) == 4


@pytest.mark.parametrize(
    ("empty", "marker"), [("panel_url", "Painel"), ("support_url", "Suporte"), ("prefix", "Comece")]
)
def test_a_line_whose_placeholder_resolves_empty_is_dropped(empty, marker):
    text, _ = render(**{empty: ""})
    assert marker not in text
    assert "{" not in text


def test_literal_backslash_n_in_the_template_is_a_line_break():
    text, _ = render("linha 1\\nlinha {prefix}")
    assert text == "linha 1\nlinha !"


def test_unknown_braces_stay_and_values_cannot_inject_control_characters():
    text, _ = render("{outro} {prefix}", prefix="!\x1b‮\n?")
    assert text == "{outro} !?"


def test_all_lines_dropped_leaves_an_empty_description():
    assert render("Painel: {panel_url}", panel_url="") == ("", False)


def test_non_text_template_renders_nothing():
    assert render(None) == ("", False)


def test_long_description_drops_whole_trailing_lines_and_reports_it():
    template = "primeira\n" + "x" * 200 + "\n" + "y" * 200 + "\nlink: {support_url}"
    text, truncated = render(template)
    assert truncated is True
    assert text == "primeira\n" + "x" * 200
    assert utf16_length(text) <= DESCRIPTION_LIMIT


def test_a_first_line_alone_over_the_limit_is_cut_with_an_ellipsis():
    out = truncate_description("🎵" * 300)
    assert utf16_length(out) <= DESCRIPTION_LIMIT
    assert out.endswith("…")


def test_truncation_counts_utf16_units_so_an_emoji_is_never_split():
    assert truncate_description("a" + "🎵" * 300, 11) == "a" + "🎵" * 4 + "…"


# ---------------------------------------------------------------------- painel


@pytest.mark.parametrize(
    ("base", "enabled", "expected"),
    [
        ("https://self.example", True, "https://self.example/painel"),
        ("https://self.example/", True, "https://self.example/painel"),
        ("https://muse.empirecommunity.org", False, ""),
        ("", True, ""),
        (None, True, ""),
    ],
)
def test_panel_url_needs_the_panel_enabled_and_a_base_url(base, enabled, expected):
    assert panel_url_of(base, enabled) == expected


# ---------------------------------------------------------------------- tags


def test_default_tags_are_all_valid():
    tags, warnings = parse_tags(DEFAULT_TAGS)
    assert tags == ("música", "music", "player", "spotify", "youtube")
    assert warnings == ()


def test_invalid_repeated_and_extra_tags_are_dropped_with_a_warning():
    tags, warnings = parse_tags(" a , ,b,A,c\x00,d,e,f,g, " + "x" * 21)
    assert tags == ("a", "b", "d", "e", "f")
    assert len(warnings) == 4  # A repetida, c com controle, g acima de 5, x*21 grande demais
    assert any("repetida" in w for w in warnings)
    assert any("máximo 5" in w for w in warnings)


def test_a_tag_of_exactly_the_limit_is_valid():
    tags, warnings = parse_tags("x" * app_profile.TAG_MAX_LENGTH)
    assert len(tags) == 1 and warnings == ()


def test_non_text_tags_are_empty():
    assert parse_tags(None) == ((), ())


# ---------------------------------------------------------------------- perfil desejado


def build(**fields):
    values = {
        "description": DEFAULT_DESCRIPTION,
        "tags": DEFAULT_TAGS,
        "prefix": "!",
        "bots": 5,
        "panel_url": PANEL,
        "support_url": SUPPORT,
    } | fields
    return build_profile(**values)


def test_empty_values_opt_out_of_each_field():
    desired = build(description="", tags="  ")
    assert desired == DesiredProfile()
    assert plan_changes("qualquer", ["x"], desired) == {}


def test_tags_without_a_single_valid_one_do_not_wipe_the_current_tags():
    desired = build(tags="x" * 30)
    assert desired.tags is None and desired.warnings


def test_build_reports_a_truncated_description():
    desired = build(description="a" * 500)
    assert desired.description is not None and utf16_length(desired.description) <= DESCRIPTION_LIMIT
    assert any("encurtada" in w for w in desired.warnings)


# ---------------------------------------------------------------------- diferença


def test_plan_sends_only_the_fields_that_changed():
    desired = build()
    assert plan_changes(desired.description, list(desired.tags), desired) == {}
    assert plan_changes("outra", list(desired.tags), desired) == {"description": desired.description}
    assert plan_changes(desired.description, ["music"], desired) == {"tags": list(desired.tags)}
    assert plan_changes(None, None, desired) == {"description": desired.description, "tags": list(desired.tags)}


def test_plan_ignores_crlf_and_edge_whitespace_in_the_current_description():
    desired = build()
    current = " " + desired.description.replace("\n", "\r\n") + "\n"
    assert plan_changes(current, list(desired.tags), desired) == {}
