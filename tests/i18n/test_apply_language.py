"""``apply_language`` (item 21, §7): depois da troca de idioma o controller sai no idioma novo, e um player com falha não impede os demais."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.i18n import I18n
from tests.i18n.test_loader import EN_BOT, EN_COMMANDS, PT_BOT, PT_COMMANDS, write

pytestmark = pytest.mark.critical

GID = 111


@pytest.fixture
def i18n(tmp_path):
    root = tmp_path / "locales"
    write(root, "en-US", EN_COMMANDS, EN_BOT)
    write(root, "pt-BR", PT_COMMANDS, PT_BOT)
    value = I18n(root)
    value.load()
    return value


def bot_with_player(i18n, *, preferred="en-US", fails=False, guild=True):
    player = SimpleNamespace(
        tr=i18n.for_locale("pt-BR"), invoke_np=AsyncMock(side_effect=RuntimeError("falha") if fails else None)
    )
    server = SimpleNamespace(id=GID, preferred_locale=preferred) if guild else None
    bot = SimpleNamespace(
        get_guild=lambda gid: server if gid == GID else None, music=SimpleNamespace(players={GID: player})
    )
    return bot, player


def attach(i18n, language, *bots):
    class Servers:
        async def settings(self, gid):
            return {"language": language}

    i18n.pool = SimpleNamespace(servers=Servers(), get_all_bots=lambda: list(bots))


@pytest.mark.asyncio
async def test_every_bot_gets_the_new_translator_and_redraws(i18n):
    first, first_player = bot_with_player(i18n)
    second, second_player = bot_with_player(i18n)
    attach(i18n, "en-US", first, second)

    await i18n.apply_language(GID)

    assert first_player.tr.locale == second_player.tr.locale == "en-US"
    first_player.invoke_np.assert_awaited_once_with(force=True)
    second_player.invoke_np.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
async def test_a_player_that_fails_does_not_stop_the_others(i18n, caplog):
    broken, broken_player = bot_with_player(i18n, fails=True)
    healthy, healthy_player = bot_with_player(i18n)
    attach(i18n, "en-US", broken, healthy)

    with caplog.at_level("WARNING"):
        await i18n.apply_language(GID)

    assert healthy_player.tr.locale == "en-US"
    healthy_player.invoke_np.assert_awaited_once_with(force=True)
    assert "falha" not in caplog.text  # só o tipo da exceção vai ao log


@pytest.mark.asyncio
async def test_bots_without_the_guild_or_a_player_are_skipped(i18n):
    outside, outside_player = bot_with_player(i18n, guild=False)
    idle = SimpleNamespace(
        get_guild=lambda gid: SimpleNamespace(id=GID, preferred_locale="en-US"), music=SimpleNamespace(players={})
    )
    attach(i18n, "en-US", outside, idle)

    await i18n.apply_language(GID)

    assert outside_player.tr.locale == "pt-BR" and outside_player.invoke_np.await_count == 0


@pytest.mark.asyncio
async def test_without_a_pool_it_does_nothing(i18n):
    i18n.pool = None

    await i18n.apply_language(GID)
