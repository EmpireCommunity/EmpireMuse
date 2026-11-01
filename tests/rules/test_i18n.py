"""Regras puras da localização (item 21): validador de catálogo (I-1 a I-7), formatação segura, ``fit``, cobertura, resolução e ``Translator``."""

import json

import pytest

from empiremuse.rules import i18n as rules

pytestmark = pytest.mark.critical

REF = {
    "cmd.music._label": "Music",
    "cmd.play.description": "Play a song.",
    "cmd.loop.choice.queue": "Queue",
    "player.paused": "Paused by {user}.",
    "player.tip": "**Tip:** use `/play` here.\nSee [{name}](https://github.com/EmpireCommunity).",
    "queue.count_one": "{count} song",
    "queue.count_other": "{count} songs",
    "player.timer": "Ends <t:{timestamp}:R>",
}


def raw(data) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def validate(data, *, namespace="bot", reference=REF):
    return rules.validate_catalog("pt-BR", namespace, raw(data) if not isinstance(data, bytes) else data, reference)


def dropped(data, *, namespace="bot") -> dict[str, str]:
    return dict(validate(data, namespace=namespace).dropped)


class TestFatal:
    @pytest.mark.parametrize(
        "content",
        [
            b"\xff\xfe\x00",
            b"not json",
            b'{"player.paused": "a", "player.paused": "b"}',
            b'{"player.paused": 1}',
            b'["a"]',
            b'{"player.paused": {"x": "y"}}',
            b"[" * 100000,
            b"{" * 100000,
            b'{"a.b": "' + b"x" * (rules.MAX_FILE_BYTES + 1) + b'"}',
            b'{"player.paused": "\xed\xa0\x80"}',
        ],
        ids=[
            "utf8",
            "json",
            "duplicate",
            "number",
            "list",
            "nested",
            "recursion-list",
            "recursion-dict",
            "huge",
            "surrogate-bytes",
        ],
    )
    def test_each_fatal_case_disables_the_locale(self, content):
        report = rules.validate_catalog("pt-BR", "bot", content, REF)

        assert report.fatal and not report.entries

    def test_more_keys_than_twice_the_reference_is_fatal(self):
        data = {f"player.extra{n}": "x" for n in range(2 * len(REF) + 1)}

        assert validate(data).fatal == "chaves demais"

    def test_the_validator_never_raises(self, monkeypatch):
        monkeypatch.setattr(rules, "check_entry", lambda *a, **k: 1 / 0)

        assert rules.validate_catalog("pt-BR", "bot", b'{"player.paused": "x"}', REF).fatal == "validador"


class TestKeys:
    @pytest.mark.parametrize(
        "key", ["Player.Paused", "player", "player.paused ", "../x.y", "a." + "b" * 130, "a.b.c.d.e.f.g.h"]
    )
    def test_a_key_outside_the_pattern_is_dropped(self, key):
        assert dropped({key: "x"})[key] == rules.KEY_INVALID

    def test_a_key_absent_from_the_reference_is_dropped(self):
        assert dropped({"player.other": "x"})["player.other"] == rules.KEY_UNKNOWN

    def test_a_plural_category_missing_from_the_reference_uses_other(self):
        report = validate({"queue.count_many": "{count} músicas"})

        assert report.entries == {"queue.count_many": "{count} músicas"} and not report.dropped

    @pytest.mark.parametrize("value", ["", "   ", "\n"])
    def test_empty_or_blank_value_is_dropped(self, value):
        assert dropped({"player.tip": value})["player.tip"] == rules.EMPTY

    def test_commands_and_bot_keys_stay_in_their_own_file(self):
        assert dropped({"cmd.play.description": "x"}, namespace="bot") == {"cmd.play.description": rules.KEY_UNKNOWN}
        assert dropped({"player.paused": "x {user}"}, namespace="commands") == {"player.paused": rules.KEY_UNKNOWN}


class TestPlaceholders:
    @pytest.mark.parametrize(
        "text",
        [
            "{user.__class__}",
            "{user[0]}",
            "{user!r}",
            "{user:>999999999}",
            "{user:{y}}",
            "{}",
            "{0}",
            "{User}",
            "{user",
            "user}",
        ],
    )
    def test_unsafe_format_fields_are_dropped(self, text):
        assert dropped({"player.paused": text})["player.paused"] == rules.FORMAT

    def test_a_different_multiset_of_placeholders_is_dropped(self):
        assert dropped({"player.paused": "Pausado."})["player.paused"] == rules.PLACEHOLDERS
        assert dropped({"player.paused": "{user}{user}"})["player.paused"] == rules.PLACEHOLDERS
        assert dropped({"player.paused": "{other}"})["player.paused"] == rules.PLACEHOLDERS

    def test_escaped_braces_only_pass_when_the_reference_has_them(self):
        assert dropped({"player.paused": "{{track.url}} {user}"})["player.paused"] == rules.PLACEHOLDERS
        ref = {**REF, "player.paused": "Set {{x}} by {user}"}

        assert not validate({"player.paused": "Defina {{x}} por {user}"}, reference=ref).dropped


class TestMentionsLinksAndMarkdown:
    @pytest.mark.parametrize(
        "text",
        [
            "@everyone {user}",
            "＠everyone {user}",
            "@HERE {user}",
            "<@123> {user}",
            "<@&5> {user}",
            "<#9> {user}",
            "</play:1> {user}",
            "<id:browse> {user}",
            "<a:x:1> {user}",
        ],
    )
    def test_mentions_and_marks_outside_the_reference_are_dropped(self, text):
        assert dropped({"player.paused": text})["player.paused"] == rules.MARKS

    def test_the_reference_marks_are_kept(self):
        assert not validate({"player.timer": "Termina <t:{timestamp}:R>"}).dropped

    @pytest.mark.parametrize(
        "text",
        [
            "{user} https://evil.example/x",
            "{user} ｈｔｔｐｓ：／／evil.example",
            "{user} www.evil.example",
            "{user} discord.gg/abc",
            "{user} discord.com/invite/abc",
            "{user} dsc.gg/abc",
            "{user} xn--e1afmkfd.com",
            "{user} evil.gg/x",
            "{user} [x](y)",
        ],
    )
    def test_links_outside_the_reference_are_dropped(self, text):
        assert dropped({"player.paused": text})["player.paused"] == rules.LINKS

    def test_a_link_present_in_the_reference_is_kept(self):
        text = "**Dica:** use `/play` aqui.\nVeja [{name}](https://github.com/EmpireCommunity)."

        assert not validate({"player.tip": text}).dropped

    @pytest.mark.parametrize(
        "text",
        [
            "# **Dica:** use `/play` aqui.\nVeja [{name}](https://github.com/EmpireCommunity).",
            "**Dica:** use ||/play|| aqui.\nVeja [{name}](https://github.com/EmpireCommunity).",
            "**Dica:** use `/play aqui.\nVeja [{name}](https://github.com/EmpireCommunity).",
            "**Dica:** use `/play` aqui. Veja [{name}](https://github.com/EmpireCommunity).",
            "**Dica:** use `/play` aqui.\n> Veja [{name}](https://github.com/EmpireCommunity).",
        ],
    )
    def test_markdown_marks_must_match_the_reference(self, text):
        assert dropped({"player.tip": text})["player.tip"] == rules.MARKDOWN

    def test_a_newline_in_a_description_is_dropped(self):
        report = validate(
            {"cmd.play.description": "x\ny"}, namespace="commands", reference={"cmd.play.description": "a\nb"}
        )

        assert dict(report.dropped) == {"cmd.play.description": rules.NEWLINE}


class TestCharacters:
    @pytest.mark.parametrize(
        "char",
        [
            "\x00",
            "\x07",
            "\x7f",
            " ",
            " ",
            "‮",
            "‎",
            "؜",
            "⁦",
            "​",
            "⁠",
            "﻿",
            "­",
            "",
        ],
    )
    def test_control_bidi_and_invisible_characters_are_dropped(self, char):
        assert dropped({"player.paused": f"Pausado{char} {{user}}"})["player.paused"] == rules.CHARS

    def test_a_surrogate_is_dropped(self):
        data = b'{"player.paused": "a\\ud800 {user}"}'

        assert dict(validate(data).dropped)["player.paused"] == rules.CHARS

    def test_joiners_only_between_two_visible_characters(self):
        assert not validate({"player.paused": "👮‍♂️ {user}"}).dropped
        assert dropped({"player.paused": "‍Pausado {user}"})["player.paused"] == rules.CHARS
        assert dropped({"player.paused": "Pausado‍ {user}"})["player.paused"] == rules.CHARS

    def test_zalgo_beyond_four_combining_marks_is_dropped(self):
        assert dropped({"player.paused": "P" + "́" * 5 + " {user}"})["player.paused"] == rules.CHARS
        assert not validate({"player.paused": "P" + "́" * 4 + " {user}"}).dropped


class TestSizes:
    def test_the_ceiling_follows_the_reference_not_the_default(self):
        assert dropped({"player.paused": "x" * 200 + " {user}"})["player.paused"] == rules.SIZE
        assert not validate({"player.paused": "Pausado por {user}, agora."}).dropped

    def test_a_description_is_measured_with_the_group_prefix(self):
        ref = {"cmd.music._label": "Music", "cmd.play.description": "x" * 80}
        data = {"cmd.music._label": "Música muito longa", "cmd.play.description": "y" * 80}

        report = validate(data, namespace="commands", reference=ref)

        assert dict(report.dropped) == {"cmd.play.description": rules.SIZE}

    def test_emoji_outside_the_bmp_count_as_two_units(self):
        assert rules.utf16_length("a🎶") == 3
        ref = {"cmd.play.description": "x" * 50}

        assert not validate({"cmd.play.description": "🎶" * 49}, namespace="commands", reference=ref).dropped
        assert validate({"cmd.play.description": "🎶" * 51}, namespace="commands", reference=ref).dropped

    def test_the_reference_passes_the_domain_allowlist_and_the_basic_marks(self):
        ok = rules.validate_catalog("en-US", "bot", raw({"player.tip": "See https://github.com/EmpireCommunity"}), None)
        bad_link = rules.validate_catalog("en-US", "bot", raw({"player.tip": "See https://evil.example/x"}), None)
        bad_mention = rules.validate_catalog("en-US", "bot", raw({"player.tip": "Hi @everyone"}), None)

        assert not ok.dropped and bad_link.dropped and bad_mention.dropped


class TestOptionNames:
    REF = {
        "cmd.play.opt.query.name": "query",
        "cmd.play.opt.position.name": "position",
        "cmd.play.opt.query.description": "What to play.",
        "cmd.play.opt.mode.choice.queue": "Queue",
        "cmd.play.description": "Play.",
        "cmd.skip.opt.query.name": "query",
    }

    def run(self, data):
        return rules.validate_catalog("pt-BR", "commands", raw(data), self.REF)

    def test_valid_translated_names_are_accepted(self):
        report = self.run({"cmd.play.opt.query.name": "nome", "cmd.play.opt.position.name": "posição"})

        assert not report.dropped and len(report.entries) == 2

    @pytest.mark.parametrize("name", ["", "Nome", "n" * 33, "com espaço", "nome!", "a.b", "nome@", "/nome", "nome\n"])
    def test_an_invalid_option_name_is_dropped(self, name):
        assert dict(self.run({"cmd.play.opt.query.name": name}).dropped) == {"cmd.play.opt.query.name": rules.NAME}

    def test_allowed_punctuation_and_other_scripts(self):
        assert not self.run({"cmd.play.opt.query.name": "música-nova_x'y"}).dropped
        assert not self.run({"cmd.play.opt.query.name": "名前"}).dropped

    def test_a_repeated_name_inside_a_command_drops_both(self):
        report = self.run({"cmd.play.opt.query.name": "nome", "cmd.play.opt.position.name": "nome"})

        assert dict(report.dropped) == {
            "cmd.play.opt.query.name": rules.DUPLICATE,
            "cmd.play.opt.position.name": rules.DUPLICATE,
        }

    def test_a_name_equal_to_the_base_of_an_untranslated_option_reverts_the_whole_level(self):
        report = self.run({"cmd.play.opt.query.name": "position"})

        assert dict(report.dropped) == {"cmd.play.opt.query.name": rules.DUPLICATE} and not report.entries

    def test_any_collision_drops_every_localized_name_of_that_level_only(self):
        ref = {**self.REF, "cmd.play.opt.third.name": "third"}
        data = {
            "cmd.play.opt.query.name": "nome",
            "cmd.play.opt.position.name": "posição",
            "cmd.play.opt.third.name": "nome",
            "cmd.skip.opt.query.name": "nome",
        }

        report = rules.validate_catalog("pt-BR", "commands", raw(data), ref)

        assert set(dict(report.dropped)) == {
            "cmd.play.opt.query.name",
            "cmd.play.opt.position.name",
            "cmd.play.opt.third.name",
        }
        assert report.entries == {"cmd.skip.opt.query.name": "nome"}

    def test_each_command_or_subcommand_is_its_own_level(self):
        ref = {"cmd.queue.move.opt.position.name": "position", "cmd.queue.clear.opt.position.name": "position"}
        data = {"cmd.queue.move.opt.position.name": "posição", "cmd.queue.clear.opt.position.name": "posição"}

        assert not rules.validate_catalog("pt-BR", "commands", raw(data), ref).dropped

    def test_an_untranslated_level_with_unique_bases_is_untouched(self):
        assert not self.run({"cmd.skip.opt.query.name": "nome"}).dropped

    def test_the_name_length_counts_utf16_units(self):
        assert not self.run({"cmd.play.opt.query.name": "𠀀" * 16}).dropped
        assert dict(self.run({"cmd.play.opt.query.name": "𠀀" * 32}).dropped) == {"cmd.play.opt.query.name": rules.NAME}
        assert dict(self.run({"cmd.play.opt.query.name": "a" * 33}).dropped) == {"cmd.play.opt.query.name": rules.NAME}

    @pytest.mark.parametrize("name", ["token", "link", "passcode", "api_key", "secret"])
    def test_a_secret_looking_localized_name_is_dropped_when_the_base_is_not_secret(self, name):
        report = self.run({"cmd.play.opt.query.name": name})

        assert dict(report.dropped) == {"cmd.play.opt.query.name": rules.NAME}

    def test_secret_names_pass_only_on_a_secret_base(self):
        ref = {"cmd.import.opt.link.name": "link", "cmd.import.opt.query.name": "query"}

        assert not rules.validate_catalog("pt-BR", "commands", raw({"cmd.import.opt.link.name": "token"}), ref).dropped
        assert dict(
            rules.validate_catalog("pt-BR", "commands", raw({"cmd.import.opt.query.name": "link"}), ref).dropped
        ) == {"cmd.import.opt.query.name": rules.NAME}

    def test_the_secret_list_mirrors_the_log_masking(self):
        from empiremuse.logs import errors

        assert rules.SECRET_OPTION_NAMES == errors._SECRET_PARAMS

    def test_in_the_reference_the_name_is_the_base(self):
        report = rules.validate_catalog("en-US", "commands", raw({"cmd.play.opt.query.name": "search"}), None)

        assert report.dropped == (("cmd.play.opt.query.name", rules.NAME),)

    def test_the_same_name_in_two_commands_is_fine(self):
        assert not self.run({"cmd.play.opt.query.name": "nome", "cmd.skip.opt.query.name": "nome"}).dropped

    def test_the_reference_base_must_match_the_pattern(self):
        bad = rules.validate_catalog("en-US", "commands", raw({"cmd.play.opt.Query.name": "query"}), None)
        too_long = rules.validate_catalog("en-US", "commands", raw({f"cmd.play.opt.{'q' * 33}.name": "q"}), None)

        assert bad.dropped and too_long.dropped

    def test_the_helpers_split_the_key(self):
        assert rules.option_parts("cmd.queue.move.opt.position.name") == ("queue.move", "position")
        assert rules.option_parts("cmd.play.description") is None
        assert rules.is_option_name("cmd.play.opt.query.name") and not rules.is_option_name("cmd.play.name")
        assert rules.is_command_description("cmd.play.description")
        assert not rules.is_command_description("cmd.play.opt.query.description")


class TestSafeFormat:
    def test_values_come_out_literal(self):
        assert rules.safe_format("Hi {user}", {"user": "{user.__class__} {x}"}) == "Hi {user.__class__} {x}"
        assert rules.safe_format("{count} songs", {"count": 3}) == "3 songs"
        assert rules.safe_format("a {{b}} {x}", {"x": "y"}) == "a {b} y"

    @pytest.mark.parametrize("template", ["{x.y}", "{x[0]}", "{x!r}", "{x:>9}", "{}", "{0}", "{x"])
    def test_a_bad_template_raises(self, template):
        with pytest.raises(ValueError):
            rules.safe_format(template, {"x": "y"})

    def test_values_must_be_str_or_int(self):
        for value in (1.5, None, True, ["a"], object()):
            with pytest.raises(TypeError):
                rules.safe_format("{x}", {"x": value})

    def test_a_missing_value_raises(self):
        with pytest.raises(KeyError):
            rules.safe_format("{x}", {})


class TestFit:
    def test_short_text_is_untouched(self):
        assert rules.fit("abc", 10) == "abc"

    def test_long_text_is_cut_in_the_middle_within_the_limit(self):
        out = rules.fit("a" * 50 + "b" * 50, 21)

        assert out.startswith("a") and out.endswith("b") and "…" in out and rules.utf16_length(out) <= 21

    def test_it_never_splits_a_mention_or_custom_emoji(self):
        text = "x" * 20 + "<@123456789012345678>" + "y" * 20

        for limit in range(2, 60):
            out = rules.fit(text, limit)
            assert out.count("<") == out.count(">") and rules.utf16_length(out) <= limit

    def test_degenerate_limits(self):
        assert rules.fit("abc", 0) == "" and rules.fit("abc", 1) == "…"


class TestCoverageAndResolution:
    def test_plural_counts_one_with_every_category(self):
        entries = {"queue.count_one": "a", "queue.count_other": "b"}
        reference = {"queue.count_one": "x", "queue.count_other": "y"}

        assert rules.coverage(entries, reference) == 1.0
        assert rules.coverage({"queue.count_one": "a"}, reference) == 0.0
        assert rules.coverage(entries, reference, ("one", "many", "other")) == 0.0

    def test_89_percent_is_off_and_90_is_on(self):
        reference = {f"a.k{n}": "x" for n in range(100)}

        assert rules.coverage({f"a.k{n}": "x" for n in range(89)}, reference) < rules.MIN_COVERAGE
        assert rules.coverage({f"a.k{n}": "x" for n in range(90)}, reference) >= rules.MIN_COVERAGE

    ENABLED = frozenset({"en-US", "pt-BR", "es-419", "de"})

    @pytest.mark.parametrize(
        ("saved", "preferred", "expected"),
        [
            ("de", "pt-BR", "de"),
            (None, "de", "de"),
            ("fr", "de", "de"),
            (None, "es-ES", "es-419"),
            (None, "en-GB", "en-US"),
            (None, "zh-TW", "en-US"),
            (None, "ja", "en-US"),
            (None, None, "en-US"),
            ("pt-BR", "en-US", "pt-BR"),
        ],
    )
    def test_saved_then_preferred_then_sibling_then_fallback(self, saved, preferred, expected):
        assert rules.resolve_locale(saved, preferred, self.ENABLED, "en-US") == expected

    def test_a_disabled_saved_value_is_skipped_not_replaced(self):
        assert rules.resolve_locale("fr", "pt-BR", self.ENABLED, "en-US") == "pt-BR"

    def test_the_fallback_is_explicit(self):
        assert rules.resolve_locale(None, "ja", self.ENABLED, "pt-BR") == "pt-BR"


class TestTranslator:
    def make(self, **kw):
        misses = []
        t = rules.Translator(
            "pt-BR",
            {
                "player.paused": "Pausado por {user}.",
                "queue.count_one": "{count} música",
                "queue.count_other": "{count} músicas",
            },
            {
                "player.paused": "Paused by {user}.",
                "player.only_en": "Only {user}",
                "queue.count_one": "{count} song",
                "queue.count_other": "{count} songs",
            },
            on_missing=lambda locale, key: misses.append((locale, key)),
            **kw,
        )
        return t, misses

    def test_locale_then_reference_then_the_key(self):
        t, misses = self.make()

        assert t("player.paused", user="Ana") == "Pausado por Ana."
        assert t("player.only_en", user="Ana") == "Only Ana"
        assert t("no.such") == "no.such" and misses == [("pt-BR", "no.such")]

    def test_plural_picks_the_category_and_fills_count(self):
        t, _ = self.make()

        assert t("queue.count", count=1) == "1 música" and t("queue.count", count=5) == "5 músicas"

    def test_a_custom_plural_rule_with_other_as_the_last_resort(self):
        t, _ = self.make(plural=lambda n: "many")

        assert t("queue.count", count=7) == "7 músicas"

    def test_it_never_raises_on_bad_values_or_missing_fields(self):
        t, _ = self.make()

        assert t("player.paused") == "player.paused"
        assert t("player.paused", user=1.5) == "player.paused"

    def test_values_are_never_reformatted_by_the_translator(self):
        t, _ = self.make()

        assert t("player.paused", user="{track.url}") == "Pausado por {track.url}."

    def test_it_is_immutable(self):
        t, _ = self.make()

        with pytest.raises(AttributeError):
            t.locale = "en-US"
        with pytest.raises(AttributeError):
            t.other = 1
