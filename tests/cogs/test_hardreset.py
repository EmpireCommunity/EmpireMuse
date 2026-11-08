"""``hardreset`` (só dono, só prefixo): quem não é dono é recusado; o premium sobrevive; todo o resto some (motor local, nunca o banco de produção)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from disnake.ext import commands

from empiremuse.cogs import hardreset as hardreset_cog
from empiremuse.i18n import get_i18n
from empiremuse.utils.db import LocalDatabase
from empiremuse.utils.music import session_store

pytestmark = pytest.mark.critical

BOT_ID = 4242
OWNER = 1000


def build(tmp_path, monkeypatch, *, owner=True):
    monkeypatch.setenv("EMPIREMUSE_HOME", str(tmp_path))
    db = LocalDatabase(str(tmp_path / "db"))
    bot = SimpleNamespace(user=SimpleNamespace(id=BOT_ID))
    manager = SimpleNamespace(known_radios=lambda: [], stop_radios=AsyncMock())
    pool = SimpleNamespace(
        database=db,
        local_database=db,
        get_all_bots=lambda: [bot],
        i18n=get_i18n(),
        radio=SimpleNamespace(manager=manager, forget_all=MagicMock()),
        servers=SimpleNamespace(forget_all=MagicMock()),
        user_prefix_cache={1: "x"},
        guild_prefix_cache={2: "y"},
    )
    bot.pool = pool
    bot.is_owner = AsyncMock(return_value=owner)
    bot.can_run = AsyncMock(return_value=True)  # os checks globais do bot
    pool.servers.main_bot = lambda: bot
    return SimpleNamespace(bot=bot, pool=pool, db=db, cog=hardreset_cog.HardReset(bot))


async def seed(w):
    db = w.db
    await db.update_data(500, {"active": 1, "granted_by": "1", "rev": 0}, db_name="premium", collection="global")
    await db.update_data("r1", {"rev": 0}, db_name="radios", collection="global")
    await db.update_data(500, {"rev": 0}, db_name="servers", collection="global")
    await db.update_data(7, {"prefix": "!"}, db_name="users", collection="global")
    await db.update_data(500, {"autoplay": True}, db_name="guilds", collection=str(BOT_ID))
    await db.update_data(7, {"tracks": []}, db_name="users", collection="scrobbles")
    await db.append_events([{"_id": "e1", "ts": 1}], db_name="usage_events", collection="global", expire_after=None)
    folder = session_store.sessions_dir(BOT_ID)
    folder.mkdir(parents=True)
    (folder / "500.json").write_text("{}")


def all_tables(db):
    names = ["global", "global_events", "scrobbles", str(BOT_ID)]
    return {f"{n}.{t}" for n in names for t in db._connect[n].list_collection_names()}


@pytest.mark.asyncio
async def test_a_non_owner_is_refused_and_nothing_is_deleted(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, owner=False)
    await seed(w)
    ctx = SimpleNamespace(
        bot=w.bot, author=SimpleNamespace(id=5), guild=None, send=AsyncMock(), command=w.cog.hardreset, cog=w.cog
    )

    with pytest.raises(commands.NotOwner):
        await w.cog.hardreset.can_run(ctx)

    assert "global.radios" in all_tables(w.db)


@pytest.mark.asyncio
async def test_premium_survives_and_everything_else_is_gone(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch)
    await seed(w)
    view = hardreset_cog.ConfirmHardReset(w.bot, get_i18n().for_locale("pt-BR"), OWNER)
    interaction = SimpleNamespace(
        author=SimpleNamespace(id=OWNER),
        response=SimpleNamespace(edit_message=AsyncMock()),
        edit_original_message=AsyncMock(),
    )

    await view.confirmed(interaction)

    assert all_tables(w.db) == {"global.premium"}
    kept = await w.db.find_doc(500, db_name="premium", collection="global")
    assert kept["active"] == 1 and kept["granted_by"] == "1"
    assert not session_store.sessions_dir(BOT_ID).exists()
    assert w.pool.user_prefix_cache == {} and w.pool.guild_prefix_cache == {}
    w.pool.servers.forget_all.assert_called_once()
    w.pool.radio.forget_all.assert_called_once()
    assert "premium" in interaction.edit_original_message.await_args.kwargs["content"].lower()


@pytest.mark.asyncio
async def test_a_reserve_bot_stays_silent_instead_of_raising_an_unexpected_check_failure(tmp_path, monkeypatch):
    """ERR-189CEF/ERR-F7D32E: o reserva recusava com CheckFailure comum, que o tratador registrava como erro inesperado."""
    from empiremuse.utils.music.errors import PoolException

    w = build(tmp_path, monkeypatch)
    w.pool.servers.main_bot = lambda: SimpleNamespace()  # outro bot é o principal
    with pytest.raises(PoolException):
        await w.cog.cog_check(SimpleNamespace())
    w.pool.servers.main_bot = lambda: w.bot
    assert await w.cog.cog_check(SimpleNamespace()) is True


def test_hard_reset_also_forgets_the_pause_all_held_in_memory() -> None:
    """CodeRabbit #199: os documentos somem no hardreset, mas a pausa geral ficava na memória até reiniciar."""
    from empiremuse.utils.radio import RadioHub
    from empiremuse.utils.radio.control import RadioControl

    control = RadioControl(SimpleNamespace())
    control._paused = True
    hub = SimpleNamespace(
        resolved=SimpleNamespace(_memory={}),
        health=SimpleNamespace(_until={}),
        _notices={},
        control=control,
    )

    RadioHub.forget_all(hub)

    assert control.paused_all is False
