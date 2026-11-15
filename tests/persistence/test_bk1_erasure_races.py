"""Regressões BK-1: gravações e apagamento compartilham o lock do titular."""

import asyncio

import pytest

from tests.persistence.test_radio_favoritas_erasure import mundo as mundo, CUSTOM, SERVIDOR, yes, uuid
from tests.support.erasure_world import ALVO, existe

pytestmark = [pytest.mark.critical, pytest.mark.regression, pytest.mark.asyncio]


@pytest.mark.parametrize("kind,table", [("account", "radio_user"), ("custom", "radio_custom_favorites")])
async def test_erasure_waits_for_an_inflight_radio_save(mundo, kind, table):
    entered, release, erasing = asyncio.Event(), asyncio.Event(), asyncio.Event()
    database = mundo.pool.database
    save = database.save_doc_cas
    original_run = mundo.service._run

    async def paused_save(*args, **kwargs):
        entered.set()
        await release.wait()
        return await save(*args, **kwargs)

    async def observed_run(user):
        erasing.set()
        return await original_run(user)

    database.save_doc_cas = paused_save
    mundo.service._run = observed_run
    if kind == "account":
        operation = mundo.hub.user_favorites.add_station(ALVO, uuid(1))
        key = str(ALVO)
    else:
        operation = mundo.hub.custom_favorites.add(SERVIDOR, ALVO, CUSTOM, yes)
        key = f"{SERVIDOR}:{ALVO}"
    writer = asyncio.create_task(operation)
    waiter = asyncio.create_task(entered.wait())
    await asyncio.wait((writer, waiter), return_when=asyncio.FIRST_COMPLETED)
    if writer.done():
        await writer
    await waiter
    erasure = asyncio.create_task(mundo.service.erase_user(ALVO, source="slash"))
    # O evento corre depois da primeira suspensão do apagamento, sem relógio real.
    turn = asyncio.Event()
    asyncio.get_running_loop().call_soon(turn.set)
    await turn.wait()
    was_erasing = erasing.is_set()
    release.set()
    await asyncio.gather(writer, erasure)
    assert not was_erasing
    assert not await existe(mundo, "global", table, key)


async def test_background_scrobble_does_not_create_data_for_unlinked_members(mundo):
    from types import SimpleNamespace
    from empiremuse.cogs.lastfm import LastFmCog

    cog = LastFmCog.__new__(LastFmCog)
    cog.bot = SimpleNamespace(pool=mundo.pool)
    await cog.save_scrobble("track", SimpleNamespace(duration=200000), [SimpleNamespace(id=ALVO)])
    assert not await existe(mundo, "scrobbles", "users", ALVO, local=True)


async def test_lastfm_toggle_after_erasure_does_not_write_an_old_view(mundo):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from empiremuse.cogs.lastfm import LastFMView
    from empiremuse.utils.db import global_db_models

    bot = SimpleNamespace(pool=mundo.pool)
    bot.get_global_data = AsyncMock(return_value=global_db_models["users"].copy())
    bot.update_global_data = AsyncMock()
    view = LastFMView(SimpleNamespace(bot=bot), "old-key", scrobble=False)
    inter = SimpleNamespace(
        author=SimpleNamespace(id=ALVO), response=SimpleNamespace(defer=AsyncMock()), edit_original_message=AsyncMock()
    )
    await view.scrobble_enable(inter)
    bot.update_global_data.assert_not_awaited()
    assert view.session_key == ""
    await view.disconnect_account(inter)
    assert inter.response.defer.await_count == 2


async def test_global_budget_reserves_inflight_erasure_and_audits_duration(mundo, caplog):
    from empiremuse.rules.erasure import ERASE_GLOBAL_PER_HOUR
    from empiremuse.utils.erasure import ErasureRateLimited

    entered, release = asyncio.Event(), asyncio.Event()
    run = mundo.service._run
    mundo.service._global_runs = [mundo.relogio()] * (ERASE_GLOBAL_PER_HOUR - 1)

    async def paused_run(user):
        if user == ALVO:
            entered.set()
            await release.wait()
            mundo.relogio.avancar(0.125)
        return await run(user)

    mundo.service._run = paused_run
    first = asyncio.create_task(mundo.service.erase_user(ALVO, source="slash"))
    await entered.wait()
    try:
        with pytest.raises(ErasureRateLimited):
            await mundo.service.erase_user(ALVO + 1, source="slash")
    finally:
        release.set()
        await first
    assert "duration_ms=125" in caplog.text


async def test_controller_favorite_cannot_land_after_erasure(mundo, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from empiremuse.music_cog import controller_buttons, controller_embed
    from empiremuse.utils.db import global_db_models

    entered, release = asyncio.Event(), asyncio.Event()
    erasing = asyncio.Event()
    original_run = mundo.service._run

    async def observed_run(user):
        erasing.set()
        return await original_run(user)

    mundo.service._run = observed_run
    bot = SimpleNamespace(pool=mundo.pool, get_color=lambda *args: 0, dispatch=Mock(), default_prefix="!!")

    async def read(user, *, db_name):
        if db_name == "guilds":
            return {"prefix": "!!"}
        return await mundo.database.get_data(user, db_name=db_name, collection="global", default_model=global_db_models)

    async def write(user, data, *, db_name):
        entered.set()
        await release.wait()
        await mundo.database.update_data(user, data, db_name=db_name, collection="global")

    bot.get_global_data, bot.update_global_data = read, write
    bot.pool.add_fav_embed_cooldown = SimpleNamespace(
        get_bucket=lambda inter: SimpleNamespace(update_rate_limit=lambda: None)
    )
    inter = SimpleNamespace(
        author=SimpleNamespace(id=ALVO),
        user=SimpleNamespace(id=ALVO),
        guild=None,
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_message=AsyncMock(),
        edit_original_response=AsyncMock(),
        message=SimpleNamespace(
            embeds=[SimpleNamespace(author=SimpleNamespace(name=" Rock", url="https://example.com/rock"))]
        ),
    )
    inter.guild = SimpleNamespace(id=SERVIDOR, preferred_locale="pt-BR", me=SimpleNamespace())
    owner = SimpleNamespace(bot=bot, play_legacy=SimpleNamespace(name="play"))
    req = SimpleNamespace(
        interaction=inter, info={"name": "Rock", "url": "https://example.com/rock"}, select_type="track"
    )
    module = controller_buttons
    monkeypatch.setattr(module, "quota_for", AsyncMock(return_value=10))
    handler = module._pc_favorite_save
    writer = asyncio.create_task(handler(owner, req))
    await entered.wait()
    erasure = asyncio.create_task(mundo.service.erase_user(ALVO, source="slash"))
    turn = asyncio.Event()
    asyncio.get_running_loop().call_soon(turn.set)
    await turn.wait()
    was_erasing = erasing.is_set()
    release.set()
    await asyncio.gather(writer, erasure)
    assert not was_erasing
    assert not await existe(mundo, "global", "users", ALVO)


async def test_embed_reply_after_erasure_never_saves_the_prepared_document(mundo, monkeypatch):
    import disnake
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from empiremuse.music_cog import controller_embed
    from empiremuse.utils.db import global_db_models

    bot = SimpleNamespace(pool=mundo.pool, default_prefix="!!")

    async def read(user, *, db_name):
        if db_name == "guilds":
            return {"prefix": "!!"}
        return await mundo.database.get_data(user, db_name=db_name, collection="global", default_model=global_db_models)

    async def write(user, data, *, db_name):
        await mundo.database.update_data(user, data, db_name=db_name, collection="global")

    bot.get_global_data, bot.update_global_data = read, write
    bot.pool.add_fav_embed_cooldown = SimpleNamespace(
        get_bucket=lambda inter: SimpleNamespace(update_rate_limit=lambda: None)
    )
    embed = disnake.Embed().set_author(name=" Rock", url="https://example.com/rock")
    inter = SimpleNamespace(
        author=SimpleNamespace(id=ALVO, mention="<@123>"),
        guild_id=SERVIDOR,
        guild=SimpleNamespace(id=SERVIDOR, preferred_locale="pt-BR"),
        response=SimpleNamespace(defer=AsyncMock()),
        message=SimpleNamespace(embeds=[embed], flags=SimpleNamespace(ephemeral=True)),
        send=AsyncMock(),
    )
    owner = SimpleNamespace(
        bot=bot, play_legacy=SimpleNamespace(name="play"), fav_manager_legacy=SimpleNamespace(name="fav_manager")
    )
    req = SimpleNamespace(interaction=inter)
    monkeypatch.setattr(controller_embed, "quota_for", AsyncMock(return_value=10))
    assert await controller_embed._pc_fav_embed_save(owner, req) is False
    await mundo.service.erase_user(ALVO, source="slash")
    await controller_embed._pc_fav_embed_reply(owner, req)
    assert not await existe(mundo, "global", "users", ALVO)


@pytest.mark.parametrize("guard", [lambda: False, lambda: 1 / 0])
async def test_revoked_session_at_lock_never_marks_counts_or_erases(mundo, guard):
    from empiremuse.utils.erasure import ErasureSessionRevoked
    from tests.support.erasure_world import gravar_usuario

    await gravar_usuario(mundo, ALVO, custom_prefix="keep")
    entered = asyncio.Event()
    lock = mundo.pool.user_locks.lock(ALVO)
    async with lock:

        async def request():
            entered.set()
            return await mundo.service.erase_user(ALVO, source="panel", still_valid=guard)

        task = asyncio.create_task(request())
        await entered.wait()
    with pytest.raises(ErasureSessionRevoked):
        await task
    assert await existe(mundo, "global", "users", ALVO)
    assert not mundo.pool.erasure_tombstones._erased_at
    assert not mundo.service._global_runs and not mundo.service._tries and not mundo.service._tasks


async def test_owner_erasure_audits_only_the_public_grant_id(mundo, caplog):
    from empiremuse.utils.erasure_grants import OwnerGrants

    grants = OwnerGrants(clock=mundo.relogio)
    grant = grants.issue(42, ALVO)
    assert grants.redeem(grant.code, ALVO) == grant
    await mundo.service.erase_user(ALVO, source="owner", owner_id=42, grant_id=grant.grant_id)
    assert f"grant={grant.grant_id}" in caplog.text
    assert grant.code not in caplog.text
