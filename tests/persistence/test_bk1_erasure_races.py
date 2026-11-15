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
    bot = SimpleNamespace(pool=mundo.pool, get_color=lambda *args: 0, dispatch=Mock(), default_prefix="padrao!")

    async def read(user, *, db_name):
        if db_name == "guilds":  # o prefixo é o do SERVIDOR: lido por outro id (o do membro), a confirmação sai errada
            return {"prefix": "srv!" if user == SERVIDOR else "outro!"}
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
        guild_id=SERVIDOR,
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
    assert "srv!" in inter.edit_original_response.await_args.kwargs["embed"].description


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


@pytest.mark.parametrize(
    "guard,raises",
    [(lambda: False, False), (lambda: 1, False), (lambda: "yes", False), (lambda: None, False), (lambda: 1 / 0, True)],
)
async def test_revoked_session_at_lock_never_marks_counts_or_erases(mundo, guard, raises, caplog):
    from empiremuse.utils.erasure import ErasureSessionRevoked
    from tests.support.erasure_world import gravar_usuario

    await gravar_usuario(mundo, ALVO, custom_prefix="keep")
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    entered = asyncio.Event()
    locks = mundo.pool.user_locks

    @asynccontextmanager
    async def observed_lock(user):
        async with locks.lock(user):
            entered.set()
            yield

    mundo.pool.user_locks = SimpleNamespace(lock=observed_lock)
    async with locks.lock(ALVO):
        task = asyncio.create_task(mundo.service.erase_user(ALVO, source="panel", still_valid=guard))
        turn = asyncio.Event()
        asyncio.get_running_loop().call_soon(turn.set)
        await turn.wait()
        assert locks.lock(ALVO).locked()
        assert not entered.is_set() and not task.done()
    with pytest.raises(ErasureSessionRevoked):
        await task
    assert entered.is_set()
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == int(raises)
    if warnings:
        assert warnings[0].args == ("ZeroDivisionError",)
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


@pytest.mark.parametrize("action,erased", [("disconnect", False), ("disconnect", True), ("toggle", True)])
async def test_lastfm_real_post_wait_flow_uses_original_edit_after_defer(mundo, monkeypatch, action, erased):
    from copy import deepcopy
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from empiremuse.cogs.lastfm import LastFMView, LastFmCog
    from empiremuse.utils.db import global_db_models
    from empiremuse.i18n import get_i18n

    bot = SimpleNamespace(pool=mundo.pool, get_cog=lambda _: None, get_color=lambda: 0)
    bot.pool.i18n = get_i18n()
    doc = deepcopy(global_db_models["users"])
    bot.get_global_data = AsyncMock(side_effect=lambda *a, **kw: deepcopy(doc))
    bot.update_global_data = AsyncMock()
    inter = SimpleNamespace(
        bot=bot,
        author=SimpleNamespace(id=ALVO),
        guild=None,
        permissions=SimpleNamespace(embed_links=True),
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_message=AsyncMock(),
    )
    button = SimpleNamespace(
        author=inter.author,
        guild=None,
        response=SimpleNamespace(
            defer=AsyncMock(),
            is_done=Mock(return_value=True),
            edit_message=AsyncMock(side_effect=AssertionError("already responded")),
        ),
        edit_original_message=AsyncMock(),
    )

    async def wait(view):
        if action == "disconnect":
            await view.disconnect_account(button)
        else:
            doc["lastfm"].update(sessionkey="key", username="name")
            view.session_key = "key"
            await view.scrobble_enable(button)
        if erased:
            mundo.relogio.avancar(1)
            mundo.pool.erasure_tombstones.mark(ALVO)
        view.stop()

    monkeypatch.setattr(LastFMView, "wait", wait)
    cog = LastFmCog.__new__(LastFmCog)
    cog.bot = bot
    await cog.lastfm.callback(cog, inter)
    button.response.defer.assert_awaited_once()
    button.response.edit_message.assert_not_awaited()
    assert button.edit_original_message.await_count == (2 if action == "toggle" else 1)
    if erased:
        assert button.edit_original_message.await_args.kwargs["content"] == get_i18n().for_locale("pt-BR")(
            "legacy.lastfm.erased_while_linking"
        )
    else:
        assert "desvinculada" in button.edit_original_message.await_args.kwargs["embeds"][-1].footer.text


async def test_controller_scrobble_holds_user_lock_until_write_and_replies_after_release(mundo):
    from copy import deepcopy
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from empiremuse.music_cog.controller_buttons import _pc_lastfm
    from empiremuse.utils.db import global_db_models
    from empiremuse.i18n import get_i18n

    entered, release = asyncio.Event(), asyncio.Event()
    lock = mundo.pool.user_locks.lock(ALVO)
    doc = deepcopy(global_db_models["users"])
    doc["lastfm"].update(username="name", sessionkey="key", scrobble=False)
    bot = SimpleNamespace(pool=mundo.pool, get_color=lambda *_: 0)
    bot.pool.i18n = get_i18n()

    async def read(*args, **kwargs):
        assert lock.locked()
        return doc

    async def write(*args, **kwargs):
        assert lock.locked()
        entered.set()
        await release.wait()

    async def reply(**kwargs):
        assert not lock.locked()

    bot.get_global_data, bot.update_global_data = read, write
    inter = SimpleNamespace(
        author=SimpleNamespace(id=ALVO),
        guild=None,
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_message=AsyncMock(side_effect=reply),
    )
    task = asyncio.create_task(_pc_lastfm(SimpleNamespace(bot=bot), SimpleNamespace(cmd=None, interaction=inter)))
    await entered.wait()
    assert lock.locked()
    inter.edit_original_message.assert_not_awaited()
    release.set()
    await task
    inter.edit_original_message.assert_awaited_once()
    assert not doc["lastfm"]["scrobble"]  # a cópia lida não foi alterada
    assert mundo.pool.lastfm_sessions[ALVO]["scrobble"] is True


@pytest.mark.parametrize("source", ["track", "embed"])
async def test_controller_favorite_limit_and_error_reply_release_the_user_lock(mundo, monkeypatch, source):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import disnake
    from empiremuse.music_cog import controller_buttons, controller_embed

    lock = mundo.pool.user_locks.lock(ALVO)

    async def quota(*args):
        assert not lock.locked()
        return 1

    async def reply(*args, **kwargs):
        assert not lock.locked()

    bot = SimpleNamespace(
        pool=mundo.pool,
        get_color=lambda *_: 0,
        get_global_data=AsyncMock(return_value={"fav_links": {"Existing": "https://example.org"}}),
        update_global_data=AsyncMock(),
    )
    bot.pool.add_fav_embed_cooldown = SimpleNamespace(
        get_bucket=lambda _: SimpleNamespace(update_rate_limit=lambda: None)
    )
    inter = SimpleNamespace(
        author=SimpleNamespace(id=ALVO),
        guild=SimpleNamespace(me=None),
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_message=AsyncMock(side_effect=reply),
        message=SimpleNamespace(embeds=[disnake.Embed().set_author(name=" Track", url="https://example.com")]),
    )
    req = SimpleNamespace(interaction=inter, info={"name": "Track", "url": "https://example.com"}, select_type="track")
    module = controller_buttons if source == "track" else controller_embed
    monkeypatch.setattr(module, "quota_for", quota)
    handler = module._pc_favorite_save if source == "track" else module._pc_fav_embed_save
    assert await handler(SimpleNamespace(bot=bot), req) is True
    inter.edit_original_message.assert_awaited_once()
    bot.update_global_data.assert_not_awaited()


async def test_lastfm_toggle_refreshes_session_before_save_link(mundo):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from empiremuse.cogs.lastfm import LastFMView, LastFmCog
    from tests.persistence.test_erasure_hooks import _fluxo
    from tests.support.erasure_world import gravar_usuario

    await gravar_usuario(
        mundo, ALVO, lastfm_sessionkey="current", lastfm_username="current-user", lastfm_scrobble=False
    )
    cog, _, inter = _fluxo(mundo)
    save = cog.bot.update_global_data

    async def update(user, data, *, db_name):
        return await save(user, data=data, db_name=db_name)

    cog.bot.update_global_data = update
    view = LastFMView(SimpleNamespace(bot=cog.bot), "stale", scrobble=False)
    inter.response = SimpleNamespace(defer=AsyncMock())
    read_at = mundo.pool.erasure_tombstones.now()
    await view.scrobble_enable(inter)
    assert view.session_key == "current"
    await gravar_usuario(mundo, ALVO, lastfm_sessionkey="latest", lastfm_username="latest-user", lastfm_scrobble=True)
    assert await cog._save_link(inter, view, None, read_at)
    doc = await mundo.database.find_doc(ALVO, db_name="users", collection="global")
    assert doc["lastfm"]["sessionkey"] == "latest"
    assert doc["lastfm"]["username"] == "latest-user"


async def test_lastfm_auth_ack_single_poll_and_stop(mundo, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from empiremuse.utils.music import lastfm_view

    both_deferred, release = asyncio.Event(), asyncio.Event()
    deferred = 0

    async def defer(**kwargs):
        nonlocal deferred
        deferred += 1
        if deferred == 2:
            both_deferred.set()

    response = SimpleNamespace(defer=AsyncMock(side_effect=defer))

    async def get_token():
        await release.wait()
        return "token"

    get_token = AsyncMock(side_effect=get_token)

    bot = SimpleNamespace(last_fm=SimpleNamespace(get_token=get_token, api_key="key"), loop=asyncio.get_running_loop())
    view = lastfm_view.LastFMView(SimpleNamespace(bot=bot), "")
    monkeypatch.setattr(lastfm_view, "guild_text", AsyncMock(return_value="texto"))
    inter = SimpleNamespace(response=response, guild=None, send=AsyncMock())
    callbacks = [asyncio.create_task(view.send_authurl_callback(inter)) for _ in range(2)]
    await both_deferred.wait()
    release.set()
    await asyncio.gather(*callbacks)
    get_token.assert_awaited_once()
    assert response.defer.await_count == 2
    assert inter.send.await_count == 2
    assert all(call.kwargs["ephemeral"] for call in inter.send.await_args_list)
    task = view.check_loop
    assert sum(t.get_coro().__qualname__.endswith("check_session_loop") for t in asyncio.all_tasks()) == 1
    view.stop()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()


@pytest.mark.parametrize(
    "outcome",
    ["timeout", "error", "client_error", "content_type", "missing_token", "finished_before", "finished_during"],
)
async def test_lastfm_auth_early_exit_answers_deferred_interaction(mundo, monkeypatch, outcome):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from empiremuse.utils.music import lastfm_view
    from empiremuse.utils.music.lastfm_tools import LastFmException

    import aiohttp

    assert 0 < lastfm_view.AUTH_TOKEN_TIMEOUT_S <= 30
    monkeypatch.setattr(lastfm_view, "AUTH_TOKEN_TIMEOUT_S", 0)
    monkeypatch.setattr(lastfm_view, "guild_text", AsyncMock(side_effect=lambda bot, guild, key: key))

    async def get_token():
        if outcome == "timeout":
            await asyncio.Event().wait()
        if outcome == "error":
            raise LastFmException({"error": 11, "message": "unavailable"})
        if outcome == "client_error":
            raise aiohttp.ClientError()
        if outcome == "content_type":
            raise aiohttp.ContentTypeError(None, ())
        if outcome == "missing_token":
            raise KeyError("token")
        view.stop()
        return "token"

    get_token = AsyncMock(side_effect=get_token)
    bot = SimpleNamespace(last_fm=SimpleNamespace(get_token=get_token), loop=asyncio.get_running_loop())
    view = lastfm_view.LastFMView(SimpleNamespace(bot=bot), "")
    if outcome == "finished_before":
        view.stop()
    inter = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()), guild=None, send=AsyncMock())
    await view.send_authurl_callback(inter)
    inter.response.defer.assert_awaited_once_with(ephemeral=True)
    key = "legacy.lastfm.auth_expired" if outcome.startswith("finished") else "legacy.lastfm.auth_retry"
    inter.send.assert_awaited_once_with(key, ephemeral=True)
    assert view.check_loop is None
    if outcome == "finished_before":
        get_token.assert_not_awaited()
    view.stop()


@pytest.mark.parametrize("view_toggle", [False, True])
async def test_lastfm_view_timeout_keeps_controller_scrobble_change(mundo, view_toggle):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from empiremuse.cogs.lastfm import LastFMView
    from empiremuse.music_cog.controller_buttons import _pc_lastfm
    from empiremuse.i18n import get_i18n
    from tests.persistence.test_erasure_hooks import _fluxo
    from tests.support.erasure_world import gravar_usuario

    await gravar_usuario(mundo, ALVO, lastfm_sessionkey="current", lastfm_username="name", lastfm_scrobble=True)
    cog, _, inter = _fluxo(mundo)
    save = cog.bot.update_global_data

    async def update(user, data, *, db_name):
        return await save(user, data=data, db_name=db_name)

    cog.bot.update_global_data = update
    cog.bot.get_color = lambda: 0
    cog.bot.pool.i18n = get_i18n()
    inter.response = SimpleNamespace(defer=AsyncMock())
    view = LastFMView(SimpleNamespace(bot=cog.bot), "current", scrobble=True)
    read_at = mundo.pool.erasure_tombstones.now()
    if view_toggle:
        await view.scrobble_enable(inter)
        assert view.scrobble_enabled is False
    await _pc_lastfm(cog, SimpleNamespace(cmd=None, interaction=inter))
    await view.on_timeout()
    assert await cog._save_link(inter, view, None, read_at)
    doc = await mundo.database.find_doc(ALVO, db_name="users", collection="global")
    assert doc["lastfm"]["scrobble"] is view_toggle
    assert mundo.pool.lastfm_sessions[ALVO]["scrobble"] is view_toggle
