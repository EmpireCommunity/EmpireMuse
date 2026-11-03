"""O ``help`` de prefixo lê o catálogo (contrato-nomes-radios §5, Help): nome, descrição e aliases vêm de ``cmd.<id>.*`` no idioma do
servidor, nunca do texto fixo do decorador. Todo comando público registrado tem as três chaves em ``en-US`` e em ``pt-BR``, e o ``help``
mostra os aliases, na lista e na página do comando."""

import importlib
import inspect
import pkgutil
from types import SimpleNamespace

import pytest
from disnake.ext import commands

from empiremuse.cogs import help_cog
from empiremuse.i18n import get_i18n

pytestmark = pytest.mark.critical

LOCALES = ("en-US", "pt-BR")
FIELDS = ("name", "description", "aliases")
PACKAGES = ("empiremuse.cogs", "empiremuse.music_cog", "empiremuse.utils.radio")


def public_commands():
    """Os comandos de prefixo que o ``help`` lista: os de topo não ocultos e, nos grupos, os subcomandos não ocultos."""
    found = {}
    for package in PACKAGES:
        root = importlib.import_module(package)
        for module_info in pkgutil.iter_modules(root.__path__):
            module = importlib.import_module(f"{package}.{module_info.name}")
            for _, cls in inspect.getmembers(module, inspect.isclass):
                if not (issubclass(cls, commands.Cog) and cls.__module__.startswith("empiremuse")):
                    continue
                for command in getattr(cls, "__cog_commands__", []):
                    if command.parent is not None or command.hidden:
                        continue
                    found[command.qualified_name] = command
                    for sub in getattr(command, "commands", ()):
                        if not sub.hidden:
                            found[sub.qualified_name] = sub
    return found


REGISTRY = public_commands()


def by_name(name):
    return REGISTRY[name]


def aliases_of(text):
    return {alias.strip() for alias in text.split(",") if alias.strip()}


class TestEveryPublicCommandIsInTheCatalog:
    @pytest.mark.parametrize("locale", LOCALES)
    @pytest.mark.parametrize("name", sorted(REGISTRY))
    def test_it_has_name_description_and_aliases(self, locale, name):
        entries = get_i18n().entries(locale)
        key = f"cmd.{help_cog.catalog_id(by_name(name))}"

        assert [field for field in FIELDS if f"{key}.{field}" not in entries] == [], key

    @pytest.mark.parametrize("name", sorted(REGISTRY))
    def test_the_catalog_aliases_are_the_registered_ones(self, name):
        command = by_name(name)
        key = f"cmd.{help_cog.catalog_id(command)}.aliases"

        for locale in LOCALES:
            assert aliases_of(get_i18n().entries(locale).get(key, "")) == set(command.aliases), (locale, key)

    def test_the_prefix_names_that_differ_from_the_catalog_id_all_exist(self):
        assert set(help_cog.PREFIX_CATALOG_IDS) <= set(REGISTRY)


# ---------------------------------------------------------------------------- o help em si


class Avatar:
    url = "https://cdn.example/avatar.png"

    def replace(self, **_):
        return self

    def __str__(self):
        return self.url


def fake_bot(*registered):
    owner = SimpleNamespace(id=9, display_avatar=Avatar())
    return SimpleNamespace(
        pool=SimpleNamespace(i18n=get_i18n()),
        user=SimpleNamespace(id=1, display_avatar=Avatar()),
        get_color=lambda member: 0,
        appinfo=SimpleNamespace(owner=owner, team=None),
        commands=set(registered),
        help_command=None,
        remove_command=lambda name: None,
    )


def ctx_in(locale, *registered, prefix="!"):
    bot = fake_bot(*registered)
    me = SimpleNamespace(id=1, name="Muse", display_name="Muse", mention="<@1>")
    guild = SimpleNamespace(id=100, preferred_locale=locale, me=me)
    return SimpleNamespace(
        bot=bot, guild=guild, me=me, prefix=prefix, invoked_with="help", author=SimpleNamespace(id=5, mention="<@5>")
    )


async def page_of(locale, command):
    ctx = ctx_in(locale)
    return (await help_cog.HelpCog(ctx.bot).get_cmd(ctx, [command])).description


class TestTheCommandPageReadsTheCatalog:
    @pytest.mark.asyncio
    async def test_name_description_and_aliases_in_the_servers_language(self):
        page = await page_of("pt-BR", by_name("play"))

        assert "Comando: !play" in page and "tocar" in page
        assert "Tocar música em um canal de voz." in page
        assert "Alternativas:" in page and "!p | !tocar" in page

    @pytest.mark.asyncio
    async def test_the_same_command_in_english(self):
        page = await page_of("en-US", by_name("play"))

        assert "Play music in a voice channel." in page
        assert "Tocar música" not in page
        assert "Command: !play" in page and "Aliases:" in page and "!p | !tocar" in page

    @pytest.mark.asyncio
    async def test_a_subcommand_alias_keeps_the_group_in_front(self):
        page = await page_of("pt-BR", by_name("jukebox setup"))

        assert "!jukebox configurar" in page
        assert "Criar ou escolher o canal dedicado da Jukebox" in page

    @pytest.mark.asyncio
    async def test_the_decorator_text_never_shows_when_the_catalog_has_the_command(self):
        """``adddj`` tem ``cmd.add_dj.*``: o texto é o do catálogo, mesmo que o decorador tenha outra descrição."""
        command = by_name("adddj")
        page = await page_of("en-US", command)

        assert "Add a member to the DJ list" in page
        assert command.description not in page

    @pytest.mark.asyncio
    async def test_a_command_outside_the_catalog_falls_back_to_the_decorator(self):
        async def nothing(ctx):
            return None

        stray = commands.Command(nothing, name="zzstray", description="Texto do decorador", aliases=["zs"])
        page = await page_of("pt-BR", stray)

        assert "Texto do decorador" in page and "!zs" in page


class TestTheOverviewListsEveryCommandWithItsAliases:
    @pytest.mark.asyncio
    async def test_each_public_command_appears_with_its_aliases(self):
        ctx = ctx_in("pt-BR", by_name("play"), by_name("pause"), by_name("nodeinfo"))
        embed, _categories, listing = await help_cog.HelpCog(ctx.bot).overview(ctx)

        assert "play (p, tocar)" in embed.description
        assert "pause (pausar)" in embed.description
        assert "nodeinfo (ll, llservers)" in embed.description
        assert [c.name for data in listing.values() for c in data["cmds"]] == ["nodeinfo", "pause", "play"]

    @pytest.mark.asyncio
    async def test_hidden_commands_stay_out_and_the_help_hint_uses_the_prefix(self):
        async def nothing(ctx):
            return None

        secret = commands.Command(nothing, name="zzsecret", hidden=True)
        ctx = ctx_in("en-US", by_name("play"), secret, prefix="?")
        ctx.bot.is_owner = None
        embed, _categories, _listing = await help_cog.HelpCog(ctx.bot).overview(ctx)

        assert "zzsecret" not in embed.description
        assert "`?help [command or alias]`" in embed.description
        assert "(1 command)" in embed.description and embed.author.name == "Help menu - Command list (prefix)"
