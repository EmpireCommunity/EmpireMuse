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
from tests.cogs.radio_config import configure
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
    channel.name = "Música"
    channel.permissions_for.side_effect = lambda who: SimpleNamespace(
        view_channel=True, connect=True, speak=True, manage_messages=manage_messages
    )
    return channel


def make_bot(bot_id, identifier, channel, *, ready=True, in_voice=False, manage_guild=True):
    member = SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=manage_guild, administrator=False))
    guild = SimpleNamespace(
        id=GUILD,
        me=SimpleNamespace(voice=object() if in_voice else None),
        get_channel=lambda cid: channel,
        get_member=lambda uid: member,
        owner_id=0,
        voice_channels=[channel],
        stage_channels=[],
    )
    player_calls = AsyncMock()
    return SimpleNamespace(
        identifier=identifier,
        user=SimpleNamespace(id=bot_id, name=f"Bot {bot_id}"),
        bot_ready=ready,
        exclusive_guild_id=None,
        music=SimpleNamespace(players={}),
        get_guild=lambda gid: guild if gid == GUILD else None,
        destroy=player_calls,
    )


def build(
    *, owner=True, manage_guild=True, manage_messages=True, playlist=True, free_bots=(2, 3), request_channel=None
):
    db = FakeDb()
    channel = make_channel(manage_messages=manage_messages)
    bots = [
        make_bot(1, "TOKEN", channel, manage_guild=manage_guild),
        *(make_bot(n, f"TOKEN_{n}", channel, manage_guild=manage_guild) for n in free_bots),
    ]
    pool = SimpleNamespace(database=db, i18n=get_i18n(), get_all_bots=lambda: bots)
    pool.radio = RadioHub(pool)
    ports = FakePorts()
    pool.radio.manager.ports_factory = lambda radio: ports
    pool.servers = SimpleNamespace(
        main_bot=lambda: bots[0],
        jukebox=SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(channel=request_channel))),
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
    await w.hub.control.load()  # a pausa do dono lida do banco: sem documento, não pausado
    await w.hub.premium.grant(GUILD, by=1)


async def run_on(w, **kwargs):
    """``/radio setup`` (gravado direto) seguido de ``/radio on``. ``playlist=`` põe uma estação do servidor, ``station=`` uma estação ao vivo, ``bot=`` o bot fixo
    (o 2 por padrão) e ``configured=False`` pula o setup (rádio nunca configurada)."""
    channel = kwargs["channel"]
    live = kwargs.get("station")
    if kwargs.get("configured", True):
        await configure(
            w.hub,
            guild_id=GUILD,
            channel_id=channel.id,
            bot_id=kwargs.get("bot", 2),
            kind="live" if live else "custom",
            source={"station": live, "reserve": None} if live else kwargs.get("source"),
        )
    await radio_cog.Radio.radio_on.callback(w.cog, w.inter, channel=channel)
    await settle(w)


def reply(w):
    return w.inter.edit_original_message.await_args.kwargs["content"]


class TestBootFromCogLoad:
    @pytest.mark.asyncio
    async def test_a_cog_loaded_after_the_first_ready_boots_from_cog_load_and_only_once(self):  # CodeRabbit #180
        w = build()
        w.hub.manager.ports_factory = None  # o build já instala dublês: aqui o boot real é que instala
        w.cog.bot.is_ready = lambda: True
        w.cog.bot.loop = asyncio.get_running_loop()
        w.cog.bot.pool.servers.main_bot = lambda: w.cog.bot

        await w.cog.cog_load()
        await w.cog.cog_load()  # carregada de novo (reload): o guard de ports_factory impede subir duas vezes
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert w.hub.manager.ports_factory is not None
        installed = w.hub.manager.ports_factory
        await w.cog.radio_boot()  # e o on_ready depois também é inofensivo
        assert w.hub.manager.ports_factory is installed
        await w.hub.manager.close()

    @pytest.mark.asyncio
    async def test_unloading_the_cog_cancels_a_boot_that_is_still_running(self):  # CodeRabbit #180
        w = build()
        w.cog.bot.is_ready = lambda: True
        w.cog.bot.loop = asyncio.get_running_loop()
        hung = asyncio.Event()

        async def slow_boot():
            hung.set()
            await asyncio.sleep(3600)

        w.cog.radio_boot = slow_boot
        await w.cog.cog_load()
        await asyncio.wait_for(hung.wait(), 3)
        task = w.cog._boot_task

        w.cog.cog_unload()
        await asyncio.wait_for(
            asyncio.gather(task, return_exceptions=True), 2
        )  # com prazo: sem o cancelamento falha rápido

        assert task.cancelled() and w.cog._boot_task is None
        w.cog.cog_unload()  # idempotente: sem tarefa, nada a fazer

    @pytest.mark.asyncio
    async def test_a_cog_loaded_before_ready_waits_for_on_ready(self):
        w = build()
        w.hub.manager.ports_factory = None
        w.cog.bot.is_ready = lambda: False
        w.cog.bot.loop = asyncio.get_running_loop()

        await w.cog.cog_load()
        await asyncio.sleep(0)

        assert w.hub.manager.ports_factory is None and not w.hub.manager._tasks  # nada subiu: espera o on_ready


class TestLigar:
    @pytest.mark.asyncio
    async def test_it_turns_on_the_configured_radio_with_its_fixed_bot_and_starts_it(self):
        w = build()
        await premium(w)

        await run_on(w, channel=w.channel, playlist="rock", bot=3)

        assert w.hub.manager.is_running(radio_id(3, GUILD)) and w.ports.connects == 1
        assert f"<#{CHANNEL}>" in reply(w) and "<@3>" in reply(w)
        assert w.inter.edit_original_message.await_args.kwargs["allowed_mentions"].everyone is False
        assert w.inter.response.defer.await_args.kwargs == {
            "ephemeral": True
        }  # confirma em 3 s, resposta só de quem pediu

    @pytest.mark.asyncio
    async def test_a_channel_without_a_configured_radio_points_to_setup(self):
        w = build()
        await premium(w)

        with pytest.raises(GenericError) as refusal:
            await run_on(w, channel=w.channel, configured=False)

        assert "/radio setup" in str(refusal.value) and not w.hub.manager.is_running(radio_id(2, GUILD))

    @pytest.mark.asyncio
    async def test_a_radio_without_a_station_points_to_setup_and_stays_off(self):
        w = build()
        await premium(w)

        with pytest.raises(GenericError) as refusal:
            await run_on(w, channel=w.channel, source={})

        assert "/radio setup" in str(refusal.value)
        assert not (await w.hub.store.list_for_guild(GUILD))[0].enabled

    @pytest.mark.asyncio
    async def test_the_jukebox_channel_is_refused_in_the_command(self):  # V181-1: o chat do canal da Jukebox é dela
        w = build(request_channel=CHANNEL)
        await premium(w)

        with pytest.raises(GenericError):
            await run_on(w, channel=w.channel, playlist="rock")

        assert not w.hub.manager.is_running(radio_id(2, GUILD))

    @pytest.mark.asyncio
    async def test_manage_guild_alone_is_not_enough_because_the_radio_deletes_messages_there(self):
        w = build(manage_messages=False)
        await premium(w)

        with pytest.raises(GenericError) as refusal:
            await run_on(w, channel=w.channel, playlist="Rock")

        assert "Gerenciar Mensagens" in str(refusal.value)
        assert not (await w.hub.store.list_for_guild(GUILD))[0].enabled

    @pytest.mark.asyncio
    async def test_without_manage_guild_nothing_happens(self):
        w = build(manage_guild=False)
        await premium(w)

        with pytest.raises(GenericError):
            await run_on(w, channel=w.channel, playlist="Rock")

        assert not (await w.hub.store.list_for_guild(GUILD))[0].enabled and w.ports.connects == 0

    @pytest.mark.asyncio
    async def test_without_premium_it_says_why(self):
        w = build()
        await w.hub.control.load()

        with pytest.raises(GenericError) as refusal:
            await run_on(w, channel=w.channel, playlist="x")

        assert "premium" in str(refusal.value)

    @pytest.mark.asyncio
    async def test_the_fixed_bot_already_in_a_call_is_not_swapped_for_another(self):
        w = build()
        w.bots[1].get_guild(GUILD).me.voice = object()  # TOKEN_2, o bot da rádio, ocupado
        await premium(w)

        with pytest.raises(GenericError) as refusal:
            await run_on(w, channel=w.channel, playlist="Rock")

        assert "bot" in str(refusal.value).lower()
        assert not w.hub.manager.is_running(radio_id(2, GUILD)) and not w.hub.manager.is_running(radio_id(3, GUILD))

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
        await configure(w.hub, guild_id=GUILD, channel_id=w.channel.id)
        w.db.fail = True

        with pytest.raises(GenericError):
            await radio_cog.Radio.radio_on.callback(w.cog, w.inter, channel=w.channel)


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
