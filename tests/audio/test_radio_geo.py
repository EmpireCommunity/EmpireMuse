"""Regras puras do filtro de local das rádios (item 8, R4): normalização e resolução do estado, país do servidor (o ⭐), ids dos nós e os números que
vêm do diretório (hostil). Sem rede, sem arquivo."""

import pytest

from empiremuse.rules.radio_geo import (
    StateIndex,
    clamp_clicks,
    distance_km,
    guild_country,
    norm,
    parse_node_id,
    resolve_state,
    search_term,
    valid_coordinates,
)

pytestmark = pytest.mark.critical

# Os idiomas que o Discord oferece para um servidor (``preferred_locale``).
DISCORD_LOCALES = (
    "id", "da", "de", "en-GB", "en-US", "es-ES", "es-419", "fr", "hr", "it", "lt", "hu", "nl", "no", "pl", "pt-BR", "ro", "fi", "sv-SE",
    "vi", "tr", "cs", "el", "bg", "ru", "uk", "hi", "th", "zh-CN", "ja", "zh-TW", "ko",
)  # fmt: skip
TREE_COUNTRIES = {"BR", "US", "GB", "ES", "FR", "JP", "UA", "DE", "ID", "TR", "RU", "PL", "KR", "CN", "TW", "IN", "IT"}

INDEX = StateIndex(
    aliases={
        "BR": {
            "goias": "29",
            "go": "29",
            "parana": "18",
            "para": "16",
            "ceara": "06",
            "distrito federal": "07",
            "sao paulo": "27",
            "sp": "27",
        }
    },
    cities={"BR": {"recife": "30", "goiania": "29"}},
    names={"BR": "brazil"},
)


class TestNorm:
    def test_it_drops_accents_case_and_punctuation(self):
        assert norm("  São  Paulo!! ") == "sao paulo"
        assert norm("Goiás") == "goias"
        assert norm("Ｇoiás") == "goias"  # NFKD: o "G" largo vira "G"

    def test_other_scripts_are_kept_not_erased(self):
        assert norm("Москва") == "москва" and norm("Санкт-Петербург") == "санкт петербург" and norm("北京") == "北京"

    def test_what_is_not_text_is_empty(self):
        assert norm(None) == "" and norm(5) == "" and norm(b"x") == ""


class TestResolveState:
    def test_an_exact_alias_name_or_abbreviation(self):
        assert resolve_state(INDEX, "BR", "Goiás") == "29"
        assert resolve_state(INDEX, "BR", "GO") == "29"
        assert resolve_state(INDEX, "BR", "SÃO PAULO") == "27"

    def test_noise_words_and_the_country_name_are_removed(self):
        assert resolve_state(INDEX, "BR", "Goiás State") == "29"
        assert resolve_state(INDEX, "BR", "Goiás Brazil") == "29"

    def test_a_state_whose_name_contains_the_country_is_still_that_state(self):
        index = StateIndex(
            aliases={"AU": {"western australia": "04", "wa": "04", "new south wales": "02", "nsw": "02"}},
            cities={},
            names={"AU": "australia"},
        )

        assert resolve_state(index, "AU", "Western Australia") == "04"
        assert resolve_state(index, "AU", "Australia, Western Australia") == "04"

    def test_a_trailing_abbreviation_is_the_state(self):
        index = StateIndex(aliases={"AU": {"nsw": "02"}, "US": {"ny": "NY", "ca": "CA"}}, cities={}, names={})

        assert resolve_state(index, "AU", "Sydney NSW") == "02"
        assert (
            resolve_state(index, "US", "New York NY") == "NY" and resolve_state(index, "US", "San Francisco CA") == "CA"
        )
        assert resolve_state(index, "US", "NY") == "NY"
        assert resolve_state(index, "US", "Rádio Nova Era") is None  # a última palavra tem de ser uma sigla conhecida

    def test_a_piece_separated_by_punctuation_from_the_last_to_the_first(self):
        assert resolve_state(INDEX, "BR", "Fortaleza, Ceará") == "06"
        assert resolve_state(INDEX, "BR", "Ceará - Fortaleza") == "06"

    def test_the_name_of_a_city_that_exists_in_one_subdivision_only(self):
        assert resolve_state(INDEX, "BR", "Recife") == "30"
        assert resolve_state(INDEX, "BR", "Bairro X / Goiânia") == "29"

    def test_para_never_becomes_parana_nor_the_other_way_around(self):
        assert resolve_state(INDEX, "BR", "Pará") == "16"
        assert resolve_state(INDEX, "BR", "Paraná") == "18"
        assert resolve_state(INDEX, "BR", "para") == "16"

    def test_unknown_empty_huge_or_not_text_is_none(self):
        for bad in ("Narnia", "", "   ", "x" * 129, None, 7, b"Goias"):
            assert resolve_state(INDEX, "BR", bad) is None
        assert resolve_state(INDEX, "ZZ", "Goiás") is None
        assert resolve_state(INDEX, None, "Goiás") is None


class TestGuildCountry:
    def test_known_languages(self):
        assert guild_country("pt-BR", TREE_COUNTRIES) == "BR"
        assert guild_country("en-US", TREE_COUNTRIES) == "US"
        assert guild_country("en-GB", TREE_COUNTRIES) == "GB"
        assert guild_country("fr", TREE_COUNTRIES) == "FR"  # sem território: os likely_subtags do CLDR
        assert guild_country("ja", TREE_COUNTRIES) == "JP"
        assert guild_country("uk", TREE_COUNTRIES) == "UA"

    def test_a_region_that_is_not_a_country_an_unknown_language_and_a_country_outside_the_tree_give_none(self):
        assert guild_country("es-419", TREE_COUNTRIES) is None  # 419 é a América Latina, não um país
        assert guild_country("no", TREE_COUNTRIES) is None  # o Babel não conhece "no": UnknownLocaleError
        assert guild_country("pt-BR", {"US"}) is None  # BR fora da árvore
        for junk in (None, 5, "", "x" * 40, "!!!", "pt_BR; DROP"):
            assert guild_country(junk, TREE_COUNTRIES) is None

    @pytest.mark.parametrize("locale", DISCORD_LOCALES)
    def test_every_discord_locale_answers_without_raising(self, locale):
        result = guild_country(locale, TREE_COUNTRIES)

        assert result is None or result in TREE_COUNTRIES


class TestNodeIds:
    def test_the_four_formats(self):
        assert parse_node_id("r:america_latina").kind == "region"
        assert parse_node_id("c:BR").cc == "BR"
        assert parse_node_id("s:BR:27").code == "27"
        assert parse_node_id("s:BR:@norte").code == "@norte"
        assert parse_node_id("x:3462377").code == "3462377"

    @pytest.mark.parametrize(
        "forged",
        [
            "r:../x",
            "r:desconhecida",
            "c:br",
            "c:BRA",
            "s:BR:",
            "s:BR:a b",
            "s:br:27",
            "x:abc",
            "x:",
            "x:1234567890123",
            "",
            "c:BR\n",
            "c:BR " + "x" * 100,
            None,
            5,
            b"c:BR",
            ["c:BR"],
        ],
    )
    def test_anything_else_is_none(self, forged):
        assert parse_node_id(forged) is None


class TestNumbersFromTheDirectory:
    def test_coordinates_must_be_finite_numbers_in_range(self):
        assert valid_coordinates(-16.68, -49.25) is True
        assert valid_coordinates(0, 0) is True
        for lat, lon in (
            (999, 0),
            (0, 181),
            ("x", 1),
            (1, None),
            (True, 1),
            (float("nan"), 0),
            (float("inf"), 0),
            (-91, 0),
        ):
            assert valid_coordinates(lat, lon) is False

    def test_clicks_are_clamped_and_junk_is_zero(self):
        assert clamp_clicks(5) == 5 and clamp_clicks("12") == 12 and clamp_clicks(3.9) == 3
        assert clamp_clicks(10**12) == 10**9
        for junk in (-5, None, "x", True, float("nan"), float("inf"), [], {}):
            assert clamp_clicks(junk) == 0

    def test_a_search_term_is_two_to_sixty_four_characters_without_controls(self):
        assert search_term("  Goiás ") == "Goiás"
        for bad in ("a", "", "x" * 65, "a\nb", "a\x00b", None, 5):
            assert search_term(bad) is None

    def test_distance_between_two_known_points(self):
        assert 168 < distance_km(-16.68, -49.25, -15.78, -47.93) < 178  # Goiânia–Brasília
        assert distance_km(1, 1, 1, 1) == pytest.approx(0)


def test_a_giant_decimal_click_count_is_zero_not_an_exception():
    from empiremuse.rules.radio_geo import clamp_clicks

    assert clamp_clicks("9" * 5000) == 0
    assert clamp_clicks("123") == 123
