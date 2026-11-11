"""T-533b (A1 a A8): os comandos de administração seguem só ``OWNER_IDS`` (modos ENV, DENY e FALLBACK).

A matriz roda sobre os portões reais: as checks dos comandos dos cogs, ``PanelView`` (o único portão do menu: ``opts_callback`` chama o
comando sem checks), ``OwnerOnlyView``, o modal de banimento de servidor, o botão do shell e as duas rotas do item 8. Sem rede, sem Discord e sem relógio.
"""

import importlib
import inspect
import logging
import pkgutil
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest
from disnake.ext import commands

import empiremuse.cogs
import empiremuse.music_cog
from empiremuse.cogs import error_handler, hardreset, legacy_cmds, ll_yt_oauth, player_resume, server_manager, user_bans
from empiremuse.cogs.error_handler import ErrorHandler
from empiremuse.music_cog.voice_status import VoiceStatusMixin
from empiremuse.rules.owner_gate import OwnerGateMode
from empiremuse.utils.client import BotCore, BotPool
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.owner_gate import is_admin_owner, log_owner_gate_boot
from empiremuse.utils.owner_panel import PanelCommand, PanelView, panel_command
from empiremuse.utils.owner_usage import NOT_OWNER_TEXT, BanReasonModal, OwnerOnlyView
from empiremuse.utils.user_ban_views import is_env_owner

pytestmark = pytest.mark.critical

ENV_OWNER, APP_OWNER, TEAM_ADMIN, STRANGER = (
    100000000000000001,
    100000000000000002,
    100000000000000003,
    100000000000000004,
)
USERS = {"env_owner": ENV_OWNER, "app_owner": APP_OWNER, "team_admin": TEAM_ADMIN, "stranger": STRANGER}
APP_OWNER_OBJ = SimpleNamespace(id=APP_OWNER)
SCENARIOS = {
    "ENV": (OwnerGateMode.ENV, {ENV_OWNER}, APP_OWNER_OBJ),
    "DENY": (OwnerGateMode.DENY, set(), APP_OWNER_OBJ),
    "FALLBACK": (OwnerGateMode.FALLBACK, set(), APP_OWNER_OBJ),
    "FALLBACK_WITHOUT_APPINFO": (OwnerGateMode.FALLBACK, set(), None),
}
ALLOWED = {("ENV", "env_owner"), ("FALLBACK", "app_owner")}  # a equipe da aplicação nunca passa, em modo nenhum

matrix = pytest.mark.parametrize(
    ("scenario", "who"), [(s, w) for s in SCENARIOS for w in USERS], ids=lambda v: v if isinstance(v, str) else None
)


def make_bot(scenario: str, **extra) -> SimpleNamespace:
    """Um bot só com o que o portão lê; ``is_owner`` (que deixaria a equipe passar) quebra o teste se for chamado."""
    mode, env_ids, app_owner = SCENARIOS[scenario]
    return SimpleNamespace(
        owner_gate=mode,
        env_owner_ids=set(env_ids),
        owner=app_owner,
        is_owner=AsyncMock(side_effect=AssertionError("o portão de administração nunca chama is_owner")),
        **extra,
    )


def passes(scenario: str, who: str) -> bool:
    return (scenario, who) in ALLOWED


# --- as checks dos comandos dos cogs ---------------------------------------------------------------------------------

ADMIN_COMMANDS = {
    "getlavaservers", "restartlavalink", "updatelavalink", "reloadconfig", "reloadskins", "reload", "update", "panel",
    "setguildprefix", "cleardm", "shell", "setbanner", "setavatar", "hardreset", "ytoauth", "servers", "saveplayers",
    "userbans",
}  # fmt: skip
COG_MODULES = (legacy_cmds, hardreset, ll_yt_oauth, server_manager, player_resume, user_bans)


def cog_commands(module):
    for _, cls in inspect.getmembers(module, inspect.isclass):
        if cls.__module__ == module.__name__ and issubclass(cls, commands.Cog):
            yield from getattr(cls, "__cog_commands__", [])


def admin_commands() -> dict:
    found = {}
    for module in COG_MODULES:
        for command in cog_commands(module):
            if any("admin_owner_only" in check.__qualname__ for check in command.checks):
                found[command.name] = command
    return found


def test_every_administration_command_of_the_table_carries_the_new_check():
    assert set(admin_commands()) == ADMIN_COMMANDS


@matrix
@pytest.mark.asyncio
async def test_a1_command_checks_pass_only_for_the_allowed_user(scenario, who):
    ctx = SimpleNamespace(bot=make_bot(scenario), author=SimpleNamespace(id=USERS[who]))
    outcomes = set()

    for command in admin_commands().values():
        for check in command.checks:
            try:
                outcomes.add(await check(ctx))
            except commands.NotOwner:
                outcomes.add("NotOwner")

    assert outcomes == ({True} if passes(scenario, who) else {"NotOwner"})


# --- PanelView: o único portão do menu -------------------------------------------------------------------------------


def make_panel(scenario: str):
    spy = AsyncMock(return_value="feito")

    async def dummy(ctx):  # um comando do painel de verdade (``PanelCommand``)
        return await spy(ctx)

    command = panel_command(name="dummy", emoji="🔧")(dummy)
    assert isinstance(command, PanelCommand)
    bot = make_bot(scenario, commands=[command], get_command=lambda name: command)
    return PanelView(bot), spy


def panel_interaction(user_id: int):
    interaction = MagicMock()
    interaction.user = SimpleNamespace(id=user_id)
    interaction.data = SimpleNamespace(values=["dummy"])
    interaction.send = AsyncMock()
    interaction.response.is_done.return_value = False
    interaction.response.edit_message = AsyncMock()
    interaction.message.embeds = [SimpleNamespace(description="")]
    return interaction


@matrix
@pytest.mark.asyncio
async def test_a1_panel_view_gates_the_menu_and_opts_callback_runs_only_after_it(scenario, who):
    view, spy = make_panel(scenario)
    select = view.children[0]
    interaction = panel_interaction(USERS[who])

    await view._scheduled_task(select, interaction)  # o caminho do disnake: ``interaction_check`` e só então o callback

    assert (spy.await_count == 1) is passes(scenario, who)
    assert interaction.send.await_count == (0 if passes(scenario, who) else 1)


# --- OwnerOnlyView, modal e botão do shell ---------------------------------------------------------------------------


@matrix
@pytest.mark.asyncio
async def test_a1_owner_only_view_and_modal_refuse_everyone_else_and_a_different_invoker(scenario, who):
    bot = make_bot(scenario)
    user = SimpleNamespace(id=USERS[who])
    view = OwnerOnlyView(bot, USERS[who])
    other = OwnerOnlyView(bot, STRANGER + 1)

    async def run_view(v):
        interaction = SimpleNamespace(author=user, response=SimpleNamespace(send_message=AsyncMock()))
        return await v.interaction_check(interaction)

    assert await run_view(view) is passes(scenario, who)
    assert await run_view(other) is False  # outro invoker, mesmo sendo dono

    async def run_modal(invoker_id):
        modal = BanReasonModal(bot, invoker_id, 500, "Servidor", None)
        interaction = SimpleNamespace(
            author=user, text_values={}, response=SimpleNamespace(send_message=AsyncMock()), guild=None
        )
        try:
            await modal.callback(interaction)
        except Exception:  # passou do portão e seguiu para a confirmação, que este teste não monta
            pass
        sent = interaction.response.send_message
        return "recusou" if sent.await_args_list and sent.await_args_list[0].args == (NOT_OWNER_TEXT,) else "passou"

    allowed = passes(scenario, who)
    assert await run_modal(USERS[who]) == ("passou" if allowed else "recusou")
    assert await run_modal(STRANGER + 1) == "recusou"


@matrix
@pytest.mark.asyncio
async def test_a1_the_shell_close_button_obeys_the_gate(scenario, who):
    cog = SimpleNamespace(bot=make_bot(scenario))
    edit = AsyncMock()
    inter = SimpleNamespace(
        data=SimpleNamespace(custom_id="close_shell_result"),
        author=SimpleNamespace(id=USERS[who]),
        send=AsyncMock(),
        response=SimpleNamespace(edit_message=edit),
    )

    await legacy_cmds.Owner.close_shell_result(cog, inter)

    assert (edit.await_count == 1) is passes(scenario, who) and inter.send.await_count == (
        0 if passes(scenario, who) else 1
    )


@matrix
@pytest.mark.asyncio
async def test_a1_the_update_body_check_refuses_the_same_users(scenario, who):
    lock = MagicMock()
    lock.locked.return_value = True  # quem passa do portão esbarra na atualização em andamento (outro texto)
    cog = SimpleNamespace(bot=make_bot(scenario, pool=SimpleNamespace(update_lock=lock)))
    cog._in_progress_error = legacy_cmds.Owner._in_progress_error
    ctx = SimpleNamespace(author=SimpleNamespace(id=USERS[who]))

    with pytest.raises(GenericError) as caught:
        await legacy_cmds.Owner.update.callback(cog, ctx)

    assert ("em andamento" in caught.value.text) is passes(scenario, who)


# --- A2 a A4: os modos, no portão direto ------------------------------------------------------------------------------


@matrix
def test_a2_a3_a4_is_admin_owner_never_lets_the_team_in(scenario, who):
    bot = make_bot(scenario)

    assert is_admin_owner(bot, SimpleNamespace(id=USERS[who])) is passes(scenario, who)
    bot.is_owner.assert_not_called()  # ``super().is_owner`` (dono ou equipe da aplicação) nunca é a fonte


def test_a_bot_without_the_gate_state_fails_closed():
    assert is_admin_owner(SimpleNamespace(env_owner_ids={ENV_OWNER}), SimpleNamespace(id=ENV_OWNER)) is False


@pytest.mark.parametrize(
    ("raw", "mode"),
    [
        ("100000000000000001", OwnerGateMode.ENV),
        ("1,2", OwnerGateMode.DENY),
        ("abc", OwnerGateMode.DENY),
        ("", OwnerGateMode.FALLBACK),
    ],
)
def test_the_real_bot_resolves_its_mode_from_owner_ids_and_keeps_is_owner_unchanged(raw, mode):
    bot = BotCore(
        command_prefix=commands.when_mentioned,
        intents=disnake.Intents.none(),
        pool=SimpleNamespace(config={"OWNER_IDS": raw}, loop=None),
        identifier="teste",
    )

    assert bot.owner_gate is mode
    if mode is OwnerGateMode.ENV:
        assert bot.env_owner_ids == {100000000000000001}


@pytest.mark.asyncio
async def test_the_real_bot_is_owner_still_accepts_owner_ids_for_the_limit_bypasses():
    bot = BotCore(
        command_prefix=commands.when_mentioned,
        intents=disnake.Intents.none(),
        pool=SimpleNamespace(config={"OWNER_IDS": str(ENV_OWNER)}, loop=None),
        identifier="teste",
    )

    assert await bot.is_owner(SimpleNamespace(id=ENV_OWNER)) is True


# --- A5: boot ------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "level"),
    [
        ("1,2", logging.ERROR),
        ("abc", logging.ERROR),
        ("", logging.WARNING),
        (None, logging.WARNING),
        ("100000000000000001", None),
    ],
)
def test_a5_boot_logs_one_record_per_mode_and_never_the_value(raw, level, caplog):
    caplog.set_level(logging.DEBUG, logger="empiremuse.utils.owner_gate")

    log_owner_gate_boot({"OWNER_IDS": raw})

    records = [r for r in caplog.records if r.name == "empiremuse.utils.owner_gate"]
    assert [r.levelno for r in records] == ([] if level is None else [level])
    assert all(raw not in r.getMessage() for r in records if raw)


def test_a5_the_pool_logs_the_mode_once_however_many_bots_it_has(monkeypatch, caplog):
    monkeypatch.setenv("OWNER_IDS", "1,2")
    caplog.set_level(logging.DEBUG, logger="empiremuse.utils.owner_gate")

    BotPool()

    assert [r.levelno for r in caplog.records if r.name == "empiremuse.utils.owner_gate"] == [logging.ERROR]


# --- A6: item 8 -------------------------------------------------------------------------------------------------------------


@pytest.fixture
def handler(monkeypatch):
    h = ErrorHandler.__new__(ErrorHandler)
    h.bot = SimpleNamespace(
        pool=SimpleNamespace(i18n=MagicMock(for_guild=AsyncMock())), config={}, user=SimpleNamespace(mention="<@1>")
    )
    h.components = []
    monkeypatch.setattr(error_handler, "parse_error", lambda *a, **k: ("sem permissão", "", False, [], False))
    return h


@matrix
@pytest.mark.asyncio
async def test_a6_missing_permissions_reinvokes_only_for_the_allowed_user(handler, scenario, who):
    ctx = MagicMock()
    ctx.bot = make_bot(scenario)
    ctx.author = SimpleNamespace(id=USERS[who], bot=False)
    ctx.channel.permissions_for.return_value = SimpleNamespace(embed_links=False, manage_messages=False)
    ctx.reinvoke = AsyncMock()
    ctx.send = AsyncMock()
    del ctx.inter  # comando de prefixo
    ctx.store_message = None

    await handler.on_legacy_command_error(ctx, commands.MissingPermissions(["manage_guild"]))

    assert (ctx.reinvoke.await_count == 1) is passes(scenario, who)
    assert ctx.send.await_count == (0 if passes(scenario, who) else 1)  # os demais recebem o erro normal


@matrix
@pytest.mark.asyncio
async def test_a6_voice_status_keeps_manage_guild_for_everyone_but_the_allowed_user(scenario, who):
    guild = MagicMock()
    author = SimpleNamespace(id=USERS[who], guild_permissions=SimpleNamespace(manage_guild=False))
    guild.get_member = lambda _: author
    inter = SimpleNamespace(
        bot=make_bot(scenario, pool=SimpleNamespace(i18n=MagicMock(for_guild=AsyncMock(return_value=lambda key: key)))),
        guild=guild,
        guild_id=1,
        author=author,
        response=SimpleNamespace(defer=AsyncMock()),
    )
    refused = False

    try:
        await VoiceStatusMixin.set_voice_status.callback(self=VoiceStatusMixin(), inter=inter, template="")
    except GenericError:
        refused = True
    except Exception:  # passou do portão e seguiu para o resto do fluxo, que este teste não monta
        pass

    assert refused is (not passes(scenario, who))


# --- A7: nenhum portão esquecido -------------------------------------------------------------------------------------


def loaded_cog_commands():
    for package in (empiremuse.cogs, empiremuse.music_cog):
        for info in pkgutil.iter_modules(package.__path__, package.__name__ + "."):
            try:
                module = importlib.import_module(info.name)
            except Exception:  # um cog que não importa neste ambiente não carrega no bot também
                continue
            for _, cls in inspect.getmembers(module, inspect.isclass):
                if issubclass(cls, commands.Cog) and cls.__module__ == module.__name__:
                    for attr in ("__cog_commands__", "__cog_app_commands__"):
                        for command in getattr(cls, attr, []):
                            yield f"{cls.__name__}.{command.name}", command


def test_a7_no_loaded_command_uses_the_default_is_owner_check():
    seen = list(loaded_cog_commands())
    offenders = [
        name
        for name, cmd in seen
        if any("is_owner" in c.__qualname__ and "admin" not in c.__qualname__ for c in cmd.checks)
    ]

    assert len(seen) > 50  # a varredura achou os cogs de verdade
    assert offenders == []


# --- A8: os portões do E5 não mudam ---------------------------------------------------------------------------------------


@matrix
def test_a8_the_e5_gate_stays_strictly_env_only(scenario, who):
    bot = make_bot(scenario)

    assert is_env_owner(bot, USERS[who]) is (USERS[who] in bot.env_owner_ids)  # nem o dono da aplicação, em FALLBACK
