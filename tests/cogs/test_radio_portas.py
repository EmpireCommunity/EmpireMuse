"""As portas reais da rádio (item 8, R1) sobre bot, canal e player falsos: o direito de agora (R-1), os ouvintes, a conexão, o palco (R-5),
a fila virtual e o desligamento por perda de direito."""

import dataclasses
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
    """Premium e a rádio criada e LIGADA no banco (o runner só segue enquanto o banco diz que ela está ligada)."""
    await w.hub.premium.grant(GUILD, by=1)
    created = await w.hub.store.create(w.ports.radio)
    await w.hub.store.set_enabled(created.id, True)


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


class TestDisabledFromOutside:
    @pytest.mark.asyncio
    async def test_a_radio_turned_off_in_the_database_is_no_longer_allowed_to_run(self):
        w = build()
        await grant(w)
        await w.hub.store.set_enabled(w.ports.radio.id, False)

        assert await w.ports.allowed() is Allowed.DISABLED

    @pytest.mark.asyncio
    async def test_a_deleted_radio_document_is_also_disabled(self):
        w = build()
        await w.hub.premium.grant(GUILD, by=1)

        assert await w.ports.allowed() is Allowed.DISABLED


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
        assert (
            w.music.get_tracks.await_args.kwargs["source"] is False
        )  # URL exata: nada de "ytsearch:" nem Deezer/Spotify (gate R-8)

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
        radio = w.ports.radio
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
    async def test_the_jukebox_panel_and_notices_of_other_pool_bots_are_protected_in_the_radio_chat(self):  # V181-1
        w, ports = chat_world(
            [FakeMessage(1, 100.0, author=55), FakeMessage(2, 100.0, author=77), FakeMessage(3, 100.0, author=BOT_ID)]
        )
        w.ports.pool.get_all_bots = lambda: [
            w.bot,
            SimpleNamespace(user=SimpleNamespace(id=55)),
            SimpleNamespace(user=None),
        ]

        got = await ports.fetch_recent(100)

        assert [(mid, protected) for mid, _, protected in got] == [
            (1, True),
            (2, False),
            (3, True),
        ]  # só o usuário 77 é varrido

    @pytest.mark.asyncio
    async def test_the_panel_message_of_the_radio_is_protected(self):
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


class TestStageAsksEveryConnection:
    """Keystone (R-5): UMA tentativa por CONEXÃO; o player sobrevive à queda, então a segunda conexão tenta de novo."""

    @pytest.mark.asyncio
    async def test_a_second_connection_on_the_same_player_asks_for_the_floor_again(self):
        w = build(stage=True, mute_members=True)

        await w.ports.connect()
        await w.ports.connect()  # a volta depois da queda reaproveita o player

        assert w.guild.me.edit.await_count == 2

    @pytest.mark.asyncio
    async def test_one_failing_connection_does_not_silence_the_next_one(self):
        w = build(stage=True, mute_members=True)
        w.guild.me.edit = AsyncMock(side_effect=[disnake.Forbidden(MagicMock(status=403), "x"), None])

        assert await w.ports.connect() is True
        assert await w.ports.connect() is True

        assert w.guild.me.edit.await_count == 2

    @pytest.mark.asyncio
    async def test_inside_one_connection_a_forbidden_is_tried_once(self):
        w = build(stage=True, mute_members=True)
        w.guild.me.edit = AsyncMock(side_effect=disnake.Forbidden(MagicMock(status=403), "x"))

        await w.ports.connect()

        assert w.guild.me.edit.await_count == 1


def panel_message(mid, *, author=BOT_ID, custom_id="radio_panel_mute", pinned=False):
    """Uma mensagem com os botões do painel de rádio (o que o Discord devolve em ``message.components``)."""
    message = FakeMessage(mid, 100.0, author=author, pinned=pinned)
    message.components = [SimpleNamespace(children=[SimpleNamespace(custom_id=custom_id)])]
    message.delete = AsyncMock()
    return message


class TestOrphanPanels:
    """Litmus: 62 mensagens acumuladas eram painéis órfãos do próprio bot (o id do painel se perdeu e a limpeza protege tudo que o bot escreveu)."""

    @pytest.mark.asyncio
    async def test_an_old_panel_of_the_radio_bot_is_swept_but_the_current_one_and_its_notices_are_kept(self):
        notice = FakeMessage(3, 100.0, author=BOT_ID)  # aviso do bot (sem os botões do painel)
        w, ports = chat_world([panel_message(1), panel_message(2), notice])
        ports.radio = dataclasses.replace(ports.radio, panel_message_id=2)

        got = await ports.fetch_recent(100)

        assert [(mid, protected) for mid, _, protected in got] == [(1, False), (2, True), (3, True)]

    @pytest.mark.asyncio
    async def test_without_a_known_panel_id_nothing_is_called_an_orphan(self):
        w, ports = chat_world([panel_message(1)])

        assert [p for _, _, p in await ports.fetch_recent(100)] == [
            True
        ]  # o painel recém-postado ainda sem id gravado não é varrido

    @pytest.mark.asyncio
    async def test_a_pinned_old_panel_and_the_jukebox_panel_of_another_bot_stay(self):
        w, ports = chat_world(
            [panel_message(1, pinned=True), panel_message(2, author=55, custom_id="musicplayer_stop"), panel_message(9)]
        )
        w.ports.pool.get_all_bots = lambda: [w.bot, SimpleNamespace(user=SimpleNamespace(id=55))]
        ports.radio = dataclasses.replace(ports.radio, panel_message_id=9)

        got = await ports.fetch_recent(100)

        assert [(mid, protected) for mid, _, protected in got] == [(1, True), (2, True), (9, True)]

    @pytest.mark.asyncio
    async def test_posting_a_new_panel_deletes_the_old_ones_of_this_bot_only(self):
        w = build()
        await grant(w)
        mine_old, jukebox, mine_pinned = (
            panel_message(1),
            panel_message(2, author=55, custom_id="musicplayer_stop"),
            panel_message(3, pinned=True),
        )

        async def history(limit):
            for message in (mine_old, jukebox, mine_pinned):
                yield message

        w.channel.history = history
        w.channel.send = AsyncMock(return_value=SimpleNamespace(id=5555))
        w.channel.fetch_message = AsyncMock()
        w.bot.get_color = lambda me: 0
        w.bot.user = SimpleNamespace(id=BOT_ID)

        await w.ports.ensure_panel()

        mine_old.delete.assert_awaited_once()
        jukebox.delete.assert_not_awaited()
        mine_pinned.delete.assert_not_awaited()
        assert (await w.hub.store.get(w.ports.radio.id)).panel_message_id == 5555

    @pytest.mark.asyncio
    async def test_a_forbidden_cleanup_does_not_stop_the_new_panel(self, caplog):
        w = build()
        await grant(w)
        old = panel_message(1)
        old.delete = AsyncMock(side_effect=disnake.Forbidden(MagicMock(status=403), "x"))

        async def history(limit):
            yield old

        w.channel.history = history
        w.channel.send = AsyncMock(return_value=SimpleNamespace(id=777))
        w.channel.fetch_message = AsyncMock()
        w.bot.get_color = lambda me: 0
        w.bot.user = SimpleNamespace(id=BOT_ID)

        with caplog.at_level("WARNING"):
            await w.ports.ensure_panel()

        assert (await w.hub.store.get(w.ports.radio.id)).panel_message_id == 777 and "Forbidden" in caplog.text


STAGE_MODERATOR = dict(view_channel=True, connect=True, mute_members=True, manage_channels=True, move_members=True)


def stage_world(*, instance=None, permissions=None):
    """Um palco SEM evento ao vivo (``channel.instance`` é ``None``): é aí que o Discord recusa o pedido de fala com 403 50001."""
    w = build(stage=True, mute_members=True)
    w.channel.instance = instance
    w.channel.create_instance = AsyncMock()
    w.channel.send = AsyncMock()
    w.channel.permissions_for.return_value = SimpleNamespace(**(permissions or STAGE_MODERATOR))
    return w


class TestStageInstance:
    """Live test: uma rádio num palco sem evento ao vivo ficava suprimida ("sem permissão para falar no palco") porque nada criava o evento."""

    @pytest.mark.asyncio
    async def test_a_stage_without_a_live_event_gets_one_before_the_bot_unsuppresses(self):
        w = stage_world()
        order = []
        w.channel.create_instance.side_effect = lambda **kw: order.append("create")
        w.guild.me.edit.side_effect = lambda **kw: order.append("unsuppress")

        assert await w.ports.connect() is True

        kwargs = w.channel.create_instance.await_args.kwargs
        assert kwargs["privacy_level"] is disnake.StagePrivacyLevel.guild_only and kwargs["topic"] == "Rock"
        assert order == ["create", "unsuppress"]

    @pytest.mark.asyncio
    async def test_the_topic_is_the_sanitised_name_and_fits_the_limit(self):
        w = stage_world()
        w.ports.radio = dataclasses.replace(
            w.ports.radio, source={"playlist": "@everyone <@1> https://evil.example " + "x" * 300}
        )

        await w.ports.connect()

        topic = w.channel.create_instance.await_args.kwargs["topic"]
        assert "@everyone" not in topic and "<@" not in topic and "://" not in topic and 0 < len(topic) <= 120

    @pytest.mark.asyncio
    async def test_a_live_event_that_already_exists_is_left_alone(self):
        w = stage_world(instance=SimpleNamespace(delete=AsyncMock()))

        await w.ports.connect()
        await w.ports.end_stage()

        w.channel.create_instance.assert_not_awaited()
        w.channel.instance.delete.assert_not_awaited()  # o evento era de uma pessoa: o bot não o encerra

    @pytest.mark.asyncio
    async def test_without_the_stage_moderator_permissions_one_clear_notice_names_what_is_missing(self):
        w = stage_world(permissions={**STAGE_MODERATOR, "manage_channels": False, "move_members": False})
        w.player.tr = lambda key, **kw: f"{key}|{kw.get('permissions', '')}"

        await w.ports.connect()
        await w.ports.connect()  # reconectou: o aviso não se repete

        w.channel.create_instance.assert_not_awaited()
        w.channel.send.assert_awaited_once()
        text = w.channel.send.await_args.args[0]
        assert "radio.stage.perm.manage_channels" in text and "radio.stage.perm.move_members" in text
        assert "radio.stage.perm.mute_members" not in text
        assert w.channel.send.await_args.kwargs["allowed_mentions"].everyone is False

    @pytest.mark.asyncio
    async def test_a_failed_creation_is_only_logged_and_the_bot_still_asks_to_speak(self, caplog):
        w = stage_world()
        w.channel.create_instance.side_effect = disnake.Forbidden(MagicMock(status=403), "x")

        with caplog.at_level("WARNING"):
            assert await w.ports.connect() is True

        assert "Forbidden" in caplog.text
        w.guild.me.edit.assert_awaited_once_with(suppress=False)

    @pytest.mark.asyncio
    async def test_turning_the_radio_off_ends_only_the_event_the_bot_created(self):
        w = stage_world()
        await w.ports.connect()
        instance = SimpleNamespace(delete=AsyncMock())
        w.channel.instance = instance  # agora o evento existe (foi o bot que o criou)

        await w.ports.end_stage()
        await w.ports.end_stage()  # desligar duas vezes não encerra duas

        instance.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_voice_channel_never_creates_an_event(self):
        w = build(mute_members=True)

        await w.ports.connect()
        await w.ports.end_stage()

        assert not any(call[0] == "create_instance" for call in w.channel.method_calls)

    @pytest.mark.asyncio
    async def test_the_manager_ends_the_event_when_the_radio_is_deactivated(self):
        w = build(stage=True)
        w.ports.end_stage = AsyncMock()
        await grant(w)
        radio = await w.hub.store.create(w.ports.radio)
        w.hub.manager.ports_factory = lambda r: w.ports
        await w.hub.manager.activate(radio, wait=True)

        await w.hub.manager.deactivate(radio.id)

        w.ports.end_stage.assert_awaited_once()
