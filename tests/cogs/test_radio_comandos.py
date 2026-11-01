"""`/radio ligar|desligar|status` e os ganchos do player (item 8, R1): as permissões conferidas em execução (R-4), o bot reserva escolhido, o
cooldown de 10 s e a rádio que o ``player_voice`` entrega ao gerenciador em vez de entrar no modo economia."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from empiremuse.cogs import radio as radio_cog
from empiremuse.i18n import get_i18n
from empiremuse.rules.radio import radio_id
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.music.models.player_voice import PlayerVoiceMixin
from empiremuse.utils.radio import RadioHub
from empiremuse.utils.radio.runner import Phase
from tests.cogs.test_radio_runner import FakePorts
from tests.persistence.test_radio_store import FakeDb

pytestmark = pytest.mark.critical

GUILD, CHANNEL = 500, 900


@pytest.fixture(autouse=True)
def fresh_cooldown():
    radio_cog.RADIO_COOLDOWN._cache.clear()
    yield
    radio_cog.RADIO_COOLDOWN._cache.clear()


def make_channel(*, manage_messages=True):
    channel = MagicMock(spec=disnake.VoiceChannel)
    channel.id = CHANNEL
    channel.mention = f"<#{CHANNEL}>"
    channel.permissions_for.side_effect = lambda who: SimpleNamespace(
        view_channel=True, connect=True, manage_messages=manage_messages
    )
    return channel


def make_bot(bot_id, identifier, channel, *, ready=True, in_voice=False):
    guild = SimpleNamespace(
        id=GUILD, me=SimpleNamespace(voice=object() if in_voice else None), get_channel=lambda cid: channel
    )
    player_calls = AsyncMock()
    return SimpleNamespace(
        identifier=identifier,
        user=SimpleNamespace(id=bot_id),
        bot_ready=ready,
        exclusive_guild_id=None,
        music=SimpleNamespace(players={}),
        get_guild=lambda gid: guild if gid == GUILD else None,
        destroy=player_calls,
    )


def build(*, owner=True, manage_guild=True, manage_messages=True, playlist=True, free_bots=(2, 3)):
    db = FakeDb()
    channel = make_channel(manage_messages=manage_messages)
    bots = [make_bot(1, "TOKEN", channel), *(make_bot(n, f"TOKEN_{n}", channel) for n in free_bots)]
    pool = SimpleNamespace(database=db, i18n=get_i18n(), get_all_bots=lambda: bots)
    pool.radio = RadioHub(pool)
    ports = FakePorts()
    pool.radio.manager.ports_factory = lambda radio: ports
    pool.servers = SimpleNamespace(
        main_bot=lambda: bots[0],
        playlists=SimpleNamespace(
            find=AsyncMock(return_value=SimpleNamespace(name="Rock", url="u") if playlist else None)
        ),
    )
    cog = radio_cog.Radio(SimpleNamespace(pool=pool, env_owner_ids={1000}))
    pool.servers.main_bot = lambda: cog.bot  # este cog é o do principal
    inter = SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_message=AsyncMock(),
        guild=SimpleNamespace(id=GUILD, preferred_locale="pt-BR"),
        guild_id=GUILD,
        author=SimpleNamespace(id=7, guild_permissions=SimpleNamespace(manage_guild=manage_guild)),
        bot=cog.bot,
    )
    return SimpleNamespace(
        cog=cog, pool=pool, hub=pool.radio, inter=inter, channel=channel, bots=bots, ports=ports, db=db
    )


async def settle(w):
    """A partida da rádio segue em segundo plano depois do comando; espera as tarefas terminarem."""
    while w.hub.manager._tasks:
        await asyncio.gather(*list(w.hub.manager._tasks))


async def premium(w):
    await w.hub.premium.grant(GUILD, by=1)


async def run_on(w, **kwargs):
    await radio_cog.Radio.radio_on.callback(w.cog, w.inter, **kwargs)
    await settle(w)


def reply(w):
    return w.inter.edit_original_message.await_args.kwargs["content"]


class TestLigar:
    @pytest.mark.asyncio
    async def test_it_turns_the_radio_on_in_the_first_free_reserve_and_starts_it(self):
        w = build()
        await premium(w)

        await run_on(w, channel=w.channel, playlist="rock")

        assert w.hub.manager.is_running(radio_id(2, GUILD)) and w.ports.connects == 1
        assert f"<#{CHANNEL}>" in reply(w) and "<@2>" in reply(w) and "Rock" in reply(w)
        assert w.inter.edit_original_message.await_args.kwargs["allowed_mentions"].everyone is False
        assert w.inter.response.defer.await_args.kwargs == {
            "ephemeral": True
        }  # confirma em 3 s, resposta só de quem pediu

    @pytest.mark.asyncio
    async def test_the_main_bot_is_never_the_one_that_plays(self):
        w = build(free_bots=())
        await premium(w)

        with pytest.raises(GenericError) as refusal:
            await run_on(w, channel=w.channel, playlist="Rock")

        assert "bot livre" in str(refusal.value) and not w.hub.manager.is_running(radio_id(1, GUILD))

    @pytest.mark.asyncio
    async def test_manage_guild_alone_is_not_enough_because_the_radio_deletes_messages_there(self):
        w = build(manage_messages=False)
        await premium(w)

        with pytest.raises(GenericError) as refusal:
            await run_on(w, channel=w.channel, playlist="Rock")

        assert "Gerenciar Mensagens" in str(refusal.value) and await w.hub.store.list_for_guild(GUILD) == []

    @pytest.mark.asyncio
    async def test_without_manage_guild_nothing_happens(self):
        w = build(manage_guild=False)
        await premium(w)

        with pytest.raises(GenericError):
            await run_on(w, channel=w.channel, playlist="Rock")

        assert await w.hub.store.list_for_guild(GUILD) == [] and w.ports.connects == 0

    @pytest.mark.asyncio
    async def test_without_premium_or_with_an_unknown_playlist_it_says_why(self):
        no_premium = build()
        no_playlist = build(playlist=False)
        await premium(no_playlist)

        with pytest.raises(GenericError) as first:
            await radio_cog.Radio.radio_on.callback(
                no_premium.cog, no_premium.inter, channel=no_premium.channel, playlist="x"
            )
        radio_cog.RADIO_COOLDOWN._cache.clear()
        with pytest.raises(GenericError) as second:
            await radio_cog.Radio.radio_on.callback(
                no_playlist.cog, no_playlist.inter, channel=no_playlist.channel, playlist="x"
            )

        assert "premium" in str(first.value) and "playlist" in str(second.value)

    @pytest.mark.asyncio
    async def test_a_bot_already_in_a_call_is_skipped(self):
        w = build()
        w.bots[1].get_guild(GUILD).me.voice = object()  # TOKEN_2 ocupado
        await premium(w)

        await run_on(w, channel=w.channel, playlist="Rock")

        assert w.hub.manager.is_running(radio_id(3, GUILD))

    @pytest.mark.asyncio
    async def test_a_radio_only_bot_comes_first(self):
        w = build()
        await premium(w)
        await w.hub.roles.add("TOKEN_3")

        await run_on(w, channel=w.channel, playlist="Rock")

        assert w.hub.manager.is_running(radio_id(3, GUILD))

    @pytest.mark.asyncio
    async def test_a_second_use_inside_ten_seconds_is_told_to_wait(self):
        w = build()
        await premium(w)
        await run_on(w, channel=w.channel, playlist="Rock")

        with pytest.raises(GenericError) as refusal:
            await run_on(w, channel=w.channel, playlist="Rock")

        assert "Aguarde" in str(refusal.value)

    @pytest.mark.asyncio
    async def test_the_database_going_down_is_a_friendly_error(self):
        w = build()
        await premium(w)
        w.db.fail = True

        with pytest.raises(GenericError):
            await run_on(w, channel=w.channel, playlist="Rock")


class TestDesligarEStatus:
    @pytest.mark.asyncio
    async def test_it_turns_the_radio_off_and_makes_the_bot_leave(self):
        w = build()
        await premium(w)
        await run_on(w, channel=w.channel, playlist="Rock")
        radio_cog.RADIO_COOLDOWN._cache.clear()
        player = SimpleNamespace(destroy=AsyncMock())
        w.bots[1].music.players[GUILD] = player

        await radio_cog.Radio.radio_off.callback(w.cog, w.inter, channel=w.channel)

        assert not w.hub.manager.is_running(radio_id(2, GUILD)) and player.destroy.await_count == 1
        assert (await w.hub.premium.fresh(GUILD)).active == 0 and "desligada" in reply(w)

    @pytest.mark.asyncio
    async def test_without_a_radio_there_it_says_so_and_without_manage_guild_it_refuses(self):
        w = build()
        await premium(w)

        with pytest.raises(GenericError) as nothing:
            await radio_cog.Radio.radio_off.callback(w.cog, w.inter, channel=w.channel)
        radio_cog.RADIO_COOLDOWN._cache.clear()
        w.inter.author.guild_permissions.manage_guild = False
        with pytest.raises(GenericError) as no_guild:
            await radio_cog.Radio.radio_off.callback(w.cog, w.inter, channel=w.channel)

        assert "Não há rádio" in str(nothing.value) and "Gerenciar Servidor" in str(no_guild.value)

    @pytest.mark.asyncio
    async def test_status_without_premium_shows_only_the_notice(self):
        w = build()

        await radio_cog.Radio.radio_status.callback(w.cog, w.inter)

        assert "premium" in reply(w)

    @pytest.mark.asyncio
    async def test_status_lists_the_radios_with_the_state_and_the_owner_pause(self):
        w = build()
        await premium(w)
        await run_on(w, channel=w.channel, playlist="Rock")
        radio_cog.RADIO_COOLDOWN._cache.clear()
        await w.hub.control.load()
        await w.hub.control.set_paused_all(True, by=1)

        await radio_cog.Radio.radio_status.callback(w.cog, w.inter)

        text = reply(w)
        assert "1 de 3" in text and f"<#{CHANNEL}>" in text and "tocando" in text and "pausadas pelo dono" in text

    @pytest.mark.asyncio
    async def test_status_with_no_radio_points_to_the_command(self):
        w = build()
        await premium(w)
        await w.hub.control.load()

        await radio_cog.Radio.radio_status.callback(w.cog, w.inter)

        assert "/radio ligar" in reply(w)


class TestBoot:
    @pytest.mark.asyncio
    async def test_the_main_bot_installs_the_discord_ports_and_resumes_once(self):
        w = build()
        w.hub.manager.ports_factory = None
        w.hub.manager.resume_all = AsyncMock(return_value=0)
        w.hub.manager.start_ticker = MagicMock()

        await w.cog.radio_boot()
        await w.cog.radio_boot()  # on_ready pode vir mais de uma vez
        await settle(w)  # a retomada roda em segundo plano, com nova tentativa

        assert w.hub.manager.ports_factory is not None and w.hub.manager.resume_all.await_count == 1
        w.hub.manager.start_ticker.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_reserve_bot_does_nothing_at_boot(self):
        w = build()
        w.hub.manager.ports_factory = None
        w.pool.servers.main_bot = lambda: w.bots[1]  # este cog não é o do principal

        await w.cog.radio_boot()

        assert w.hub.manager.ports_factory is None


class Radioplayer(PlayerVoiceMixin):
    """Só o que os ganchos do ``player_voice`` leem do player."""

    def __init__(self, radio_id_value, manager):
        self.radio_id = radio_id_value
        self.bot = SimpleNamespace(pool=SimpleNamespace(radio=SimpleNamespace(manager=manager)))
        self.members_timeout_task = None


class TestPlayerHooks:
    def test_a_radio_player_hands_presence_to_the_manager_instead_of_the_economy_mode(self):
        manager = MagicMock()
        player = Radioplayer("2:500", manager)

        player.start_members_timeout(check=True)
        player.start_members_timeout(check=False)

        assert [c.args for c in manager.note_presence.call_args_list] == [("2:500", True), ("2:500", False)]
        assert player.members_timeout_task is None  # nenhum members_timeout, nenhum destroy

    @pytest.mark.asyncio
    async def test_a_radio_player_hands_the_return_to_the_manager_never_reconnecting_by_itself(self):
        manager = MagicMock()
        player = Radioplayer("2:500", manager)

        await player.reconnect_voice_channel()

        manager.note_disconnect.assert_called_once_with("2:500")

    @pytest.mark.asyncio
    async def test_the_manager_turns_a_disconnect_into_the_backoff_and_a_presence_into_a_registration(self):
        w = build()
        await premium(w)
        await run_on(w, channel=w.channel, playlist="Rock")
        runner = w.hub.manager.runner(radio_id(2, GUILD))
        assert runner.phase is Phase.PLAYING

        w.hub.manager.note_presence(runner.radio_id, False)
        while w.hub.manager._tasks:
            await asyncio.gather(*list(w.hub.manager._tasks))
        assert runner.empty_since is not None  # a call esvaziou: o passo seguinte decide a pausa

        w.hub.manager.note_disconnect(runner.radio_id)
        while w.hub.manager._tasks:
            await asyncio.gather(*list(w.hub.manager._tasks))

        assert runner.phase is Phase.PLAYING and w.ports.sleeps[0] >= 4.0  # voltou, mas só depois do recuo
