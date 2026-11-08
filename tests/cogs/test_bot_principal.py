"""Bot principal × reservas (item 11, P2): papéis, comandos de barra só no principal (S5/S5b), prefixo sem corrida (S-V6),
servidor sem o principal (S6, S-V10), entrada de reserva sem tocar a configuração (S9) e o perfil da aplicação."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from empiremuse import paths
from empiremuse.cogs import misc
from empiremuse.cogs.misc import Misc
from empiremuse.rules.app_profile import DEFAULT_DESCRIPTION, DEFAULT_RESERVE_DESCRIPTION
from empiremuse.rules.bot_roles import NOTICE_WINDOW_SECONDS
from empiremuse.utils import app_profile as app_profile_utils
from empiremuse.utils import client
from empiremuse.utils.client import BotCore, BotPool

pytestmark = pytest.mark.critical

GID = 555
URL = "https://discord.com/oauth2/authorize?client_id=1"


# ---------------------------------------------------------------------- papéis (S-V9)


@pytest.mark.parametrize(
    ("identifier", "configured", "is_main", "is_reserve"),
    [("TOKEN", True, True, False), ("TOKEN_RESERVE_2", True, False, True), ("TOKEN_RESERVE_2", False, False, False)],
)
def test_roles_of_a_bot(identifier, configured, is_main, is_reserve):
    bot = SimpleNamespace(identifier=identifier, pool=SimpleNamespace(main_configured=configured))
    assert (BotCore.is_main.fget(bot), BotCore.is_reserve.fget(bot)) == (is_main, is_reserve)


@pytest.mark.parametrize(
    ("bots", "failed", "configured"),
    [
        (["TOKEN", "TOKEN_RESERVE_2"], {}, True),
        (["TOKEN_RESERVE_2"], {"TOKEN": "erro"}, True),
        (["TOKEN_RESERVE_2"], {}, False),
    ],
)
def test_a_main_that_failed_to_log_in_still_counts_as_configured(bots, failed, configured):
    pool = SimpleNamespace(bots=[SimpleNamespace(identifier=i) for i in bots], failed_bots=failed)
    assert BotPool.main_configured.fget(pool) is configured


def guild_with(channel):
    return SimpleNamespace(me=object(), get_channel_or_thread=lambda cid: channel)


def channel_where(can_send):
    return SimpleNamespace(permissions_for=lambda me: SimpleNamespace(send_messages=can_send))


@pytest.mark.parametrize(
    ("main", "serves"),
    [
        (None, False),
        (SimpleNamespace(bot_ready=False, is_closed=lambda: False, get_guild=lambda gid: guild_with(None)), False),
        (SimpleNamespace(bot_ready=True, is_closed=lambda: True, get_guild=lambda gid: guild_with(None)), False),
        (SimpleNamespace(bot_ready=True, is_closed=lambda: False, get_guild=lambda gid: None), False),
        (SimpleNamespace(bot_ready=True, is_closed=lambda: False, get_guild=lambda gid: guild_with(None)), False),
        (
            SimpleNamespace(
                bot_ready=True, is_closed=lambda: False, get_guild=lambda gid: guild_with(channel_where(False))
            ),
            False,
        ),
        (
            SimpleNamespace(
                bot_ready=True, is_closed=lambda: False, get_guild=lambda gid: guild_with(channel_where(True))
            ),
            True,
        ),
    ],
)
def test_main_serves_a_channel_only_when_it_is_there_ready_and_can_write(main, serves):
    assert BotPool.main_can_serve(SimpleNamespace(main_bot=main), GID, 7) is serves


# ---------------------------------------------------------------------- S5/S5b: comandos de barra só no principal


def reserve_bot(*, main_up=True, http_error=None):
    http = SimpleNamespace(
        bulk_upsert_global_commands=AsyncMock(side_effect=http_error),
        bulk_upsert_guild_commands=AsyncMock(side_effect=http_error),
    )
    return SimpleNamespace(
        identifier="TOKEN_RESERVE_2",
        user=SimpleNamespace(id=22),
        application_id=220,
        _test_guilds=[1, 2],
        http=http,
        loop=asyncio.get_running_loop(),
        pool=SimpleNamespace(wait_main_outcome=AsyncMock(return_value=main_up)),
    )


def sync_file():
    return paths.RUNTIME_DIR / ".app_commands_sync_data" / "22.json"


def calls_of(bot):
    return bot.http.bulk_upsert_global_commands.await_count + bot.http.bulk_upsert_guild_commands.await_count


@pytest.mark.asyncio
async def test_reserve_ends_with_no_global_or_test_commands_and_the_second_boot_calls_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("EMPIREMUSE_HOME", str(tmp_path))
    bot = reserve_bot()

    await BotCore._clear_remote_commands(bot)

    bot.http.bulk_upsert_global_commands.assert_awaited_once_with(220, [])
    assert [c.args for c in bot.http.bulk_upsert_guild_commands.await_args_list] == [(220, 1, []), (220, 2, [])]
    assert json.loads(sync_file().read_text()) == []

    await BotCore._clear_remote_commands(bot)  # 2º boot: idempotente pelo arquivo
    assert calls_of(bot) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["main_down", "http_error"])
async def test_reserve_keeps_its_commands_when_it_cannot_do_it_safely_and_retries_next_boot(
    tmp_path, monkeypatch, cause
):
    monkeypatch.setenv("EMPIREMUSE_HOME", str(tmp_path))
    bot = reserve_bot(main_up=cause != "main_down", http_error=OSError("falhou") if cause == "http_error" else None)

    await BotCore._clear_remote_commands(bot)

    assert not sync_file().exists()  # sem a marca, o próximo boot tenta de novo
    if cause == "main_down":
        assert calls_of(bot) == 0  # sem o principal logado a reserva não mexe em nada


@pytest.mark.asyncio
@pytest.mark.parametrize("is_reserve", [True, False])
async def test_only_a_reserve_schedules_the_cleanup_and_only_once(
    is_reserve,
):  # S5b: principal e exclusivo nunca são zerados
    clear = AsyncMock()
    bot = SimpleNamespace(
        wait_until_ready=AsyncMock(),
        is_reserve=is_reserve,
        _clear_commands_task=None,
        clear_remote_commands=clear,
        loop=asyncio.get_running_loop(),
        command_sync_flags=SimpleNamespace(sync_commands=False),  # o principal/exclusivo para aqui, sem sincronizar
    )

    await BotCore.sync_app_commands(bot)
    await BotCore.sync_app_commands(bot)  # a tarefa de fundo ainda não terminou: não agenda outra
    if bot._clear_commands_task is not None:
        await bot._clear_commands_task

    assert clear.await_count == (1 if is_reserve else 0)


# ---------------------------------------------------------------------- S-V6: o prefixo não tem corrida


def prefix_ctx():
    return SimpleNamespace(guild_id=GID, channel=SimpleNamespace(id=7), message=SimpleNamespace(id=99))


@pytest.mark.asyncio
@pytest.mark.parametrize(("taken", "expected"), [(True, True), (False, False)])
async def test_reserve_acts_on_a_prefix_command_only_if_the_main_took_it(monkeypatch, taken, expected):
    monkeypatch.setattr(client, "MAIN_PREFIX_WAIT", 0.0)
    ids = {f"{GID}-7-99"} if taken else set()
    bot = SimpleNamespace(loop=asyncio.get_running_loop(), pool=SimpleNamespace(message_ids=ids))
    assert await BotCore._main_took_prefix(bot, prefix_ctx()) is expected


@pytest.mark.parametrize(
    ("owner_id", "foreign_post", "owns"),
    [(22, False, True), (22, True, False), (99, False, False), (None, False, False)],
)
def test_reserve_owns_a_forum_post_only_when_it_created_it(owner_id, foreign_post, owns):
    bot = SimpleNamespace(user=SimpleNamespace(id=22), check_bot_forum_post=lambda channel, raise_error: foreign_post)
    assert BotCore._owns_forum_post(bot, SimpleNamespace(owner_id=owner_id)) is owns


# ---------------------------------------------------------------------- linha de comandos de barra


@pytest.mark.parametrize(
    ("is_reserve", "main_here", "url", "expected"),
    [
        (False, True, URL, None),  # o principal e os exclusivos não têm essa linha
        (True, True, URL, "<@1>"),  # servidor com o principal: aponta para ele
        (True, False, URL, URL),  # sem o principal: o convite montado pelo código
        (True, False, None, None),
    ],
)
def test_reserve_says_where_the_slash_commands_are(is_reserve, main_here, url, expected):
    main = SimpleNamespace(
        user=SimpleNamespace(id=1, mention="<@1>", name="Muse"), get_guild=lambda gid: object() if main_here else None
    )
    bot = SimpleNamespace(is_reserve=is_reserve, pool=SimpleNamespace(main_bot=main), main_invite_url=lambda: url)
    line = BotCore.slash_commands_line(bot, GID)
    assert (line is None) if expected is None else (expected in line)


# ---------------------------------------------------------------------- S6/S-V10: aviso de servidor sem o principal


def notice_world(
    *, identifier="TOKEN_RESERVE_2", others=("TOKEN_RESERVE_3",), main_here=False, url=URL, system=True, can_send=True
):
    send = AsyncMock()
    channel = SimpleNamespace(send=send, permissions_for=lambda me: SimpleNamespace(send_messages=can_send))
    guild = SimpleNamespace(
        id=GID,
        me=object(),
        system_channel=channel if system else None,
        text_channels=[channel],  # existe outro canal com permissão: o aviso nunca cai para ele
    )
    here = {identifier, *others}
    reserves = [SimpleNamespace(identifier=i, is_reserve=True, get_guild=lambda gid: guild) for i in sorted(here)]
    main = SimpleNamespace(
        user=SimpleNamespace(name="@everyone [Muse](http://e.vil)"),
        identifier="TOKEN",
        is_reserve=False,
        get_guild=lambda gid: guild if main_here else None,
    )
    pool = SimpleNamespace(main_bot=main, bots=[main, *reserves], join_notices={})
    bot = SimpleNamespace(
        identifier=identifier, pool=pool, default_prefix="!", main_invite_url=lambda: url, is_reserve=True
    )
    cog = Misc.__new__(Misc)
    cog.bot = bot
    return cog, guild, send, pool


@pytest.mark.asyncio
async def test_the_lowest_reserve_warns_once_in_the_system_channel_with_a_fixed_text_and_no_mentions():
    cog, guild, send, pool = notice_world()

    await Misc.reserve_join_notice(cog, guild)
    await Misc.reserve_join_notice(cog, guild)  # de novo dentro da janela: nada

    send.assert_awaited_once()
    text, mentions = send.await_args.args[0], send.await_args.kwargs["allowed_mentions"]
    assert f"<{URL}>" in text and "@everyone" not in text and "](http" not in text
    assert (mentions.everyone, mentions.users, mentions.roles, mentions.replied_user) == (False, False, False, False)
    assert GID in pool.join_notices


@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize("radio_is_sender", [True, False])
async def test_radio_bots_neither_send_nor_prevent_the_music_reserve_join_notice(radio_is_sender):
    identifier = "TOKEN_RADIO_2" if radio_is_sender else "TOKEN_RESERVE_2"
    other = "TOKEN_RESERVE_2" if radio_is_sender else "TOKEN_RADIO_2"
    cog, guild, send, _ = notice_world(identifier=identifier, others=(other,))
    await Misc.reserve_join_notice(cog, guild)
    assert send.await_count == (0 if radio_is_sender else 1)


@pytest.mark.regression
@pytest.mark.asyncio
async def test_music_invites_exclude_radio_only_bots():
    def bot(bot_id, identifier, name):
        return SimpleNamespace(
            identifier=identifier,
            user=SimpleNamespace(id=bot_id, name=name),
            is_main=False,
            guilds=[],
            config={"INVITE_PERMISSIONS": 8},
            appinfo=SimpleNamespace(bot_public=True, flags=SimpleNamespace(gateway_message_content_limited=False)),
        )

    pool = SimpleNamespace(bots=[bot(2, "TOKEN_RADIO_2", "RADIOONLY"), bot(3, "TOKEN_RESERVE_3", "MUSIC")])
    cog = SimpleNamespace(bot=SimpleNamespace(pool=pool, get_color=lambda _: 0))
    inter = SimpleNamespace(
        guild_id=GID,
        guild=SimpleNamespace(me=object(), members=[]),
        author=SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=False)),
        send=AsyncMock(),
    )
    await Misc.invite_button(cog, inter, is_command=True)
    text = "\n".join(embed.description for embed in inter.send.await_args.kwargs["embeds"])
    assert "MUSIC" in text and "RADIOONLY" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "world",
    [
        {
            "identifier": "TOKEN_RESERVE_3",
            "others": ("TOKEN_RESERVE_2",),
        },  # outra reserva, de maior identifier: quem avisa é a TOKEN_RESERVE_2
        {"main_here": True},  # o principal está no servidor
        {"system": False},  # sem canal de sistema não cai para outro canal
        {"can_send": False},
        {"url": None},  # principal não é público: sem convite
    ],
    ids=["not_the_notifier", "main_present", "no_system_channel", "no_send_permission", "no_invite"],
)
async def test_no_warning_when_it_is_not_this_reserves_turn_or_it_cannot_be_sent_safely(world):
    cog, guild, send, pool = notice_world(**world)
    await Misc.reserve_join_notice(cog, guild)
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_send_is_logged_and_not_repeated_inside_the_window(caplog):
    cog, guild, send, pool = notice_world()
    send.side_effect = disnake.Forbidden(MagicMock(status=403), "Missing Access")

    await Misc.reserve_join_notice(cog, guild)
    await Misc.reserve_join_notice(cog, guild)

    send.assert_awaited_once()
    assert pool.join_notices.get(GID) and NOTICE_WINDOW_SECONDS == 86400


# ---------------------------------------------------------------------- S9: reserva entra no servidor


@pytest.mark.asyncio
@pytest.mark.parametrize("banned", [True, False])
async def test_a_reserve_joining_only_warns_and_never_touches_data_and_a_banned_guild_gets_nothing(monkeypatch, banned):
    cog, guild, send, pool = notice_world()
    bans = SimpleNamespace(ensure_loaded=AsyncMock(), is_banned=lambda gid: banned)
    monkeypatch.setattr(misc, "bans_of", lambda owner: bans)
    cog.reserve_join_notice = AsyncMock()
    # sem pool.database nem servers: qualquer leitura ou gravação de configuração estouraria com AttributeError

    await Misc.guild_add(cog, guild)

    assert cog.reserve_join_notice.await_count == (0 if banned else 1)  # ban é checado antes do aviso
    assert not hasattr(pool, "database") and not hasattr(pool, "servers")


# ---------------------------------------------------------------------- perfil da aplicação


def profile_bot(*, reserve, main_up):
    main = SimpleNamespace(user=SimpleNamespace(name="Muse")) if main_up else None
    config = {
        "APP_DESCRIPTION": DEFAULT_DESCRIPTION,
        "APP_DESCRIPTION_RESERVE": DEFAULT_RESERVE_DESCRIPTION,
        "APP_TAGS": "música,music",
        "DEFAULT_PREFIX": "!",
        "PANEL_BASE_URL": "",
        "PANEL_ENABLED": False,
        "SUPPORT_SERVER": "https://discord.gg/x",
    }
    appinfo = SimpleNamespace(description="antiga", tags=["velha"], edit=AsyncMock())
    pool = SimpleNamespace(wait_main_outcome=AsyncMock(return_value=main_up), main_bot=main)
    return SimpleNamespace(identifier="TOKEN_RESERVE_2", appinfo=appinfo, config=config, is_reserve=reserve, pool=pool)


@pytest.mark.asyncio
async def test_reserve_profile_names_the_main_and_waits_for_it():
    bot = profile_bot(reserve=True, main_up=True)
    await app_profile_utils._sync(bot, 3)
    bot.pool.wait_main_outcome.assert_awaited_once()
    changes = bot.appinfo.edit.await_args.kwargs
    assert "Reserva do Muse" in changes["description"] and "reserva entra" not in changes["description"]


@pytest.mark.asyncio
async def test_reserve_without_the_main_leaves_the_discord_description_alone_but_still_sets_the_tags():
    bot = profile_bot(reserve=True, main_up=False)
    await app_profile_utils._sync(bot, 3)
    changes = bot.appinfo.edit.await_args.kwargs
    assert "description" not in changes and changes["tags"] == ["música", "music"]


@pytest.mark.asyncio
async def test_main_profile_uses_the_general_text_and_does_not_wait():
    bot = profile_bot(reserve=False, main_up=True)
    await app_profile_utils._sync(bot, 3)
    bot.pool.wait_main_outcome.assert_not_awaited()
    assert "uma reserva entra na sua call" in bot.appinfo.edit.await_args.kwargs["description"]
