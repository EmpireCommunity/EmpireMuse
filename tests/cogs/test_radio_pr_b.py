"""T-453: exclusão sem backup, runner órfão e canal visível/conectável pelo ator."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from empiremuse.utils.radio.premium import RadioUnavailable
from empiremuse.utils.radio.store import RadioInvalid, RadioNotFound
from tests.cogs.radio_config import configure
from tests.cogs.test_radio_comandos import GUILD, CHANNEL, build, premium

pytestmark = pytest.mark.critical
ACTOR = 7


async def world(**kwargs):
    w = build(**kwargs)

    async def tick():
        if w.db.fail:
            raise ConnectionError("banco indisponível")

    w.db._tick = tick
    w.hub.premium._clock = lambda: 0.0
    await premium(w)
    return w


@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_reset_deletes_before_stopping_and_stops_even_when_delete_fails(failure):
    w = await world()
    radio = await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
    radio = await w.hub.store.set_enabled(radio.id, True)
    events = []
    delete = w.db.delete_data

    async def delete_hook(id_, db_name, collection):
        events.append("delete")
        if failure:
            raise ConnectionError("banco indisponível")
        await delete(id_, db_name, collection)

    async def stop(radios):
        events.append("stop")

    w.db.delete_data = delete_hook
    w.hub.manager.stop_radios = AsyncMock(side_effect=stop)
    w.hub.setup._delete_panel = AsyncMock()
    if failure:
        with pytest.raises(RadioUnavailable):
            await w.hub.setup.reset(GUILD, ACTOR, radio.id, radio.rev)
        assert (await w.hub.store.get(radio.id)).enabled is False
        w.hub.setup._delete_panel.assert_not_awaited()
    else:
        await w.hub.setup.reset(GUILD, ACTOR, radio.id, radio.rev)
        assert await w.hub.store.get(radio.id) is None
        w.hub.setup._delete_panel.assert_awaited_once()
    assert events == ["delete", "stop"]
    assert (await w.hub.premium.fresh(GUILD)).active == 0
    assert "config_backup" not in w.db.tables


@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent", [False, True])
async def test_reset_ignores_old_revision_including_an_edit_after_the_snapshot(concurrent):
    w = await world()
    radio = await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
    real_delete = w.hub.store.delete

    async def delete(rid):
        if concurrent:
            await w.hub.store.save_config(
                replace(radio, channel_id=CHANNEL + 1), expected_rev=radio.rev, updated_by="other"
            )
        await real_delete(rid)

    w.hub.store.delete = delete
    await w.hub.setup.reset(GUILD, ACTOR, radio.id, radio.rev if concurrent else radio.rev + 1)
    assert await w.hub.store.get(radio.id) is None
    assert "config_backup" not in w.db.tables


@pytest.mark.regression
@pytest.mark.asyncio
async def test_activation_of_a_document_deleted_during_ensure_clock_never_builds_a_runner():
    w = await world()
    radio = await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)

    async def deleted(radio):
        await w.hub.store.delete(radio.id)
        raise RadioNotFound

    w.hub.store.ensure_clock = deleted
    w.hub.manager._runner_for = MagicMock(return_value=None)
    await w.hub.manager.activate(radio)
    w.hub.manager._runner_for.assert_not_called()
    assert not w.hub.manager.is_running(radio.id)


def actor_permissions(w, *, view=True, connect=True, bot_connect=True):
    me = [b.get_guild(GUILD).me for b in w.bots]
    w.channel.permissions_for.side_effect = lambda who: SimpleNamespace(
        view_channel=True if any(who is m for m in me) else view,
        connect=bot_connect if any(who is m for m in me) else connect,
        speak=True,
    )


@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["view", "connect"])
@pytest.mark.parametrize("state", ["free", "radio", "jukebox", "bot_cannot_connect"])
async def test_private_channels_never_reveal_their_state_through_options_or_configure(missing, state):
    w = await world(request_channel=CHANNEL if state == "jukebox" else None)
    if state == "radio":
        await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
    actor_permissions(w, **{missing: False}, bot_connect=state != "bot_cannot_connect")
    options = await w.hub.setup.options(GUILD, ACTOR)
    assert options.channels == ()
    with pytest.raises(RadioInvalid) as refused:
        await w.hub.setup.configure(GUILD, ACTOR, radio_id=None, channel_id=CHANNEL, bot_id=3, kind="custom", rev=None)
    assert (refused.value.error.code, refused.value.error.key) == ("channel_kind", "channel_id")


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [False, True])
async def test_visible_connectable_channel_is_allowed_for_an_admin_with_cache_fallback(cached):
    w = await world(manage_guild=False)
    actor_permissions(w)
    for bot in w.bots:
        guild = bot.get_guild(GUILD)
        actor = SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=False, administrator=True))
        guild.get_member = lambda uid: actor if cached else None
        guild.fetch_member = AsyncMock(return_value=actor)
    assert (await w.hub.setup.options(GUILD, ACTOR)).channels
    radio = await w.hub.setup.configure(
        GUILD, ACTOR, radio_id=None, channel_id=CHANNEL, bot_id=2, kind="custom", rev=None
    )
    assert radio.channel_id == CHANNEL


@pytest.mark.asyncio
async def test_options_without_management_permission_is_empty_and_missing_member_fails_closed():
    w = await world(manage_guild=False)
    assert not (await w.hub.setup.options(GUILD, ACTOR)).channels
    assert not (await w.hub.setup.options(GUILD, ACTOR)).bots
    for bot in w.bots:
        guild = bot.get_guild(GUILD)
        guild.get_member = lambda uid: None
        guild.fetch_member = AsyncMock(side_effect=disnake.HTTPException(MagicMock(status=404), "x"))
    assert not (await w.hub.setup.options(GUILD, ACTOR)).channels
    with pytest.raises(RadioInvalid) as refused:
        await w.hub.setup._check_channel(GUILD, CHANNEL, w.bots[1], ACTOR)
    assert refused.value.error.code == "channel_kind"


@pytest.mark.asyncio
async def test_autocomplete_passes_the_discord_actor_to_the_real_options_gate():
    from empiremuse.cogs.radio import bot_autocomplete

    w = await world()
    original = w.hub.setup.options
    w.hub.setup.options = AsyncMock(side_effect=original)
    await bot_autocomplete(w.inter, "")
    w.hub.setup.options.assert_awaited_once_with(GUILD, ACTOR)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["view", "connect"])
async def test_an_unchanged_configured_channel_is_rechecked_for_the_actor(missing):
    w = await world()
    radio = await configure(w.hub, guild_id=GUILD, channel_id=CHANNEL, bot_id=2)
    actor_permissions(w, **{missing: False})
    with pytest.raises(RadioInvalid) as refused:
        await w.hub.setup.configure(
            GUILD, ACTOR, radio_id=radio.id, channel_id=CHANNEL, bot_id=2, kind=radio.kind, rev=radio.rev
        )
    assert refused.value.error.code == "channel_kind"
    assert await w.hub.store.get(radio.id) == radio
