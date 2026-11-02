"""Os comandos de prefixo e seus aliases (contrato-nomes-radios §7): a tabela fixada com literais e nenhum nome ou alias repetido entre comandos."""

import importlib
import inspect
import pkgutil
from collections import Counter

import pytest
from disnake.ext import commands

import empiremuse.cogs as cogs_package

pytestmark = pytest.mark.critical

# nome: aliases. Só os comandos públicos de prefixo da tabela; os ocultos de dono e de manutenção não mudam e ficam de fora.
PUBLIC_ALIASES = {
    "addposition": {"adp", "addpos"},
    "autoplay": {"ap"},
    "back": {"b", "voltar"},
    "changeskin": {"skin", "skins"},
    "clear": {"limpar"},
    "commandlog": {"cmdlog", "cl"},
    "controller": {"ctl", "controle"},
    "customskin": {"cskin", "cs", "setskin"},
    "adddj": {"adj"},
    "favmanager": {"favs", "favoritos", "itg", "spl"},
    "invite": {"convidar"},
    "lastfm": {"lfm"},
    "listenalong": {"la"},
    "loop": {"repetir"},
    "move": {"mv", "mover"},
    "nightcore": {"nc"},
    "nodeinfo": {"ll", "llservers"},
    "nowplaying": {"np", "tocando"},
    "pause": {"pausar"},
    "play": {"p", "tocar"},
    "playersettings": {"ps", "settings"},
    "playeruptime": {"puptime"},
    "queue": {"q", "fila"},
    "readd": {"rdd", "readicionar"},
    "remove": {"r", "remover"},
    "restrictmode": {"rstc", "restrito"},
    "resume": {"unpause", "retomar"},
    "reverse": {"rv", "inverter"},
    "richpresence": {"rpc"},
    "rotate": {"rt", "rotacionar"},
    "savequeue": {"sq", "salvarfila"},
    "search": {"sc", "buscar"},
    "seek": {"sk"},
    "setprefix": {"prefix", "mudarprefixo"},
    "setuserprefix": {"myprefix", "uprefix", "meuprefixo"},
    "setvoicestatus": {"vcstatus", "voicestatus", "statusvoz"},
    "shuffle": {"sf", "misturar"},
    "skip": {"n", "s", "next", "pular"},
    "stageregion": {"regiaopalco"},
    "stop": {"leave", "parar"},
    "summon": {"spawn", "smn"},
    "volume": {"vol", "v"},
}
GROUPS = {
    "jukebox": ({"jb"}, {"setup": {"configurar"}, "reset": {"redefinir"}, "thread": {"conversa"}}),
    "radio": (
        {"rd", "247"},
        {
            "setup": {"configurar"},
            "on": {"ligar"},
            "off": {"desligar"},
            "status": set(),
            "reset": {"redefinir"},
            "premium": set(),
            "bot": set(),
            "pauseall": {"pausartodas"},
            "resumeall": {"retomartodas"},
        },
    ),
}


def top_level_commands():
    found = []
    for module_info in pkgutil.iter_modules(cogs_package.__path__):
        module = importlib.import_module(f"empiremuse.cogs.{module_info.name}")
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, commands.Cog) and cls.__module__ == module.__name__:
                found += [
                    c
                    for c in getattr(cls, "__cog_commands__", [])
                    if isinstance(c, commands.Command) and c.parent is None
                ]
    return found


@pytest.fixture(scope="module")
def registry():
    return {c.name: c for c in top_level_commands()}


def test_no_name_or_alias_is_registered_twice_at_the_top_level():
    """Um alias repetido derruba o cog que carrega por último (``CommandRegistrationError``)."""
    seen = Counter(word for command in top_level_commands() for word in (command.name, *command.aliases))

    assert [word for word, count in seen.items() if count > 1] == []


@pytest.mark.parametrize("name", sorted(PUBLIC_ALIASES))
def test_each_public_command_has_exactly_the_aliases_of_the_table(registry, name):
    assert name in registry and not registry[name].hidden, name
    assert set(registry[name].aliases) == PUBLIC_ALIASES[name], name


@pytest.mark.parametrize("group", sorted(GROUPS))
def test_the_jukebox_and_radio_groups_and_their_subcommands(registry, group):
    aliases, subs = GROUPS[group]

    assert set(registry[group].aliases) == aliases
    assert {c.name: set(c.aliases) for c in registry[group].commands if group == "jukebox" or not c.hidden} | {
        c.name: set(c.aliases) for c in registry[group].commands
    } == {name: wanted for name, wanted in subs.items()}


def test_the_247_and_nonstop_commands_are_gone_as_commands(registry):
    assert "247" not in registry and "nonstop" not in registry
    assert "247" in registry["radio"].aliases


def test_the_owner_commands_of_the_radio_are_hidden_subcommands_not_top_level(registry):
    for name in ("premium", "bot", "pauseall", "resumeall"):
        assert name not in registry
        assert next(c for c in registry["radio"].commands if c.name == name).hidden
