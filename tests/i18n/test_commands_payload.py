"""Metadados dos comandos de aplicação (item 21, I-6): o payload de CADA locale ligado é válido e os nomes de comando não mudam.

Percorre todos os comandos de barra dos cogs, liga o provider e confere os limites do Discord, a unicidade dos nomes de opção, o nome de
opção em pt-BR igual ao de hoje, os autocompletes (citam a base) e o mascaramento do ``link`` (feito pela base, em todo idioma).
"""

import importlib
import inspect
import json
import pkgutil
from pathlib import Path

import disnake
import pytest
from disnake.ext import commands

from empiremuse.i18n import get_i18n
from empiremuse.logs import errors as log_errors
from empiremuse.rules import i18n as rules

pytestmark = pytest.mark.critical

GOLDEN = json.loads((Path(__file__).parent / "golden_option_names_pt_br.json").read_text(encoding="utf-8"))
SUBS = (disnake.OptionType.sub_command, disnake.OptionType.sub_command_group)


def all_commands():
    found = {}
    for package in ("empiremuse.cogs", "empiremuse.music_cog"):
        root = importlib.import_module(package)
        for module in pkgutil.iter_modules(root.__path__):
            loaded = importlib.import_module(f"{package}.{module.name}")
            for _, cls in inspect.getmembers(loaded, inspect.isclass):
                if issubclass(cls, commands.Cog) and cls.__module__.startswith("empiremuse"):
                    for command in getattr(cls, "__cog_app_commands__", []):
                        if isinstance(command, commands.InvokableSlashCommand):
                            found[command.qualified_name] = command
    return found


def walk(options, path):
    """``(caminho do comando, [opções], opção-pai)`` de cada nível: o comando, o grupo e cada subcomando."""
    leaves = [o for o in options if o.type in SUBS]
    plain = [o for o in options if o.type not in SUBS]
    if plain or not leaves:
        yield path, plain
    for sub in leaves:
        yield from walk(sub.options, f"{path}.{sub.name}")


@pytest.fixture(scope="module")
def provider():
    return get_i18n().provider


@pytest.fixture(scope="module")
def linked(provider):
    result = all_commands()
    for command in result.values():
        command.body.localize(provider)
    return result


def locales():
    return sorted(get_i18n().enabled - {rules.REFERENCE_LOCALE})


def levels(linked):
    for name, command in linked.items():
        yield from walk(command.body.options, name)


def test_every_slash_command_was_found(linked):
    assert len(linked) >= 40 and {"play", "queue", "language", "playlists", "setup"} <= set(linked)


def test_descriptions_fit_in_every_enabled_locale(linked):
    for name, command in linked.items():
        body = command.body
        assert 1 <= rules.utf16_length(body.description) <= 100, name
        for locale, text in (body.description_localizations.data or {}).items():
            assert locale in locales() and 1 <= rules.utf16_length(text) <= 100, (name, locale)


def test_command_names_are_never_localized(linked):
    for name, command in linked.items():
        assert not command.body.name_localizations.data, name


def test_option_bases_and_localized_names_are_valid_and_unique_per_level(linked):
    for path, options in levels(linked):
        effective = {locale: [] for locale in locales()}
        bases = []
        for option in options:
            assert rules.OPTION_BASE.fullmatch(option.name), (path, option.name)
            bases.append(option.name)
            localized = option.name_localizations.data or {}
            for locale in locales():
                name = localized.get(locale)
                if name is not None:
                    assert rules.option_name_ok(name), (path, option.name, locale)
                effective[locale].append(name or option.name)
        assert len(set(bases)) == len(bases), path
        for locale, names in effective.items():
            assert len(set(names)) == len(names), (path, locale)


def test_option_descriptions_and_choices_fit_in_every_locale(linked):
    for path, options in levels(linked):
        for option in options:
            assert 1 <= rules.utf16_length(option.description) <= 100, (path, option.name)
            for locale, text in (option.description_localizations.data or {}).items():
                assert 1 <= rules.utf16_length(text) <= 100, (path, option.name, locale)
            for choice in option.choices or []:
                assert 1 <= rules.utf16_length(choice.name) <= 100, (path, option.name, choice.value)
                for locale, text in (choice.name_localizations.data or {}).items():
                    assert 1 <= rules.utf16_length(text) <= 100, (path, option.name, choice.value, locale)


def test_pt_br_sees_the_same_option_names_as_today(linked):
    shown = {}
    for path, options in levels(linked):
        key = path.replace(" ", ".")
        if key == "language" or not options:
            continue
        shown[key] = sorted((o.name_localizations.data or {}).get("pt-BR", o.name) for o in options)

    for key, names in GOLDEN.items():
        if key in shown:
            assert shown[key] == names, key
    assert set(GOLDEN) - {"language"} <= set(shown) | {"language"}


def test_every_autocomplete_points_to_an_option_base_that_exists(linked):
    checked = 0
    for name, command in linked.items():
        bases = {option.name for _, options in walk(command.body.options, name) for option in options}
        for registered in command.autocompleters:
            assert registered in bases, (name, registered)
            checked += 1
    assert checked >= 10


def test_the_playlist_link_stays_masked_by_its_base_in_every_language(linked):
    bases = {option.name for option in dict(levels(linked))["playlists.importar"]}

    assert "link" in bases and "link" in log_errors._SECRET_PARAMS
    assert rules.SECRET_OPTION_NAMES == log_errors._SECRET_PARAMS
    assert log_errors._summarize("link", "https://x/y?token=1", reveal=True, budget=100) == log_errors.MASKED


def test_the_provider_never_serves_command_names_or_choice_values(provider):
    assert provider.get("cmd.play.name") is None and provider.get("cmd.play.opt.play_now.choice.value") is None
    assert provider.get("cmd.play.opt.play_now.choice.yes") == {"pt-BR": "Sim"}
    assert provider.get("cmd.play.opt.query.name") == {"pt-BR": "busca"}


def test_every_command_description_key_has_a_legend_group():
    reference = get_i18n().reference
    keys = [key for key in reference if rules.is_command_description(key)]

    assert keys and all(rules.command_group(key) in rules.GROUP_EMOJI for key in keys)
