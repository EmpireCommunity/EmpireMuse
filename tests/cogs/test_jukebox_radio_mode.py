"""Modo rádio da Jukebox (T-434 J2a): o controlador ``utils/radio/jukebox_radio.py`` com fakes, relógio e espera injetados.

Cobre o §8 (itens 2, 3, 4, 5) e os testes de ameaça 11.1 (MJ1), 11.2 (m1), 11.3 (m2), 11.4 (m3) e 11.6 (m5/s1). Sem Discord nem Lavalink
reais; ``tr`` devolve a chave, então nenhuma asserção depende de texto.
"""

from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from empiremuse.rules.radio_mode import LIVE_RETRY_S, RETRY_RESET_S, SWITCH_COOLDOWN_S, VOTE_COOLDOWN_S, VOTE_TTL_S
from empiremuse.rules.radio_stations import Station
from empiremuse.utils.music import jukebox_ui
from empiremuse.utils.music.checks import can_act_as_dj
from empiremuse.utils.music.models import playback_steps
from empiremuse.utils.radio.jukebox_radio import JukeboxRadio, Refused

UUID = "8f2a1c3e-0000-4000-8000-000000000001"
OTHER = "8f2a1c3e-0000-4000-8000-000000000002"
HOSTILE = "@everyone <@&123> discord.gg/abc [x](https://evil.example) ‮evil"


def tr(key, **fields):
    return key


def station(uuid=UUID, name="Rádio Boa"):
    return Station(uuid, name, "https://stream.example/live", "MP3", 128, "Brasil")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Sleeper:
    """Espera controlada: cada ``sleep`` fica presa até o teste soltar (nada do relógio real)."""

    def __init__(self):
        self.calls: list[float] = []
        self.gates: list[asyncio.Event] = []

    async def __call__(self, seconds):
        self.calls.append(seconds)
        gate = asyncio.Event()
        self.gates.append(gate)
        await gate.wait()

    def release(self):
        for gate in self.gates:
            gate.set()


async def settle():
    for _ in range(30):
        await asyncio.sleep(0)


class Relay:
    def __init__(self):
        self.count = 0
        self.registered: list[tuple[str, str]] = []
        self.revoked: list[str] = []
        self.flowing = True
        self.reached_tokens: set[str] = set()
        self.limited_tokens: set[str] = set()
        self.on_title = {}

    async def ensure_started(self):
        return 1

    def register(self, url, *, codec, bitrate, on_title=None):
        self.count += 1
        token = f"tok{self.count}"
        self.registered.append((url, token))
        self.on_title[token] = on_title
        return token

    def local_url(self, token):
        return f"http://127.0.0.1:9/r/{token}"

    async def revoke(self, token):
        self.revoked.append(token)

    def expect_flow(self, token):
        return None

    async def wait_flowing(self, token, timeout):
        return self.flowing

    def reached(self, token):
        return token in self.reached_tokens

    def limited(self, token):
        return token in self.limited_tokens


class Node:
    identifier = "LOCAL"

    def __init__(self):
        self.loaded: list[str] = []

    async def get_tracks(self, url, **kwargs):
        self.loaded.append(url)
        assert kwargs.get("trusted_local") is True
        return [NS(info={"uri": url, "identifier": url, "sourceName": "http"}, identifier=url, title="", author="")]


def member(mid, channel, *, admin=False, deaf=False, bot=False):
    perms = NS(administrator=admin, manage_guild=False, manage_channels=False)
    voice = NS(channel=channel, self_deaf=deaf, deaf=False) if channel is not None else None
    return NS(id=mid, bot=bot, voice=voice, guild_permissions=perms, roles=[], mention=f"<@{mid}>")


class Player:
    def __init__(self, world, *, current=None, queue=(), creator=None, restrict=False):
        self.bot, self.guild = world.bot, world.guild
        self.queue = deque(queue)
        self.queue_autoplay = deque(["recomendação"])
        self.loop = "queue"
        self.current = current
        self.radio_mode = None
        self.radio_vote = None
        self.radio_lock = asyncio.Lock()
        self.radio_epoch = 0
        self.radio_switched_at = None
        self.radio_vote_expired = {}
        self.restrict_mode = restrict
        self.player_creator = creator
        self.dj = set()
        self.is_closing = False
        self.volume = 70
        self.node = NS(identifier="LOCAL")
        self.text_channel = NS(id=60, send=AsyncMock(side_effect=self._sent))
        self.messages = []
        self.tr = tr
        self.stopped = 0

    async def _sent(self, *args, **kwargs):
        message = NS(edit=AsyncMock(), args=args, kwargs=kwargs)
        self.messages.append(message)
        return message

    async def stop(self):
        self.stopped += 1
        self.current = None

    async def process_next(self):
        self.current = self.queue.popleft() if self.queue else None

    async def change_node(self, identifier):
        self.node = NS(identifier=identifier)

    async def update_stage_topic(self, reconnect=True):
        return None


class World:
    def __init__(self, monkeypatch, *, humans=1):
        self.clock, self.wall, self.sleeper = Clock(), Clock(), Sleeper()
        self.channel = NS(id=50, members=[], permissions_for=lambda m: NS(move_members=False))
        self.members = {}
        for i in range(humans):
            self.add(member(10 + i, self.channel))
        self.guild = NS(id=1, me=NS(voice=NS(channel=self.channel)), get_member=self.members.get)
        self.node = Node()
        self.bot = NS(
            user=NS(id=900),
            identifier="TOKEN",
            bot_ready=True,
            music=NS(players={}, nodes={"LOCAL": self.node}),
            get_guild=lambda gid: self.guild,
            get_data=AsyncMock(return_value={"djroles": []}),
        )
        self.pool = NS(get_all_bots=lambda: [self.bot], config={"VOTE_SKIP_AMOUNT": 3})
        self.relay = Relay()
        self.hub = NS(
            premium=NS(fresh=AsyncMock(return_value=object())),
            manager=NS(running_in=Mock(return_value=[])),
            health=NS(unavailable=Mock(return_value=False), record_exhausted=AsyncMock(return_value=False)),
            directory=NS(
                fresh_for_play=AsyncMock(side_effect=lambda uuid: station(uuid)),
                by_uuid_strict=AsyncMock(side_effect=lambda uuid: station(uuid)),
            ),
            relay=self.relay,
        )
        self.radio = JukeboxRadio(self.pool, self.hub, clock=self.clock, wall=self.wall, sleep=self.sleeper)
        self.idle = []

        async def go_idle(player):
            self.idle.append(player)
            await player.stop()

        monkeypatch.setattr(playback_steps, "go_idle", go_idle)

    def add(self, someone):
        self.members[someone.id] = someone
        self.channel.members.append(someone)
        return someone

    def player(self, **kwargs):
        player = Player(self, **kwargs)
        self.bot.music.players[self.guild.id] = player
        return player

    def inter(self, someone):
        return NS(guild_id=self.guild.id, author=someone, channel_id=7, channel=None)


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


# ---------------------------------------------------------------------- entrada (§8 item 4)


@pytest.mark.critical
@pytest.mark.asyncio
async def test_an_idle_empty_player_enters_radio_mode_without_a_vote(world):
    player = world.player()
    key, _ = await world.radio.request_live(world.inter(world.members[10]), UUID)
    assert key == "jukebox.radio.started"
    assert player.radio_mode is not None and player.radio_mode.uuid == UUID
    assert player.radio_vote is None and not player.messages
    assert world.node.loaded == [world.relay.local_url("tok1")]  # só o nó local, só a URL do relay


@pytest.mark.critical
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        (lambda w: setattr(w.hub.manager, "running_in", Mock(return_value=["24h"])), "jukebox.radio.has_24h"),
        (lambda w: setattr(w.hub.premium, "fresh", AsyncMock(return_value=None)), "radio.refused.no_premium"),
        (lambda w: w.bot.music.nodes.clear(), "jukebox.radio.no_local_node"),
        (lambda w: setattr(w.bot, "identifier", "TOKEN_RADIO_1"), "player.all_music_bots_are_in"),
    ],
)
async def test_entry_preconditions_refuse_without_touching_the_player_or_the_station_health(world, setup, expected):
    player = world.player(current="música", queue=["fila"])
    setup(world)
    with pytest.raises(Refused) as refused:
        await world.radio.request_live(world.inter(world.members[10]), UUID)
    assert refused.value.key == expected
    assert player.radio_mode is None and list(player.queue) == ["fila"] and player.current == "música"
    assert world.relay.registered == [] and world.hub.health.record_exhausted.await_count == 0


@pytest.mark.critical
@pytest.mark.asyncio
async def test_a_bot_playing_a_24h_radio_is_never_chosen(world):
    player = world.player()
    player.radio_id = "radio-24h"
    with pytest.raises(Refused) as refused:
        await world.radio.request_live(world.inter(world.members[10]), UUID)
    assert refused.value.key == "player.all_music_bots_are_in"
    assert player.radio_mode is None


# ---------------------------------------------------------------------- a porta com DJ real (11.3, m2)


@pytest.mark.critical
@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("self_promoted", False),
        ("creator", True),
        ("dj_role", True),
        ("move_members", True),
        ("manage_channels", True),
        ("manage_guild", True),
        ("admin", True),
    ],
)
def test_only_real_permissions_count_as_dj_and_player_dj_is_never_read(world, kind, expected):
    someone = member(10, world.channel, admin=kind == "admin")
    if kind in ("manage_channels", "manage_guild"):
        setattr(someone.guild_permissions, kind, True)
    player = Player(world, creator=10 if kind == "creator" else 99)
    player.dj = {10}  # a autopromoção de check_player_perm: nunca conta
    guild_data = {"djroles": ["777"] if kind == "dj_role" else []}
    if kind == "dj_role":
        someone.roles = [NS(id=777)]
    if kind == "move_members":
        world.channel.permissions_for = lambda m: NS(move_members=True)
    assert can_act_as_dj(someone, player, guild_data) is expected
    assert player.dj == {10}  # o predicado não promove ninguém


# ---------------------------------------------------------------------- votação (§8 itens 2 e 3; 11.4, 11.6)


async def open_member_vote(world, voters=3):
    world.members.clear()
    world.channel.members.clear()
    for i in range(voters):
        world.add(member(10 + i, world.channel))
    player = world.player(current="música", queue=["fila"], creator=99)
    key, _ = await world.radio.request_live(world.inter(world.members[10]), UUID)
    assert key == "jukebox.radio.vote.opened"
    await settle()  # o prazo da votação já está armado
    return player


@pytest.mark.critical
@pytest.mark.asyncio
async def test_a_member_opens_one_vote_and_nothing_changes_until_it_passes(world):
    player = await open_member_vote(world, voters=3)  # 3 humanos: precisa de 2
    vote = player.radio_vote
    assert vote is not None and vote.voters == {10} and vote.uuid == UUID
    assert player.radio_mode is None and list(player.queue) == ["fila"] and player.loop == "queue"
    with pytest.raises(Refused) as again:
        await world.radio.request_live(world.inter(world.members[11]), OTHER)
    assert again.value.key == "jukebox.radio.vote.running"  # um voto aberto por player
    key, _ = await world.radio.vote_click(world.inter(world.members[11]), vote.nonce)
    assert key == "jukebox.radio.vote.counted"
    assert player.radio_vote is None and player.radio_mode is not None
    assert not player.queue and not player.queue_autoplay and player.loop is False  # descartados só quando passou (G1)
    assert player.messages[0].edit.await_args.kwargs["components"] == []  # o botão some


@pytest.mark.critical
@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["stale_nonce", "duplicate", "outside_call", "deafened"])
async def test_votes_that_do_not_count(world, case):
    player = await open_member_vote(world, voters=3)
    vote = player.radio_vote
    nonce, voter = vote.nonce, world.members[11]
    expected = {
        "stale_nonce": "jukebox.radio.vote.over",
        "duplicate": "jukebox.radio.vote.already",
        "outside_call": "jukebox.radio.vote.over",  # fora da call não há player da call de quem clicou
        "deafened": "jukebox.radio.vote.not_eligible",
    }[case]
    if case == "stale_nonce":
        nonce = "00000000"
    elif case == "duplicate":
        voter = world.members[10]
    elif case == "outside_call":
        voter = member(77, NS(id=51, members=[]))
    else:
        voter.voice.self_deaf = True
    key, _ = await world.radio.vote_click(world.inter(voter), nonce)
    assert key == expected
    assert player.radio_vote is vote and len(vote.voters) == 1 and player.radio_mode is None


@pytest.mark.critical
@pytest.mark.asyncio
async def test_a_deafened_member_does_not_count_in_the_quorum(world):
    player = await open_member_vote(world, voters=3)
    world.members[12].voice.self_deaf = True  # 2 elegíveis: basta 1 voto, o do autor
    world.sleeper.release()
    await settle()
    assert player.radio_mode is not None  # recalculado no prazo: passou


@pytest.mark.critical
@pytest.mark.asyncio
async def test_the_vote_expires_after_60s_leaves_nothing_changed_and_blocks_the_author_for_the_cooldown(world):
    player = await open_member_vote(world, voters=3)
    assert world.sleeper.calls == [VOTE_TTL_S]
    world.sleeper.release()
    await settle()
    assert player.radio_vote is None and player.radio_mode is None and list(player.queue) == ["fila"]
    assert player.messages[0].edit.await_args.kwargs["components"] == []
    world.clock.t += VOTE_COOLDOWN_S - 1
    with pytest.raises(Refused) as refused:
        await world.radio.request_live(world.inter(world.members[10]), UUID)
    assert refused.value.key == "jukebox.radio.vote.cooldown"
    world.clock.t += 1
    key, _ = await world.radio.request_live(world.inter(world.members[11]), UUID)  # outro membro pode abrir
    assert key == "jukebox.radio.vote.opened"


@pytest.mark.critical
@pytest.mark.asyncio
async def test_two_concurrent_last_votes_execute_once(world):
    player = await open_member_vote(world, voters=4)  # 4 humanos: precisa de 2
    nonce = player.radio_vote.nonce
    results = await asyncio.gather(
        world.radio.vote_click(world.inter(world.members[11]), nonce),
        world.radio.vote_click(world.inter(world.members[12]), nonce),
    )
    assert sorted(key for key, _ in results) == ["jukebox.radio.vote.counted", "jukebox.radio.vote.over"]
    assert world.hub.directory.fresh_for_play.await_count == 1 and len(world.relay.registered) == 1


@pytest.mark.critical
@pytest.mark.asyncio
async def test_a_dj_transition_cancels_the_open_vote_and_closes_its_message(world):
    """Alicerce m2 + Crivo MI1: o DJ não é recusado por um voto aberto; a transição real anula o voto e a mensagem perde o botão."""
    player = await open_member_vote(world, voters=3)
    vote = player.radio_vote
    task = vote.task
    dj = world.add(member(20, world.channel, admin=True))
    key, _ = await world.radio.request_live(world.inter(dj), OTHER)
    await settle()
    assert key == "jukebox.radio.started" and player.radio_mode.uuid == OTHER
    assert player.radio_vote is None and task.cancelled()
    closed = player.messages[0].edit.await_args.kwargs
    assert closed["components"] == [] and closed["content"] == "jukebox.radio.vote.expired"
    key, _ = await world.radio.vote_click(world.inter(world.members[11]), vote.nonce)
    assert key == "jukebox.radio.vote.over"  # o botão velho não faz nada
    assert 10 not in player.radio_vote_expired  # anulado não é expirado: sem cooldown para o autor


@pytest.mark.critical
@pytest.mark.asyncio
async def test_a_passing_vote_redoes_the_preconditions(world):
    player = await open_member_vote(world, voters=3)
    world.hub.manager.running_in = Mock(return_value=["24h"])  # uma 24 h entrou na call enquanto votavam
    await world.radio.vote_click(world.inter(world.members[11]), player.radio_vote.nonce)
    assert player.radio_mode is None and list(player.queue) == ["fila"]
    assert world.relay.registered == []


@pytest.mark.critical
@pytest.mark.asyncio
async def test_the_vote_message_mentions_only_the_author_and_neutralizes_the_station_name(world):
    world.hub.directory.by_uuid_strict = AsyncMock(return_value=station(name=HOSTILE))
    player = await open_member_vote(world, voters=3)
    mentions = player.messages[0].kwargs["allowed_mentions"]
    assert [u.id for u in mentions.users] == [10] and mentions.roles is False and mentions.everyone is False
    vote = player.radio_vote
    world.radio._tr = lambda player: lambda key, **f: f.get("station", key)  # o texto mostra só o nome da estação
    payload = world.radio._vote_payload(player, vote)
    content = payload["content"]
    assert "@everyone" not in content and "<@&" not in content
    assert "discord.gg" not in content and "](" not in content and "‮" not in content
    assert vote.nonce in payload["components"][0].custom_id and UUID not in payload["components"][0].custom_id


# ---------------------------------------------------------------------- troca (s1) e saída (A4)


@pytest.mark.critical
@pytest.mark.asyncio
async def test_switching_radio_waits_the_cooldown_even_for_a_dj(world):
    player = world.player(creator=10)
    await world.radio.request_live(world.inter(world.members[10]), UUID)
    world.clock.t += SWITCH_COOLDOWN_S - 1
    with pytest.raises(Refused) as refused:
        await world.radio.request_live(world.inter(world.members[10]), OTHER)
    assert refused.value.key == "jukebox.radio.switch_wait"
    world.clock.t += 1
    await world.radio.request_live(world.inter(world.members[10]), OTHER)
    assert player.radio_mode.uuid == OTHER and world.relay.revoked == ["tok1"]  # o token antigo foi revogado


@pytest.mark.critical
@pytest.mark.asyncio
@pytest.mark.parametrize("is_dj", [True, False])
async def test_a_music_request_in_radio_mode_leaves_directly_for_a_dj_and_votes_for_a_member(world, is_dj):
    world.add(member(11, world.channel))
    world.add(member(12, world.channel))
    player = world.player(creator=10 if is_dj else 99)
    await world.radio.request_live(world.inter(world.members[10]), UUID)  # ocioso: entra direto
    if is_dj:
        await world.radio.leave_for_music(world.inter(world.members[10]), player)
        assert player.radio_mode is None and world.relay.revoked == ["tok1"]
        return
    with pytest.raises(Refused) as refused:
        await world.radio.leave_for_music(world.inter(world.members[10]), player)
    assert refused.value.key == "jukebox.radio.vote.opened"
    assert player.radio_mode is not None and player.radio_vote.target.value == "leave"


# ---------------------------------------------------------------------- queda (§8 item 5, P11)


async def on_air(world):
    player = world.player()
    await world.radio.request_live(world.inter(world.members[10]), UUID)
    return player


async def run_retries(world):
    for _ in LIVE_RETRY_S:
        await settle()
        world.sleeper.release()
    await settle()


@pytest.mark.critical
@pytest.mark.asyncio
async def test_a_drop_retries_at_2_4_8_then_the_player_goes_idle_with_one_message(world):
    player = await on_air(world)
    world.relay.flowing = False  # nenhuma nova tentativa abre
    world.relay.reached_tokens = {"tok1", "tok2", "tok3", "tok4"}
    world.radio.on_stream_lost(player)
    world.radio.on_stream_lost(player)  # exceção e fim do mesmo fluxo: uma volta só
    await run_retries(world)
    assert world.sleeper.calls == list(LIVE_RETRY_S)
    assert player.radio_mode is None and world.idle == [player]
    assert len(player.messages) == 1 and player.messages[0].args == ("jukebox.radio.dropped",)
    assert world.hub.health.record_exhausted.await_count == 1  # Q5: uma vez, ao esgotar
    assert set(world.relay.revoked) == {"tok1", "tok2", "tok3", "tok4"}


@pytest.mark.critical
@pytest.mark.asyncio
async def test_a_relay_policy_refusal_does_not_count_against_the_station(world):
    player = await on_air(world)
    world.relay.flowing = False
    world.relay.limited_tokens = {"tok1"}
    world.radio.on_stream_lost(player)
    await run_retries(world)
    assert player.radio_mode is None
    assert world.hub.health.record_exhausted.await_count == 0


@pytest.mark.critical
@pytest.mark.asyncio
async def test_staying_on_air_resets_the_retry_count(world):
    player = await on_air(world)
    player.radio_mode.attempt = len(LIVE_RETRY_S)
    world.clock.t += RETRY_RESET_S
    world.radio.on_stream_lost(player)
    await settle()
    assert world.sleeper.calls == [LIVE_RETRY_S[0]]
    world.sleeper.release()
    await settle()
    assert player.radio_mode is not None and player.radio_mode.token == "tok2"


@pytest.mark.critical
@pytest.mark.asyncio
async def test_a_failed_first_open_leaves_the_player_idle_and_refuses(world):
    player = world.player()
    world.relay.flowing = False
    world.relay.reached_tokens = {"tok1"}
    with pytest.raises(Refused) as refused:
        await world.radio.request_live(world.inter(world.members[10]), UUID)
    assert refused.value.key == "radio.refused.station_down"
    assert player.radio_mode is None and world.relay.revoked == ["tok1"] and world.idle == [player]


@pytest.mark.critical
@pytest.mark.asyncio
async def test_destroying_the_player_revokes_the_token_and_cancels_the_vote(world):
    player = await open_member_vote(world, voters=3)
    task = player.radio_vote.task
    await world.radio.forget(player)
    await settle()
    assert player.radio_vote is None and task.cancelled()
    assert player.messages[0].edit.await_args.kwargs["components"] == []  # a mensagem pública perde o botão (Crivo MI1)


# ---------------------------------------------------------------------- MJ1 e m1 na faixa ao vivo (11.1, 11.2)


@pytest.mark.critical
@pytest.mark.asyncio
async def test_the_live_track_carries_no_local_url_and_no_live_link_or_mention(world):
    world.hub.directory.fresh_for_play = AsyncMock(return_value=station(name=HOSTILE))
    player = await on_air(world)
    track = player.radio_mode.track
    assert track.info["uri"] == "" and track.info["identifier"] == "" and track.identifier == ""
    world.relay.on_title["tok1"](HOSTILE)  # o título ICY chega do relay
    for text in (track.title, track.author, player.radio_mode.title):
        assert "@everyone" not in text and "<@&" not in text and "discord.gg" not in text and "](" not in text
        assert "127.0.0.1" not in text and "/r/" not in text and "‮" not in text
    lines = "\n".join(jukebox_ui.now_playing_lines(tr, world.radio.view(player), world.wall.t + 120))
    assert "@everyone" not in lines and "discord.gg" not in lines and "](" not in lines
    dropped = jukebox_ui.dropped_text(lambda key, **f: f["station"], player.radio_mode.name, "<@10>")
    assert "@everyone" not in dropped and "discord.gg" not in dropped


# ---------------------------------------------------------------------- estrela do player, retomar e player novo


@pytest.mark.critical
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("ADDED", "radio.favorites.mine.added"),
        ("REMOVED", "radio.favorites.mine.removed"),
        ("FULL", "radio.favorites.mine.full"),
        ("not_found", "radio.live.not_found"),
        ("unavailable", "radio.owner.unavailable"),
    ],
)
async def test_the_player_star_in_radio_mode_toggles_my_live_favorite_by_the_players_uuid(world, outcome, expected):
    from empiremuse.utils.radio.custom_favorites import Toggled
    from empiremuse.utils.radio.favorites import FavoriteNotFound
    from empiremuse.utils.radio.premium import RadioUnavailable

    player = await on_air(world)
    effect = {"not_found": FavoriteNotFound(), "unavailable": RadioUnavailable()}.get(outcome)
    toggle = AsyncMock(side_effect=effect) if effect else AsyncMock(return_value=Toggled[outcome])
    world.hub.user_favorites = NS(toggle_station=toggle)
    clicker = world.inter(world.members[10])
    key, _ = await world.radio.toggle_favorite(clicker, player)
    assert key == expected
    toggle.assert_awaited_once_with(10, UUID)  # o uuid vem do player, a identidade da interação


@pytest.mark.critical
@pytest.mark.asyncio
async def test_resuming_in_radio_mode_reopens_the_live_stream_with_a_new_token(world):
    player = await on_air(world)
    await world.radio.resume_live(player)
    assert world.relay.revoked == ["tok1"] and player.radio_mode.token == "tok2"


@pytest.mark.critical
@pytest.mark.asyncio
async def test_resuming_a_dead_stream_falls_back_to_the_drop_retries(world):
    player = await on_air(world)
    world.relay.flowing = False
    await world.radio.resume_live(player)
    await settle()
    assert world.sleeper.calls == [LIVE_RETRY_S[0]]


@pytest.mark.critical
@pytest.mark.asyncio
async def test_a_free_bot_gets_a_new_player_on_the_local_node_and_the_requester_is_its_creator(world):
    world.guild.me.voice = None  # bot livre, fora de call

    async def create_player(**kwargs):
        assert kwargs["node"] is world.node
        player = Player(world, creator=kwargs["inter"].author.id)
        player.is_connected = False
        world.bot.music.players[world.guild.id] = player
        return player

    async def do_connect(inter, **kwargs):
        world.bot.music.players[
            world.guild.id
        ].channel_id = world.channel.id  # o ``connect`` do wavelink grava a call na hora

    cog = NS(create_player=AsyncMock(side_effect=create_player), do_connect=AsyncMock(side_effect=do_connect))
    world.bot.get_cog = lambda name: cog
    world.bot.get_channel = lambda cid: None
    world.bot.get_data = AsyncMock(return_value={"djroles": [], "check_other_bots_in_vc": False})
    key, _ = await world.radio.request_live(world.inter(world.members[10]), UUID)
    assert key == "jukebox.radio.started"
    cog.do_connect.assert_awaited_once()
    assert world.bot.music.players[world.guild.id].player_creator == 10
