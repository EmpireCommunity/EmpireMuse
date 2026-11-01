"""As portas reais da rádio (item 8, R1) sobre bot, canal e player falsos: o direito de agora (R-1), os ouvintes, a conexão, o palco (R-5),
a fila virtual e o desligamento por perda de direito."""

from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from empiremuse.rules.radio import check_radio, radio_id
from empiremuse.utils.radio import RadioHub
from empiremuse.utils.radio.discord_ports import DiscordRadioPorts
from empiremuse.utils.radio.runner import Allowed
from tests.persistence.test_radio_store import FakeDb

pytestmark = pytest.mark.critical

BOT_ID, GUILD, CHANNEL = 11, 500, 900


def member(*, bot=False, deaf=False, self_deaf=False):
    return SimpleNamespace(bot=bot, voice=SimpleNamespace(deaf=deaf, self_deaf=self_deaf))


def track(n, duration=60_000):
    return SimpleNamespace(title=f"t{n}", duration=duration)


class Player:
    def __init__(self):
        self.queue = deque()
        self.connect = AsyncMock()
        self.process_next = AsyncMock()
        self.stop = AsyncMock()
        self.destroy = AsyncMock()


def build(*, stage=False, premium=True, perms=(True, True), has_channel=True, mute_members=False):
    db = FakeDb()
    pool = SimpleNamespace(database=db)
    hub = RadioHub(pool)
    pool.radio = hub
    channel = None
    if has_channel:
        channel = MagicMock(spec=disnake.StageChannel if stage else disnake.VoiceChannel)
        channel.id = CHANNEL
        channel.members = []
        channel.permissions_for.return_value = SimpleNamespace(
            view_channel=perms[0], connect=perms[1], mute_members=mute_members
        )
    me = SimpleNamespace(edit=AsyncMock(), request_to_speak=AsyncMock())
    guild = SimpleNamespace(id=GUILD, me=me, get_channel=lambda cid: channel if cid == CHANNEL else None)
    player = Player()
    music = SimpleNamespace(
        get_best_node=AsyncMock(return_value=SimpleNamespace(identifier="LOCAL")),
        get_tracks=AsyncMock(return_value=([track(0), track(1, 30_000), track(2, 90_000)], None)),
    )
    bot = SimpleNamespace(
        user=SimpleNamespace(id=BOT_ID),
        get_guild=lambda gid: guild if gid == GUILD else None,
        get_cog=lambda name: music,
        pool=SimpleNamespace(i18n=SimpleNamespace(for_guild=AsyncMock(return_value=lambda key, **k: key))),
        music=SimpleNamespace(players={}, get_player=MagicMock(side_effect=lambda **kw: _register(bot, player, kw))),
    )
    pool.get_all_bots = lambda: [bot]
    pool.servers = SimpleNamespace(
        playlists=SimpleNamespace(find=AsyncMock(return_value=SimpleNamespace(url="https://x.invalid/list")))
    )
    radio = check_radio(
        {
            "_id": radio_id(BOT_ID, GUILD),
            "channel_id": str(CHANNEL),
            "kind": "custom",
            "source": {"playlist": "Rock"},
        }
    )
    ports = DiscordRadioPorts(pool, radio)
    return SimpleNamespace(
        ports=ports, hub=hub, db=db, bot=bot, guild=guild, channel=channel, player=player, music=music, premium=premium
    )


def _register(bot, player, kwargs):
    bot.music.players[kwargs["guild_id"]] = player
    player.kwargs = kwargs
    return player


async def grant(w):
    await w.hub.premium.grant(GUILD, by=1)


class TestAllowed:
    @pytest.mark.asyncio
    async def test_without_a_premium_document_the_radio_is_not_allowed(self):
        w = build()

        assert await w.ports.allowed() is Allowed.NO_PREMIUM

    @pytest.mark.asyncio
    async def test_a_deleted_channel_and_a_lost_permission_are_told_apart(self):
        gone = build(has_channel=False)
        await grant(gone)
        no_connect = build(perms=(True, False))
        await grant(no_connect)
        no_view = build(perms=(False, True))
        await grant(no_view)

        assert await gone.ports.allowed() is Allowed.NO_CHANNEL
        assert await no_connect.ports.allowed() is Allowed.NO_PERMISSION
        assert await no_view.ports.allowed() is Allowed.NO_PERMISSION

    @pytest.mark.asyncio
    async def test_a_premium_guild_with_the_channel_and_permissions_is_allowed(self):
        w = build()
        await grant(w)

        assert await w.ports.allowed() is Allowed.YES

    @pytest.mark.asyncio
    async def test_a_database_failure_propagates_so_the_runner_can_treat_it_as_not_knowing(self):
        w = build()
        w.db.fail = True

        with pytest.raises(Exception):
            await w.ports.allowed()


class TestListeners:
    def test_bots_and_deafened_members_do_not_count(self):
        w = build()
        w.channel.members = [member(bot=True), member(deaf=True), member(self_deaf=True), member(), member()]

        assert w.ports.listeners() == 2

    def test_a_missing_channel_has_no_listeners(self):
        assert build(has_channel=False).ports.listeners() == 0


class TestConnect:
    @pytest.mark.asyncio
    async def test_it_creates_a_marked_player_that_loops_the_queue_and_joins_deafened(self):
        w = build()

        assert await w.ports.connect() is True

        assert w.player.radio_id == radio_id(BOT_ID, GUILD) and w.player.loop == "queue"
        assert w.player.controller_mode is False and w.player.kwargs["keep_connected"] is True
        assert w.player.kwargs["static"] is False
        w.player.connect.assert_awaited_once_with(CHANNEL, self_deaf=True)

    @pytest.mark.asyncio
    async def test_a_second_connect_reuses_the_player(self):
        w = build()
        await w.ports.connect()
        await w.ports.connect()

        assert w.bot.music.get_player.call_count == 1 and w.player.connect.await_count == 2

    @pytest.mark.asyncio
    async def test_a_discord_failure_is_false_never_an_exception(self):
        w = build()
        w.player.connect.side_effect = disnake.HTTPException(MagicMock(status=500), "x")

        assert await w.ports.connect() is False

    @pytest.mark.asyncio
    async def test_a_gone_bot_or_channel_cannot_connect(self):
        assert await build(has_channel=False).ports.connect() is False

    @pytest.mark.asyncio
    async def test_on_a_stage_it_unsuppresses_with_mute_members_and_asks_to_speak_without(self):
        with_perm = build(stage=True, mute_members=True)
        without = build(stage=True, mute_members=False)

        await with_perm.ports.connect()
        await without.ports.connect()

        with_perm.guild.me.edit.assert_awaited_once_with(suppress=False)
        without.guild.me.request_to_speak.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_forbidden_on_the_stage_is_tried_once_per_connection(self):
        w = build(stage=True, mute_members=True)
        w.guild.me.edit = AsyncMock(side_effect=disnake.Forbidden(MagicMock(status=403), "x"))

        assert await w.ports.connect() is True  # a call continua; só o palco não deixou falar
        await w.ports.connect()

        assert w.guild.me.edit.await_count == 1


class TestVirtualQueue:
    @pytest.mark.asyncio
    async def test_the_queue_is_rotated_to_the_virtual_track_and_played_at_the_virtual_position(self):
        w = build()
        await w.ports.connect()
        await w.ports.prepare()

        await w.ports.play_virtual(1, 12_000)

        assert [t.title for t in w.player.queue] == ["t1", "t2", "t0"]
        w.player.process_next.assert_awaited_once_with(start_position=12_000)
        assert w.ports.durations_ms() == [60_000, 30_000, 90_000]

    @pytest.mark.asyncio
    async def test_the_playlist_is_loaded_once_and_with_the_server_playlist_url(self):
        w = build()
        await w.ports.connect()

        await w.ports.prepare()
        await w.ports.play_virtual(0, 0)

        assert w.music.get_tracks.await_count == 1 and w.music.get_tracks.await_args.args[0] == "https://x.invalid/list"

    @pytest.mark.asyncio
    async def test_an_empty_or_missing_playlist_raises_so_the_runner_backs_off(self):
        empty = build()
        empty.music.get_tracks.return_value = ([], None)
        missing = build()
        missing.hub.premium.pool.servers = missing.ports.pool.servers
        missing.ports.pool.servers.playlists.find.return_value = None

        for w in (empty, missing):
            await w.ports.connect()
            with pytest.raises(RuntimeError):
                await w.ports.prepare()

    @pytest.mark.asyncio
    async def test_stopping_the_stream_clears_the_queue_and_stops_the_player(self):
        w = build()
        await w.ports.connect()
        w.player.queue.extend([track(0)])

        await w.ports.stop_stream()

        assert not w.player.queue and w.player.stop.await_count == 1

    @pytest.mark.asyncio
    async def test_stopping_without_a_player_is_harmless(self):
        await build().ports.stop_stream()


class TestTurnOff:
    @pytest.mark.asyncio
    async def test_losing_the_right_disables_the_radio_forgets_it_and_leaves_the_call(self):
        w = build()
        await grant(w)
        radio = await w.hub.store.create(w.ports.radio)
        await w.hub.store.set_enabled(radio.id, True)
        await w.ports.connect()

        await w.ports.turn_off(Allowed.NO_PREMIUM)

        assert not (await w.hub.store.get(radio.id)).enabled
        assert (await w.hub.premium.fresh(GUILD)).active == 0
        w.player.destroy.assert_awaited_once()


class FakeMessage:
    def __init__(self, mid, created, *, pinned=False, author=77, system=False):
        self.id = mid
        self.created_at = SimpleNamespace(timestamp=lambda: created)
        self.pinned = pinned
        self.author = SimpleNamespace(id=author)
        self._system = system

    def is_system(self):
        return self._system


def chat_world(messages):
    from empiremuse.utils.radio.discord_ports import DiscordChatPorts

    w = build()
    channel = w.channel

    async def history(limit):
        for message in messages[:limit]:
            yield message

    channel.history = history
    channel.delete_messages = AsyncMock()
    return w, DiscordChatPorts(w.ports.pool, w.ports.radio)


class TestChatPorts:
    @pytest.mark.asyncio
    async def test_the_pinned_the_panel_the_bots_own_and_system_messages_are_protected(self):
        messages = [
            FakeMessage(1, 100.0),
            FakeMessage(2, 100.0, pinned=True),
            FakeMessage(3, 100.0, author=BOT_ID),
            FakeMessage(4, 100.0, system=True),
        ]
        w, ports = chat_world(messages)

        got = await ports.fetch_recent(100)

        assert [(mid, protected) for mid, _, protected in got] == [(1, False), (2, True), (3, True), (4, True)]

    @pytest.mark.asyncio
    async def test_the_panel_message_of_the_radio_is_protected(self):
        import dataclasses

        w, ports = chat_world([FakeMessage(9, 100.0)])
        ports.radio = dataclasses.replace(ports.radio, panel_message_id=9)

        assert [p for _, _, p in await ports.fetch_recent(100)] == [True]

    @pytest.mark.asyncio
    async def test_a_forbidden_history_or_delete_becomes_permission_lost(self):
        from empiremuse.utils.radio.chat import PermissionLost

        w, ports = chat_world([FakeMessage(1, 100.0)])
        w.channel.delete_messages = AsyncMock(side_effect=disnake.Forbidden(MagicMock(status=403), "x"))

        with pytest.raises(PermissionLost):
            await ports.bulk_delete([1])

        async def refused(limit):
            raise disnake.Forbidden(MagicMock(status=403), "x")
            yield  # pragma: no cover

        w.channel.history = refused
        with pytest.raises(PermissionLost):
            await ports.fetch_recent(100)

    @pytest.mark.asyncio
    async def test_the_delete_goes_to_the_radios_own_channel_in_one_call(self):
        w, ports = chat_world([])

        await ports.bulk_delete([5, 6, 7])

        w.channel.delete_messages.assert_awaited_once()
        assert [o.id for o in w.channel.delete_messages.await_args.args[0]] == [5, 6, 7]

    @pytest.mark.asyncio
    async def test_a_gone_channel_has_nothing_to_sweep(self):
        w, ports = chat_world([])
        w.guild.get_channel = lambda cid: None

        assert await ports.fetch_recent(100) == []
        await ports.bulk_delete([1])

    def test_a_stage_channel_is_told_apart_from_a_voice_channel(self):
        _, voice = chat_world([])
        stage_world = build(stage=True)
        from empiremuse.utils.radio.discord_ports import DiscordChatPorts

        assert voice.stage is False
        assert DiscordChatPorts(stage_world.ports.pool, stage_world.ports.radio).stage is True
