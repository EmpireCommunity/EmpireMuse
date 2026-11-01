"""``/language`` (item 21, I-11): só com ``manage_guild``, só em servidor, valor exato ou ``auto``, cooldown de 10 s, resposta no idioma novo."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from disnake.ext import commands

from empiremuse.cogs import language as language_cog
from empiremuse.i18n import I18n
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.servers import ServerStore
from tests.persistence.test_servers_store import GID, MAIN_ID, SERVERS, Banco, make_bot, run
from tests.persistence.test_server_language import stored

pytestmark = pytest.mark.critical


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(
        language_cog, "LANGUAGE_COOLDOWN", commands.CooldownMapping.from_cooldown(1, 10, commands.BucketType.guild)
    )
    banco = Banco()
    banco.put(*SERVERS, GID, stored(language="pt-BR"))
    bot = make_bot(MAIN_ID, "TOKEN")
    bot.music = SimpleNamespace(players={})
    i18n = I18n()
    i18n.load()
    pool = SimpleNamespace(database=banco, get_all_bots=lambda: [bot], i18n=i18n)
    pool.servers = ServerStore(pool)
    i18n.pool = pool
    bot.pool = pool
    i18n.apply_language = AsyncMock()
    cog = language_cog.Language(bot)
    return SimpleNamespace(banco=banco, bot=bot, pool=pool, cog=cog, i18n=i18n)


def inter(world, *, manage=True, in_guild=True, preferred="en-US"):
    guild = SimpleNamespace(id=GID, preferred_locale=preferred) if in_guild else None
    author = SimpleNamespace(id=77, guild_permissions=SimpleNamespace(manage_guild=manage))
    return SimpleNamespace(
        guild=guild,
        guild_id=GID if in_guild else None,
        author=author,
        bot=world.bot,
        response=SimpleNamespace(send_message=AsyncMock()),
    )


def call(world, value, **kwargs):
    context = inter(world, **kwargs)
    run(language_cog.Language.language.callback(world.cog, context, value))
    return context


def saved(world):
    return world.banco.servers_doc()["settings"]["language"]


def test_a_manager_changes_the_language_and_gets_the_answer_in_the_new_one(world):
    context = call(world, "en-US")

    assert saved(world) == "en-US"
    message = context.response.send_message.await_args.args[0]
    assert message == "Language changed to **English (United States)**."
    assert context.response.send_message.await_args.kwargs == {"ephemeral": True}
    world.i18n.apply_language.assert_awaited_once_with(GID)


def test_auto_stores_none_and_answers_in_the_guild_language(world):
    context = call(world, "auto", preferred="pt-BR")

    assert saved(world) is None
    assert "Automático" in context.response.send_message.await_args.args[0]


@pytest.mark.parametrize("value", ["../x", "pt-br", "en-US ", "fr", "", "PT-BR", "pt-BR\n", "auto "])
def test_anything_that_is_not_an_exact_enabled_locale_is_refused_without_echo(world, value):
    with pytest.raises(GenericError) as error:
        call(world, value)

    assert saved(world) == "pt-BR"
    assert value.strip() == "" or value.strip() not in error.value.text
    world.i18n.apply_language.assert_not_awaited()


def test_without_manage_guild_nothing_is_written(world):
    with pytest.raises(GenericError):
        call(world, "en-US", manage=False)

    assert saved(world) == "pt-BR" and world.banco.saves == []


def test_in_a_dm_the_command_is_refused(world):
    with pytest.raises(GenericError):
        call(world, "en-US", in_guild=False)

    assert world.banco.saves == []


def test_the_cooldown_is_ten_seconds_per_guild(world):
    call(world, "en-US")

    with pytest.raises(GenericError) as error:
        call(world, "pt-BR")

    assert saved(world) == "en-US" and error.value.text.startswith("Wait ")


def test_a_store_failure_is_a_friendly_error_never_the_exception_text(world, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("segredo-do-banco")

    monkeypatch.setattr(world.pool.servers, "patch", broken)

    with pytest.raises(GenericError) as error:
        call(world, "en-US")

    assert "segredo-do-banco" not in error.value.text


def test_autocomplete_lists_auto_and_the_enabled_locales_up_to_25(world):
    context = inter(world)

    everything = run(language_cog.language_autocomplete(context, ""))
    filtered = run(language_cog.language_autocomplete(context, "port"))

    assert set(everything.values()) == {"auto", "en-US", "pt-BR"} and len(everything) <= 25
    assert set(filtered.values()) == {"pt-BR"}


def test_the_command_is_guild_only_and_hidden_without_manage_guild():
    body = language_cog.Language.language.body

    assert body.default_member_permissions.manage_guild is True
    assert body.contexts is not None and body.contexts.guild and not body.contexts.bot_dm
