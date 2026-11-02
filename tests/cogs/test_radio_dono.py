"""Comandos de dono da rádio (item 8, R1, R-1): só quem está em ``OWNER_IDS`` pode; a equipe do app, o administrador do servidor e a lista vazia
não. Premium, revogação, "pausar todas" e os bots só-rádio."""

import ast
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.cogs import radio as radio_cog
from empiremuse.i18n import get_i18n
from empiremuse.rules.radio import check_radio, radio_id
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.radio import RadioHub
from tests.persistence.test_radio_store import FakeDb

pytestmark = pytest.mark.critical

OWNER, TEAM_MEMBER, ADMIN = 1000, 2000, 3000
GUILD = 777


def build(*, owner_ids=(OWNER,), is_main=True):
    db = FakeDb()
    pool = SimpleNamespace(
        database=db, i18n=get_i18n(), servers=SimpleNamespace(main_bot=lambda: bot if is_main else None)
    )
    pool.radio = RadioHub(pool)
    bot = SimpleNamespace(env_owner_ids=set(owner_ids), pool=pool, is_owner=AsyncMock(return_value=True))
    cog = radio_cog.Radio(bot)
    return SimpleNamespace(cog=cog, bot=bot, pool=pool, hub=pool.radio, db=db)


def ctx_of(user_id):
    guild = SimpleNamespace(id=GUILD, preferred_locale="pt-BR")
    return SimpleNamespace(author=SimpleNamespace(id=user_id), guild=guild, send=AsyncMock())


def said(ctx):
    return ctx.send.await_args.args[0]


class TestWhoMayUseTheOwnerCommands:
    @pytest.mark.asyncio
    async def test_only_the_env_owner_passes(self):
        w = build()

        assert await w.cog.owner_check(ctx_of(OWNER)) is True
        assert await w.cog.owner_check(ctx_of(TEAM_MEMBER)) is False
        assert await w.cog.owner_check(ctx_of(ADMIN)) is False

    @pytest.mark.asyncio
    async def test_the_app_team_is_refused_even_though_bot_is_owner_says_yes(self):
        """``BotCore.is_owner`` também aceita o dono e a equipe da aplicação; a R-1 proíbe esse caminho."""
        w = build()

        assert await w.bot.is_owner(ctx_of(TEAM_MEMBER).author) is True
        assert await w.cog.owner_check(ctx_of(TEAM_MEMBER)) is False

    @pytest.mark.asyncio
    async def test_an_empty_owner_list_means_nobody(self):
        w = build(owner_ids=())

        assert await w.cog.owner_check(ctx_of(OWNER)) is False

    @pytest.mark.asyncio
    async def test_a_reserve_bot_never_answers_the_owner_commands(self):
        w = build(is_main=False)

        assert await w.cog.owner_check(ctx_of(OWNER)) is False

    def test_the_cog_never_calls_is_owner_nor_the_decorator(self):
        tree = ast.parse(pathlib.Path(radio_cog.__file__).read_text(encoding="utf-8"))
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
        }

        assert "is_owner" not in names

    def test_the_owner_subcommands_are_hidden_and_the_public_ones_are_not(self):
        w = build()
        group = w.cog.radio_prefix
        by_name = {command.name: command for command in group.commands}

        assert {n for n, c in by_name.items() if c.hidden} == {"premium", "bot", "pauseall", "resumeall"}
        assert {"setup", "on", "off", "status", "reset"} <= set(by_name) and not group.hidden
        assert [c for c in w.cog.get_commands()] == [group]  # nenhum comando de topo além do grupo

    def test_the_prefix_group_and_its_aliases_follow_the_contract(self):
        w = build()
        group = w.cog.radio_prefix
        by_name = {command.name: command for command in group.commands}

        assert group.name == "radio" and set(group.aliases) == {"rd", "247"}
        assert set(by_name["pauseall"].aliases) == {"pausartodas"} and set(by_name["resumeall"].aliases) == {
            "retomartodas"
        }
        assert set(by_name["setup"].aliases) == {"configurar"} and set(by_name["reset"].aliases) == {"redefinir"}
        assert set(by_name["on"].aliases) == {"ligar"} and set(by_name["off"].aliases) == {"desligar"}

    @pytest.mark.asyncio
    async def test_a_server_manager_uses_the_public_subcommands_but_never_the_owner_ones(self):
        w = build()
        manager = ctx_of(ADMIN)
        manager.author.guild_permissions = SimpleNamespace(manage_guild=True)
        plain = ctx_of(ADMIN)
        plain.author.guild_permissions = SimpleNamespace(manage_guild=False)

        assert await w.cog.manager_check(manager) is True and await w.cog.manager_check(plain) is False
        assert await w.cog.owner_check(manager) is False  # gerenciar o servidor não abre premium, bot nem pause

    @pytest.mark.asyncio
    async def test_the_owner_passes_the_group_check_even_without_manage_guild(self):
        w = build()
        owner = ctx_of(OWNER)
        owner.author.guild_permissions = SimpleNamespace(manage_guild=False)

        assert await w.cog.group_check(owner) is True

    @pytest.mark.asyncio
    async def test_a_reserve_bot_answers_no_prefix_command(self):
        w = build(is_main=False)
        manager = ctx_of(ADMIN)
        manager.author.guild_permissions = SimpleNamespace(manage_guild=True)

        assert await w.cog.manager_check(manager) is False and await w.cog.group_check(ctx_of(OWNER)) is False


class TestPremiumCommand:
    @pytest.mark.asyncio
    async def test_grant_then_list_then_revoke(self):
        w, ctx = build(), ctx_of(OWNER)

        await w.cog.radio_premium.callback(w.cog, ctx, "conceder", str(GUILD))
        assert (await w.hub.premium.fresh(GUILD)) is not None and str(GUILD) in said(ctx)
        await w.cog.radio_premium.callback(w.cog, ctx, "listar")
        assert str(GUILD) in said(ctx) and "0/3" in said(ctx)
        await w.cog.radio_premium.callback(w.cog, ctx, "revogar", str(GUILD))
        assert await w.hub.premium.fresh(GUILD) is None

    @pytest.mark.asyncio
    async def test_revoking_turns_the_guilds_radios_off_and_says_how_many(self):
        w, ctx = build(), ctx_of(OWNER)
        await w.hub.premium.grant(GUILD, by=OWNER)
        for bot_id in (1, 2):
            radio = check_radio(
                {"_id": radio_id(bot_id, GUILD), "channel_id": "9", "kind": "custom", "source": {"playlist": "p"}}
            )
            await w.hub.store.create(radio)
            await w.hub.store.set_enabled(radio.id, True)

        await w.cog.radio_premium.callback(w.cog, ctx, "revogar", str(GUILD))

        assert not any(r.enabled for r in await w.hub.store.list_for_guild(GUILD))
        assert "2" in said(ctx)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("action", "guild"),
        [("", ""), ("conceder", ""), ("conceder", "abc"), ("conceder", "0"), ("x", "5"), ("conceder", "-3")],
    )
    async def test_bad_arguments_get_the_usage_and_change_nothing(self, action, guild):
        w, ctx = build(), ctx_of(OWNER)

        with pytest.raises(GenericError):
            await w.cog.radio_premium.callback(w.cog, ctx, action, guild)

        assert w.db.tables.get("premium", {}) == {}

    @pytest.mark.asyncio
    async def test_a_database_failure_is_a_friendly_error(self):
        w, ctx = build(), ctx_of(OWNER)
        w.db.fail = True

        with pytest.raises(GenericError):
            await w.cog.radio_premium.callback(w.cog, ctx, "conceder", str(GUILD))


class TestPauseAll:
    @pytest.mark.asyncio
    async def test_pause_and_resume_are_idempotent_and_remembered(self):
        w, ctx = build(), ctx_of(OWNER)

        await w.cog.radio_pause_all.callback(w.cog, ctx)
        first = said(ctx)
        await w.cog.radio_pause_all.callback(w.cog, ctx)
        assert w.hub.control.paused_all is True and said(ctx) != first
        await w.cog.radio_resume_all.callback(w.cog, ctx)
        assert w.hub.control.paused_all is False

    @pytest.mark.asyncio
    async def test_it_does_not_touch_enabled_nor_the_counter(self):
        w, ctx = build(), ctx_of(OWNER)
        await w.hub.premium.grant(GUILD, by=OWNER)
        radio = check_radio(
            {"_id": radio_id(1, GUILD), "channel_id": "9", "kind": "custom", "source": {"playlist": "p"}}
        )
        await w.hub.store.create(radio)
        await w.hub.store.set_enabled(radio.id, True)

        await w.cog.radio_pause_all.callback(w.cog, ctx)

        assert (await w.hub.store.get(radio.id)).enabled and (await w.hub.premium.fresh(GUILD)).active == 1


class TestRadioOnlyBots:
    @pytest.mark.asyncio
    async def test_the_owner_replies_never_ping_anyone(self):  # V181 L6
        w, ctx = build(), ctx_of(OWNER)

        await w.cog.radio_bot.callback(w.cog, ctx, "adicionar", "TOKEN_3")
        changed = ctx.send.await_args.kwargs["allowed_mentions"]
        await w.cog.radio_bot.callback(w.cog, ctx, "listar")
        listed = ctx.send.await_args.kwargs["allowed_mentions"]

        for mentions in (changed, listed):
            assert mentions.everyone is False and mentions.users is False and mentions.roles is False

    @pytest.mark.asyncio
    async def test_add_list_remove(self):
        w, ctx = build(), ctx_of(OWNER)

        await w.cog.radio_bot.callback(w.cog, ctx, "adicionar", "TOKEN_3")
        await w.cog.radio_bot.callback(w.cog, ctx, "listar")
        assert "TOKEN_3" in said(ctx)
        await w.cog.radio_bot.callback(w.cog, ctx, "remover", "TOKEN_3")
        assert w.hub.roles.radio_only == ()

    @pytest.mark.asyncio
    async def test_the_main_bot_is_refused(self):
        w, ctx = build(), ctx_of(OWNER)

        with pytest.raises(GenericError):
            await w.cog.radio_bot.callback(w.cog, ctx, "adicionar", "TOKEN")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("identifier", ["", "x" * 41, "tökén"])
    async def test_a_bad_identifier_gets_the_usage(self, identifier):
        w, ctx = build(), ctx_of(OWNER)

        with pytest.raises(GenericError):
            await w.cog.radio_bot.callback(w.cog, ctx, "adicionar", identifier)
