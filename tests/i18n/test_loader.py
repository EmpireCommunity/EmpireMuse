"""Carregador, ``I18n``, provider e CLI de checagem (item 21, I-6, I-7, I-9, I-10): arquivo, cobertura, resolução e saída da CLI."""

import json
import os

import pytest

from empiremuse.i18n import check, loader
from empiremuse.i18n.service import I18n
from empiremuse.rules import i18n as rules

pytestmark = pytest.mark.critical

needs_fifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="os.mkfifo não existe nesta plataforma (Windows)")


def make_symlink(source, link, *, directory=False):
    """``os.symlink`` ou pula o teste: no Windows sem privilégio de symlink ele levanta ``OSError``."""
    try:
        os.symlink(source, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink indisponível nesta plataforma ({type(error).__name__})")


EN_COMMANDS = {
    "cmd.music._label": "Music",
    "cmd.play.description": "Play a song.",
    "cmd.play.opt.query.name": "query",
    "cmd.play.opt.query.description": "Song name or link.",
    "cmd.loop.opt.mode.choice.queue": "Queue",
}
PT_COMMANDS = {
    "cmd.music._label": "Música",
    "cmd.play.description": "Tocar uma música.",
    "cmd.play.opt.query.name": "nome",
    "cmd.play.opt.query.description": "Nome ou link da música.",
    "cmd.loop.opt.mode.choice.queue": "Fila",
}
EN_BOT = {"player.paused": "Paused by {user}.", "queue.count_one": "{count} song", "queue.count_other": "{count} songs"}
PT_BOT = {
    "player.paused": "Pausado por {user}.",
    "queue.count_one": "{count} música",
    "queue.count_many": "{count} de músicas",
    "queue.count_other": "{count} músicas",
}


def write(root, locale, commands=None, bot=None, panel=None):
    folder = root / locale
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in (("commands", commands), ("bot", bot), ("panel", panel)):
        if data is not None:
            (folder / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return folder


@pytest.fixture
def root(tmp_path):
    base = tmp_path / "locales"
    write(base, "en-US", EN_COMMANDS, EN_BOT)
    write(base, "pt-BR", PT_COMMANDS, PT_BOT)
    return base


def community(root, locale, keep: int):
    """Um locale com as ``keep`` primeiras das 7 chaves-base (as 5 de ``commands`` mais as 2 de ``bot``: ``player.paused`` e o plural)."""
    commands = {"cmd.music._label": "Musik", "cmd.play.description": "Spiele."}
    rest = {k: v for k, v in PT_COMMANDS.items() if k not in commands}
    bot = {
        "player.paused": "Pausiert von {user}.",
        "queue.count_one": "{count} Lied",
        "queue.count_other": "{count} Lieder",
    }
    pool = [*commands.items(), *rest.items(), *bot.items()]
    chosen = dict(pool[:keep])
    write(
        root,
        locale,
        {k: v for k, v in chosen.items() if k.startswith("cmd.")},
        {k: v for k, v in chosen.items() if not k.startswith("cmd.")},
    )


class TestLoading:
    def test_required_locales_load_and_are_enabled(self, root):
        i18n = I18n(root)

        i18n.load()

        assert i18n.enabled == {"en-US", "pt-BR"}
        assert i18n.for_locale("pt-BR")("player.paused", user="Ana") == "Pausado por Ana."
        assert i18n.for_locale("en-US")("player.paused", user="Ana") == "Paused by Ana."

    def test_loading_is_idempotent(self, root):
        i18n = I18n(root)

        assert i18n.load() is i18n.load()

    def test_a_broken_required_locale_fails_the_boot(self, root):
        (root / "pt-BR" / "bot.json").write_text("{not json", encoding="utf-8")

        with pytest.raises(loader.I18nError):
            I18n(root).load()

    def test_a_missing_reference_fails_the_boot(self, tmp_path):
        with pytest.raises(loader.I18nError):
            I18n(tmp_path / "nothing").load()

    def test_a_dropped_entry_in_a_required_locale_fails_the_boot(self, root):
        write(root, "pt-BR", PT_COMMANDS, {**PT_BOT, "player.paused": "Pausado por @everyone {user}"})

        with pytest.raises(loader.I18nError):
            I18n(root).load()

    def test_a_broken_community_locale_stays_off_and_never_stops_the_boot(self, root):
        write(root, "de", {"cmd.music._label": "Musik"}, {"player.paused": "{user"})
        (root / "de" / "commands.json").write_bytes(b"\xff\xff")

        i18n = I18n(root)
        i18n.load()

        assert "de" not in i18n.enabled

    def test_89_percent_stays_off_and_90_turns_on_without_code_changes(self, root):
        community(root, "de", 8)  # tudo
        community(root, "fr", 4)  # 4 de 7 chaves-base
        i18n = I18n(root)
        loaded = i18n.load()

        assert "de" in i18n.enabled and "fr" not in i18n.enabled
        assert loaded["de"].coverage == 1.0 and loaded["fr"].coverage < rules.MIN_COVERAGE

    def test_the_provider_serves_enabled_locales_only_and_never_command_names_or_values(self, root):
        community(root, "de", 8)
        community(root, "fr", 4)
        i18n = I18n(root)
        i18n.load()

        served = i18n.provider.get("cmd.play.opt.query.description")

        assert set(served) == {"pt-BR", "de"} and served["pt-BR"] == "Nome ou link da música."
        assert i18n.provider.get("cmd.play.opt.query.name")["pt-BR"] == "nome"
        assert i18n.provider.get("cmd.play.name") is None and i18n.provider.get("player.paused") is None
        assert i18n.provider.get("cmd.play.opt.query.value") is None

    def test_the_description_carries_the_group_legend_in_each_language(self, root):
        i18n = I18n(root)
        i18n.load()

        localized = i18n.localized("cmd.play.description")

        assert localized.string == "[🎶 Music] | Play a song."
        assert i18n.provider.get("cmd.play.description") == {"pt-BR": "[🎶 Música] | Tocar uma música."}


class TestFiles:
    def test_a_symlinked_file_is_ignored(self, root, tmp_path):
        outside = tmp_path / "outside.json"
        outside.write_text(json.dumps({"player.paused": "Pausado por {user}."}), encoding="utf-8")
        write(root, "de", {"cmd.music._label": "Musik"})
        make_symlink(outside, root / "de" / "bot.json")

        assert loader.read_catalog(root / "de" / "bot.json") is None

    @needs_fifo
    def test_a_fifo_does_not_hang_the_boot(self, root):
        write(root, "de", {"cmd.music._label": "Musik"})
        os.mkfifo(root / "de" / "bot.json")

        assert loader.read_catalog(root / "de" / "bot.json") is None

    def test_an_oversized_file_is_fatal_without_reading_all_of_it(self, root):
        (root / "de").mkdir()
        (root / "de" / "bot.json").write_bytes(b" " * (rules.MAX_FILE_BYTES + 10))

        raw = loader.read_catalog(root / "de" / "bot.json")

        assert raw is not None and len(raw) == rules.MAX_FILE_BYTES + 1

    @pytest.mark.parametrize("name", ["PT-br", "pt_BR", "xx", "pt-BR ", "en-us"])
    def test_a_directory_that_is_not_a_discord_locale_is_ignored(self, root, name):
        (root / name).mkdir()

        assert loader.locale_directories(root) == ["en-US", "pt-BR"]

    def test_a_symlinked_locale_directory_is_ignored(self, root, tmp_path):
        target = tmp_path / "elsewhere"
        write(target, "de", {"cmd.music._label": "Musik"})
        make_symlink(target / "de", root / "de", directory=True)

        assert "de" not in loader.locale_directories(root)

    def test_only_the_three_known_file_names_are_read(self, root):
        write(root, "de", {"cmd.music._label": "Musik"})
        (root / "de" / "extra.json").write_text("{}", encoding="utf-8")

        loaded = loader.load_catalogs(root)

        assert set(loaded["de"].entries) == {"cmd.music._label"}


class TestDisplayName:
    def test_only_the_allowlist_reaches_babel(self, root):
        i18n = I18n(root)

        assert i18n.display_name("pt-BR") == "Português (Brasil)"
        assert i18n.display_name("../../etc/passwd") == "../../etc/passwd"
        assert i18n.display_name("pt-br") == "pt-br"


class TestGuildResolution:
    @pytest.fixture
    def i18n(self, root):
        community(root, "de", 8)
        value = I18n(root)
        value.load()
        return value

    def settings(self, i18n, language):
        class Servers:
            async def settings(self, gid):
                return {"language": language}

        i18n.pool = type("Pool", (), {"servers": Servers()})()

    @pytest.mark.asyncio
    async def test_saved_wins_then_preferred_then_the_fallback(self, i18n):
        guild = type("Guild", (), {"id": 1, "preferred_locale": "pt-BR"})()
        self.settings(i18n, "de")
        assert (await i18n.for_guild(guild)).locale == "de"
        self.settings(i18n, None)
        assert (await i18n.for_guild(guild)).locale == "pt-BR"
        guild.preferred_locale = "ja"
        assert (await i18n.for_guild(guild)).locale == rules.FALLBACK_LOCALE

    @pytest.mark.asyncio
    async def test_a_disabled_saved_language_is_skipped_but_stays_saved(self, i18n):
        guild = type("Guild", (), {"id": 1, "preferred_locale": "pt-BR"})()
        self.settings(i18n, "fr")

        assert (await i18n.for_guild(guild)).locale == "pt-BR"
        assert (await i18n.pool.servers.settings(1))["language"] == "fr"

    @pytest.mark.asyncio
    async def test_dm_and_a_failing_store_use_the_preferred_locale(self, i18n):
        assert (await i18n.for_guild(None)).locale == rules.FALLBACK_LOCALE

        class Broken:
            async def settings(self, gid):
                raise RuntimeError("banco fora")

        i18n.pool = type("Pool", (), {"servers": Broken()})()
        guild = type("Guild", (), {"id": 1, "preferred_locale": "pt-BR"})()

        assert (await i18n.for_guild(guild)).locale == "pt-BR"


class TestCli:
    @pytest.fixture(autouse=True)
    def _empty_code(self, root):
        (root / "empty-code").mkdir()  # sem chaves usadas: o resultado só depende dos catálogos temporários

    def test_a_valid_tree_exits_zero(self, root, capsys):
        assert check.main(["--root", str(root), "--code", str(root / "empty-code")]) == 0
        assert "en-US: cobertura 100.0%" in capsys.readouterr().out

    def test_a_dropped_entry_and_a_fatal_fail_without_echoing_catalog_text(self, root, capsys):
        write(root, "pt-BR", PT_COMMANDS, {"player.paused": "Pausado por {user} @everyone"})
        write(root, "de", {"cmd.music._label": "Musik"}, None)
        (root / "de" / "bot.json").write_bytes(b"\xff")

        assert check.main(["--root", str(root), "--code", str(root / "empty-code")]) == 1
        err = capsys.readouterr().err
        assert "pt-BR: bot.player.paused descartada" in err and "de: arquivo bot recusado" in err
        assert "Pausado" not in err

    def test_a_required_locale_below_100_percent_fails(self, root, capsys):
        write(root, "pt-BR", PT_COMMANDS, {"player.paused": "Pausado por {user}."})

        assert check.main(["--root", str(root), "--code", str(root / "empty-code")]) == 1
        assert "pt-BR: cobertura abaixo de 100%" in capsys.readouterr().err

    def test_a_key_used_in_code_but_missing_from_en_us_is_reported(self, tmp_path):
        package = tmp_path / "pkg"
        package.mkdir()
        (package / "m.py").write_text(
            "def f(player):\n"
            "    player.tr('player.paused')\n"
            "    tr('queue.count', count=2)\n"
            "    localized('cmd.ghost.description')\n"
            "    other('x.y')\n",
            encoding="utf-8",
        )
        used = check.used_keys(package)

        assert set(used) == {"player.paused", "queue.count", "cmd.ghost.description"}
        assert check.missing_keys(used, {"player.paused": "x", "queue.count_other": "y"}) == ["cmd.ghost.description"]

    def test_the_real_catalogs_and_code_pass_the_check(self):
        assert check.main([]) == 0


class TestRequiredFiles:
    """Revisão (I-7): arquivo obrigatório ausente, ilegível ou trocado por link derruba a subida; faltar o diretório do en-US não dá traceback."""

    @pytest.mark.parametrize(
        "locale, name", [("en-US", "bot"), ("en-US", "commands"), ("pt-BR", "bot"), ("pt-BR", "commands")]
    )
    def test_a_missing_required_file_fails_the_boot(self, root, locale, name):
        (root / locale / f"{name}.json").unlink()

        with pytest.raises(loader.I18nError):
            I18n(root).load()

    def test_a_required_file_replaced_by_a_symlink_fails_the_boot(self, root, tmp_path):
        outside = tmp_path / "x.json"
        outside.write_text("{}", encoding="utf-8")
        (root / "pt-BR" / "bot.json").unlink()
        make_symlink(outside, root / "pt-BR" / "bot.json")

        with pytest.raises(loader.I18nError):
            I18n(root).load()

    @needs_fifo
    def test_a_required_file_replaced_by_a_fifo_fails_the_boot(self, root):
        (root / "pt-BR" / "bot.json").unlink()
        os.mkfifo(root / "pt-BR" / "bot.json")

        with pytest.raises(loader.I18nError):
            I18n(root).load()

    def test_the_panel_file_is_optional(self, root):
        assert not (root / "en-US" / "panel.json").exists()

        I18n(root).load()

    def test_a_tree_without_the_en_us_directory_is_an_error_not_a_traceback(self, tmp_path):
        base = tmp_path / "locales"
        write(base, "pt-BR", PT_COMMANDS, PT_BOT)

        loaded = loader.load_catalogs(base)

        assert "en-US" not in loaded and loader.required_errors(loaded)
        with pytest.raises(loader.I18nError):
            I18n(base).load()

    def test_the_log_and_the_cli_never_print_a_raw_community_key(self, root, capsys, caplog):
        write(root, "de", {"cmd.music._label": "Musik", "a.b\n::error::x": "y"})

        with caplog.at_level("WARNING"):
            I18n(root).load()
        check.main(["--root", str(root), "--code", str(root / "none")])

        assert "::error::x" not in caplog.text and "::error::x" not in capsys.readouterr().err


def test_a_disnake_locale_enum_resolves_like_its_value(root):
    import disnake

    i18n = I18n(root)
    i18n.load()

    assert i18n.resolve(None, disnake.Locale.pt_BR) == "pt-BR"
    assert i18n.resolve(disnake.Locale.en_US, None) == "en-US"
    assert i18n.resolve(None, disnake.Locale.ja) == rules.FALLBACK_LOCALE


def test_the_production_fallback_is_en_us_and_the_i18n_tests_run_with_it(root):
    assert rules.FALLBACK_LOCALE == "en-US" and rules.REFERENCE_LOCALE == "en-US"
    i18n = I18n(root)
    i18n.load()

    assert i18n.resolve(None, "ja") == "en-US"
    assert i18n.for_locale("ja").locale == "en-US" and i18n.for_locale(None).locale == "en-US"


class TestForGuildWithin:
    """O modal é a PRIMEIRA resposta da interação: o idioma não pode segurá-la (revisão da #175)."""

    @pytest.mark.asyncio
    async def test_a_fast_read_returns_the_saved_language(self, root):
        i18n = I18n(root)
        i18n.load()
        saved = type("Servers", (), {"settings": staticmethod(lambda gid: _done({"language": "pt-BR"}))})()
        i18n.pool = type("Pool", (), {"servers": saved})()
        guild = type("Guild", (), {"id": 1, "preferred_locale": "en-US"})()

        assert (await i18n.for_guild_within(guild)).locale == "pt-BR"

    @pytest.fixture
    def slow_store(self, root, monkeypatch):
        """Um store que só responde quando o teste libera, e um prazo que vence na hora (sem relógio)."""
        import asyncio

        i18n = I18n(root)
        i18n.load()
        release = asyncio.Event()
        calls = []

        async def slow(gid):
            calls.append(gid)
            await release.wait()
            return {"language": "pt-BR"}

        async def deadline_passed(awaitable, timeout):
            raise TimeoutError

        i18n.pool = type("Pool", (), {"servers": type("Servers", (), {"settings": staticmethod(slow)})()})()
        monkeypatch.setattr("empiremuse.i18n.service.asyncio.wait_for", deadline_passed)
        guild = type("Guild", (), {"id": 1, "preferred_locale": "en-US"})()
        return i18n, guild, release, calls

    @pytest.mark.asyncio
    async def test_a_slow_read_falls_back_to_the_discord_language_and_still_finishes_in_the_background(
        self, slow_store
    ):
        i18n, guild, release, calls = slow_store

        tr = await i18n.for_guild_within(guild)
        task = i18n._reads[1]

        assert tr.locale == "en-US" and not task.done()  # não esperou a leitura
        release.set()
        assert (await task).locale == "pt-BR"  # a leitura não foi cancelada: o cache esquenta para o próximo clique
        assert i18n._reads == {}

    @pytest.mark.asyncio
    async def test_repeated_clicks_on_a_slow_store_share_one_read_per_server(self, slow_store):
        i18n, guild, release, calls = slow_store

        for _ in range(5):
            await i18n.for_guild_within(guild)
        task = i18n._reads[1]
        release.set()
        await task

        assert calls == [1]

    @pytest.mark.asyncio
    async def test_a_background_read_that_fails_is_logged_with_the_exception_type_only(self, slow_store, caplog):
        i18n, guild, release, calls = slow_store

        async def failing(guild_):
            raise RuntimeError("segredo-do-banco")

        i18n.for_guild = failing
        with caplog.at_level("WARNING", logger="empiremuse.i18n.service"):
            await i18n.for_guild_within(guild)
            with pytest.raises(RuntimeError):
                await i18n._reads[1]

        assert "RuntimeError" in caplog.text and "segredo-do-banco" not in caplog.text

    @pytest.mark.asyncio
    async def test_close_cancels_the_pending_reads(self, slow_store):
        import asyncio

        i18n, guild, release, calls = slow_store
        await i18n.for_guild_within(guild)
        task = i18n._reads[1]

        i18n.close()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert i18n._reads == {}


async def _done(value):
    return value


class TestReviewRound173:
    """Achados do CodeRabbit na #173 sobre o código que entrou pela #174."""

    def test_a_locale_without_the_reference_is_reported_not_raised(self, tmp_path):
        base = tmp_path / "locales"
        write(base, "pt-BR", None, PT_BOT)

        loaded = loader.load_catalogs(base)

        assert "en-US" not in loaded
        assert loader.required_errors(loaded)

    def test_the_boot_with_only_one_non_reference_locale_fails_with_i18n_error_not_key_error(self, tmp_path):
        base = tmp_path / "locales"
        write(base, "pt-BR", PT_COMMANDS, PT_BOT)

        with pytest.raises(loader.I18nError):
            I18n(base).load()

    def test_root_without_code_does_not_compare_the_real_code_with_the_temporary_catalog(self, root, capsys):
        assert check.main(["--root", str(root)]) == 0
        assert "chave usada no código" not in capsys.readouterr().err

    def test_the_default_run_still_checks_the_real_code_against_the_real_catalog(self, capsys):
        assert check.main([]) == 0
        assert "pt-BR: cobertura 100.0%" in capsys.readouterr().out


class TestCommunityLocalesOnlyWarn:
    """Crowdin: só ``en-US`` e ``pt-BR`` derrubam o CI; os outros idiomas avisam e o valor vazio (chave não traduzida) nem isso."""

    @pytest.fixture(autouse=True)
    def _empty_code(self, root):
        (root / "empty-code").mkdir()

    def run(self, root):
        return check.main(["--root", str(root), "--code", str(root / "empty-code")])

    def test_an_empty_value_in_a_community_locale_is_ignored_silently_and_falls_back_to_en_us(self, root, capsys):
        write(
            root,
            "de",
            {"cmd.music._label": "Musik", "cmd.play.description": ""},
            {"player.paused": "", "queue.count_one": "{count} Lied", "queue.count_other": "{count} Lieder"},
        )

        assert self.run(root) == 0
        err = capsys.readouterr().err
        assert "descartada" not in err and "ERRO" not in err
        de = loader.load_catalogs(root)["de"]
        assert (
            "player.paused" not in de.entries and ("bot", "player.paused", "empty") in de.dropped
        )  # tratado como ausente
        i18n = I18n(root)
        i18n.load()
        assert i18n.for_locale("de")("player.paused", user="Ana") == "Paused by Ana."  # o en-US, nunca o pt-BR

    def test_an_empty_option_name_is_untranslated_not_an_invalid_name(self, root, capsys):
        write(
            root,
            "de",
            {"cmd.music._label": "Musik", "cmd.play.description": "Spiele.", "cmd.play.opt.query.name": ""},
            None,
        )

        assert self.run(root) == 0
        err = capsys.readouterr().err
        assert "descartada" not in err and "ERRO" not in err
        assert ("commands", "cmd.play.opt.query.name", "empty") in loader.load_catalogs(root)["de"].dropped

    def test_a_bad_non_empty_option_name_is_still_a_name_error(self, root, capsys):
        write(root, "de", {"cmd.music._label": "Musik", "cmd.play.opt.query.name": "Nome Inválido!"}, None)

        assert self.run(root) == 0
        assert "AVISO de: commands.cmd.play.opt.query.name descartada (name)" in capsys.readouterr().err

    def test_other_drops_and_a_refused_file_in_a_community_locale_are_warnings_with_exit_zero(self, root, capsys):
        write(root, "de", {"cmd.music._label": "Musik"}, {"player.paused": "Pausiert @everyone"})
        write(root, "fr", {"cmd.music._label": "Musique"}, None)
        (root / "fr" / "bot.json").write_bytes(b"\xff")

        assert self.run(root) == 0
        err = capsys.readouterr().err
        assert "AVISO de: bot.player.paused descartada" in err and "AVISO fr: arquivo bot recusado" in err
        assert "ERRO" not in err and "Pausiert" not in err

    def test_a_community_locale_below_the_minimum_coverage_only_warns(self, root, capsys):
        community(root, "de", 2)

        assert self.run(root) == 0
        assert "AVISO de: cobertura abaixo de 90%" in capsys.readouterr().err

    def test_a_required_locale_is_still_strict_even_for_an_empty_value(self, root, capsys):
        write(root, "pt-BR", PT_COMMANDS, {**PT_BOT, "player.paused": ""})

        assert self.run(root) == 1
        assert "ERRO pt-BR: bot.player.paused descartada" in capsys.readouterr().err
