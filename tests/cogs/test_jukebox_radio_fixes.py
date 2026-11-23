"""Modo rádio da Jukebox: um teste de regressão por caminho achado na revisão do PR 270 (T-J2A-FIX).

Crivo (MA1, MI1-MI3), Sentinela (Minor 1-4, S1-S7) e Alicerce (M1, m1-m5). Só fakes, relógio e espera injetados; ``tr`` devolve a chave.
"""

from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, Mock

import disnake
import pytest

import empiremuse.wavelink as wavelink
from empiremuse.music_cog import jukebox_steps, radio_guard
from empiremuse.music_cog.nodes import NodesMixin
from empiremuse.rules.radio_mode import LIVE_RETRY_S, SWITCH_COOLDOWN_S, Decision
from empiremuse.rules.voice_status import voice_status_value
from empiremuse.utils.jukebox_panel import entry_for
from empiremuse.utils.music.converters import md_link
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.music.live_mark import is_live, mark_live
from empiremuse.utils.music.models import event_steps, playback_steps
from empiremuse.utils.music.models.player import LavalinkPlayer
from empiremuse.utils.music.models.tracks import LavalinkTrack
from empiremuse.utils.radio.jukebox_radio import RadioMode, Refused
from tests.cogs.test_jukebox_radio_mode import (
    HOSTILE,
    OTHER,
    UUID,
    World,
    member,
    on_air,
    run_retries,
    settle,
    station,
)
from tests.support.formato_sessao import gravar
from tests.support.formatos_reais import cog_de_sessoes, faixa_youtube, jogador_falso

RELAY_URL = "http://127.0.0.1:9/r/tok1"


def tr(key, **fields):
    return key


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


def live_track(n=0):
    track = faixa_youtube(n)
    track.info["uri"] = RELAY_URL  # o que importa: nada disto pode ser gravado nem reenviado
    mark_live(track)
    return track


# ---------------------------------------------------------------------- MA1 / Minor 2: a marca sobrevive a ``radio_mode = None``


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_a_live_track_left_in_the_queue_after_radio_mode_ended_is_never_saved(tmp_path, monkeypatch):
    """Crivo MA1 (a): ⏮ em R deixava a faixa ao vivo na fila; ``save_info`` a gravava com o ``track.id`` (ficha do relay)."""
    monkeypatch.setenv("EMPIREMUSE_HOME", str(tmp_path))
    stale = live_track()
    player = jogador_falso(atual=faixa_youtube(1), fila=[stale], tocadas=[stale], falhas=[stale], radio_mode=None)
    data = await gravar(cog_de_sessoes(), player)
    assert "127.0.0.1" not in repr(data) and "/r/" not in repr(data)
    assert data["queue"] and all(t.get("uri") != RELAY_URL for t in data["queue"])  # a música atual fica


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_a_stale_live_track_popped_from_the_queue_never_plays_again():
    player = NS(queue=deque([live_track()]), radio_mode=None, radio_id=None, process_next=AsyncMock())
    req = NS(track=None)
    assert await playback_steps._pn_next_track(player, req) is True
    assert req.track is None and not player.queue
    player.process_next.assert_awaited_once()


@pytest.mark.critical
@pytest.mark.parametrize(("live", "expected"), [(True, ""), (False, "https://www.youtube.com/results?search_query=x")])
def test_the_live_track_has_no_search_link_in_any_skin(live, expected):
    """Alicerce m4 / Crivo S2: as skins usam ``uri or search_uri``; a estação ao vivo não vira busca do YouTube."""
    track = NS(info={"extra": {}}, title="x")
    if live:
        mark_live(track)
    assert LavalinkTrack.search_uri.fget(track) == expected


# ---------------------------------------------------------------------- troca de nó em R (Minor 2, Alicerce m1)


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_a_node_change_never_resends_the_live_track_and_hands_over_to_the_controller(monkeypatch):
    """``/change_node``, perda do nó local e o recuo do ``update_player`` passam todos pelo ``change_node`` do player."""
    sent = []

    async def base_change_node(self, identifier=None):
        sent.append((identifier, self.current, self.current_encoded))

    monkeypatch.setattr(wavelink.Player, "change_node", base_change_node)
    player = LavalinkPlayer.__new__(LavalinkPlayer)
    mode = object()
    jukebox = NS(on_stream_lost=Mock())
    player.current, player.current_encoded, player.radio_mode = live_track(), "encoded-with-token", mode
    player.bot = NS(pool=NS(radio=NS(jukebox=jukebox)))
    await LavalinkPlayer.change_node(player, "REMOTE")
    assert sent == [("REMOTE", None, None)]
    jukebox.on_stream_lost.assert_called_once_with(player, mode)


@pytest.mark.critical
@pytest.mark.asyncio
async def test_the_change_node_command_is_refused_in_radio_mode():
    player = NS(radio_mode=object(), tr=tr, node=NS(identifier="LOCAL"), change_node=AsyncMock())
    bot = NS(music=NS(nodes={"REMOTE": object()}, players={1: player}))
    inter = NS(music_bot=bot, guild_id=1, response=NS(defer=AsyncMock()))
    with pytest.raises(GenericError) as error:
        await NodesMixin.change_node.callback(NS(), inter, "REMOTE")
    assert error.value.text == "jukebox.radio.no_change_node"
    player.change_node.assert_not_awaited()


def record_order(player, calls):
    stop, change = player.stop, player.change_node

    async def stop_then():
        calls.append("stop")
        await stop()

    async def change_then(identifier):
        calls.append(("change_node", identifier))
        await change(identifier)

    player.stop, player.change_node = stop_then, change_then


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_entering_radio_mode_stops_the_song_before_moving_to_the_local_node(world):
    player = world.player(current="música", creator=10)
    player.node = NS(identifier="REMOTE")
    calls = []
    record_order(player, calls)
    await world.radio.request_live(world.inter(world.members[10]), UUID)
    assert calls[:2] == ["stop", ("change_node", "LOCAL")]


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_a_retry_after_the_player_left_the_local_node_reopens_on_it(world):
    player = await on_air(world)
    player.node = NS(identifier="REMOTE")  # a perda do nó local levou o player a outro nó
    calls = []
    record_order(player, calls)
    world.radio.on_stream_lost(player)
    await settle()
    world.sleeper.release()
    await settle()
    assert player.node.identifier == "LOCAL" and player.radio_mode.token == "tok2"
    assert ("change_node", "LOCAL") in calls and world.node.loaded[-1] == world.relay.local_url("tok2")


# ---------------------------------------------------------------------- o player passa o modo que viu (Alicerce M1)


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_a_late_release_for_an_older_mode_never_drops_the_new_radio(world):
    player = await on_air(world)
    old = player.radio_mode
    world.clock.t += SWITCH_COOLDOWN_S
    await world.radio.request_live(world.inter(world.members[10]), OTHER)
    assert await world.radio.release(player, old) is False
    assert player.radio_mode.uuid == OTHER and "tok2" not in world.relay.revoked


@pytest.mark.critical
@pytest.mark.asyncio
async def test_the_real_release_leaves_radio_mode_and_revokes_the_token(world):
    player = await on_air(world)
    assert await world.radio.release(player, player.radio_mode) is True
    assert player.radio_mode is None and world.relay.revoked == ["tok1"]


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_next_track_does_nothing_while_the_radio_is_still_opening():
    """O ``process_next`` do ``/play`` concorrente durante a abertura não tira nada da fila nem pede ``release``."""
    opening = RadioMode(uuid="u", name="R", country="BR", starter_id=1, started_at=0.0)  # ``track`` ainda é None
    jukebox = NS(release=AsyncMock())
    player = NS(queue=deque(["música"]), radio_mode=opening, radio_id=None, bot=NS(pool=NS(radio=NS(jukebox=jukebox))))
    assert await playback_steps._pn_next_track(player, NS(track=None)) is True
    assert list(player.queue) == ["música"]
    jukebox.release.assert_not_awaited()


# ---------------------------------------------------------------------- queda presa à faixa de agora (Crivo MI2, Sentinela S7)


def event_world(sent):
    jukebox = NS(on_stream_lost=Mock())
    node = NS(identifier="LOCAL")
    mode = RadioMode(uuid="u", name="R", country="BR", starter_id=1, started_at=0.0, track=NS(id="new"))
    player = NS(radio_mode=mode, node=node, bot=NS(pool=NS(radio=NS(jukebox=jukebox))))
    event = wavelink.TrackEnd.__new__(wavelink.TrackEnd)
    event.node, event.reason, event.track = node, "FINISHED", sent
    return player, event, jukebox


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.parametrize(
    ("sent", "lost"), [("old", False), ({"encoded": "old"}, False), ("new", True), ({"encoded": "new"}, True)]
)
def test_a_late_end_of_the_old_stream_never_counts_as_a_drop_of_the_new_one(sent, lost):
    player, event, jukebox = event_world(sent)
    assert event_steps._he_jukebox_radio(player, event) is True  # consumido nos dois casos: nunca o caminho de música
    assert jukebox.on_stream_lost.call_count == int(lost)


@pytest.mark.critical
@pytest.mark.asyncio
async def test_a_drop_reported_for_an_older_mode_is_ignored(world):
    player = await on_air(world)
    world.radio.on_stream_lost(player, object())
    await settle()
    assert world.sleeper.calls == [] and player.radio_mode.token == "tok1"


# ---------------------------------------------------------------------- quem pediu está na call do player (Sentinela Minor 3)


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_a_request_from_another_call_on_the_same_bot_is_refused_and_changes_nothing(world):
    # o bot parecia livre quando o pedido chegou (fora de call), mas o pedido de outra call já o levou para a dela antes do lock
    world.guild.me.voice = None
    player = world.player(current="música", creator=99)
    player.channel_id = world.channel.id
    outsider = world.add(member(30, NS(id=51, members=[]), admin=True))
    with pytest.raises(Refused) as refused:
        await world.radio.request_live(world.inter(outsider), UUID)
    assert refused.value.key == "jukebox.radio.other_call"
    assert player.radio_mode is None and player.current == "música" and world.relay.registered == []


# ---------------------------------------------------------------------- caminhos de risco sem teste (Crivo MI3)


async def leave_vote(world, voters=3):
    world.members.clear()
    world.channel.members.clear()
    for i in range(voters):
        world.add(member(10 + i, world.channel))
    player = world.player(creator=99)
    await world.radio.request_live(world.inter(world.members[10]), UUID)  # ocioso: entra direto
    with pytest.raises(Refused) as refused:
        await world.radio.leave_for_music(world.inter(world.members[10]), player)
    assert refused.value.key == "jukebox.radio.vote.opened"
    assert refused.value.fields == {"channel": "<#60>", "seconds": 60}
    return player


@pytest.mark.critical
@pytest.mark.asyncio
async def test_a_passing_leave_vote_stops_the_radio_and_mentions_only_the_author(world):
    player = await leave_vote(world)
    key, _ = await world.radio.vote_click(world.inter(world.members[11]), player.radio_vote.nonce)
    assert key == "jukebox.radio.vote.counted"
    assert player.radio_mode is None and world.idle == [player] and world.relay.revoked == ["tok1"]
    closed = player.messages[0].edit.await_args.kwargs
    assert closed["content"] == "jukebox.radio.vote.passed_leave" and closed["components"] == []
    assert [u.id for u in closed["allowed_mentions"].users] == [10]


@pytest.mark.critical
@pytest.mark.asyncio
async def test_a_counted_vote_that_does_not_pass_updates_the_message_and_keeps_waiting(world):
    player = await leave_vote(world, voters=5)  # 5 humanos: precisa de 3
    vote = player.radio_vote
    key, _ = await world.radio.vote_click(world.inter(world.members[11]), vote.nonce)
    assert key == "jukebox.radio.vote.counted" and player.radio_vote is vote and player.radio_mode is not None
    edited = player.messages[0].edit.await_args.kwargs
    assert edited["components"] and vote.nonce in edited["components"][0].custom_id
    assert world.radio.view(player).vote.votes == 2


@pytest.mark.critical
@pytest.mark.asyncio
async def test_losing_premium_during_a_retry_stops_without_counting_against_the_station(world):
    player = await on_air(world)
    world.hub.premium.fresh = AsyncMock(return_value=None)  # Q4: o premium acabou com a rádio no ar
    world.radio.on_stream_lost(player)
    await run_retries(world)
    assert player.radio_mode is None and world.sleeper.calls == [LIVE_RETRY_S[0]]
    assert len(world.relay.registered) == 1  # nenhuma ficha nova sem premium
    assert world.hub.health.record_exhausted.await_count == 0


@pytest.mark.critical
@pytest.mark.asyncio
async def test_a_non_station_failure_while_opening_cleans_up_and_refuses(world):
    player = world.player(current="música", creator=10)
    player.node = NS(identifier="REMOTE")
    player.change_node = AsyncMock(side_effect=RuntimeError("lavalink"))
    with pytest.raises(Refused) as refused:
        await world.radio.request_live(world.inter(world.members[10]), UUID)
    assert refused.value.key == "radio.owner.unavailable"
    assert player.radio_mode is None and world.idle == [player] and world.relay.registered == []
    assert world.hub.health.record_exhausted.await_count == 0


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_destroying_the_player_while_the_radio_opens_never_idles_a_closed_player(world):
    """Alicerce m5: ``forget`` (sem lock) no meio do ``_start``; a abertura não chama ``go_idle`` num player fechado."""
    player = world.player(creator=10)
    player.node = NS(identifier="REMOTE")

    async def destroyed_while_moving(identifier):
        player.is_closing = True
        await world.radio.forget(player)

    player.change_node = destroyed_while_moving
    with pytest.raises(Refused):
        await world.radio.request_live(world.inter(world.members[10]), UUID)
    assert world.idle == [] and player.radio_mode is None and world.relay.registered == []


@pytest.mark.critical
@pytest.mark.asyncio
async def test_go_idle_does_nothing_on_a_closed_player():
    player = NS(is_closing=True, stop=AsyncMock())
    await playback_steps.go_idle(player)
    player.stop.assert_not_awaited()


@pytest.mark.critical
@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["enter", "leave"])
async def test_restricted_mode_refuses_a_member_in_the_controller(world, surface):
    world.add(member(11, world.channel))
    player = world.player(current="música", creator=10, restrict=True)
    if surface == "leave":
        await world.radio.request_live(world.inter(world.members[10]), UUID)  # o criador entra em R
        call = world.radio.leave_for_music(world.inter(world.members[11]), player)
    else:
        call = world.radio.request_live(world.inter(world.members[11]), UUID)
    with pytest.raises(Refused) as refused:
        await call
    assert refused.value.key == "jukebox.radio.restricted" and player.radio_vote is None


@pytest.mark.critical
@pytest.mark.asyncio
async def test_a_deafened_author_cannot_open_a_vote(world):
    """Sentinela S6: quem não vota também não abre voto (o mesmo ``voter_ok``), recusado antes do diretório (S2)."""
    world.add(member(11, world.channel))
    world.add(member(12, world.channel))
    world.player(current="música", creator=99)
    world.members[10].voice.self_deaf = True
    with pytest.raises(Refused) as refused:
        await world.radio.request_live(world.inter(world.members[10]), UUID)
    assert refused.value.key == "jukebox.radio.vote.not_eligible"
    world.hub.directory.by_uuid_strict.assert_not_awaited()


@pytest.mark.critical
@pytest.mark.asyncio
async def test_a_failing_vote_effect_still_closes_the_message(world):
    """Sentinela S1: banco fora na passagem do voto; a mensagem fecha mesmo assim, sem botão."""
    from empiremuse.utils.radio.premium import RadioUnavailable

    player = await leave_vote(world)
    world.radio._to_idle = AsyncMock(side_effect=RadioUnavailable())
    await world.radio.vote_click(world.inter(world.members[11]), player.radio_vote.nonce)
    closed = player.messages[0].edit.await_args.kwargs
    assert closed["content"] == "jukebox.radio.vote.failed" and closed["components"] == []


# ---------------------------------------------------------------------- a visão imutável (Alicerce m3, §1.5.1)


@pytest.mark.critical
@pytest.mark.asyncio
async def test_the_view_has_no_token_nor_local_url_and_follows_the_quorum(world):
    assert world.radio.view(world.player(creator=99)) is None
    player = await leave_vote(world)
    view = world.radio.view(player)
    assert "tok1" not in repr(view) and "127.0.0.1" not in repr(view) and "/r/" not in repr(view)
    assert (view.vote.votes, view.vote.needed) == (1, 2)
    world.channel.members.remove(world.members[12])  # alguém saiu da call
    view = world.radio.view(player)
    assert (view.vote.votes, view.vote.needed) == (1, 1)


# ---------------------------------------------------------------------- 11.1 (MJ1) nos destinos que faltavam (Sentinela Minor 4)


@pytest.mark.critical
@pytest.mark.asyncio
async def test_a_hostile_station_and_title_stay_inert_in_skins_voice_status_and_the_panel(world):
    world.hub.directory.fresh_for_play = AsyncMock(return_value=station(name=HOSTILE))
    world.node.get_tracks = AsyncMock(side_effect=lambda url, **kw: [faixa_youtube(0)])
    player = await on_air(world)
    world.relay.on_title["tok1"](HOSTILE)
    track = player.radio_mode.track
    assert is_live(track)
    skin = md_link(track.single_title, track.uri or track.search_uri)  # o título de todas as skins de hoje
    status = voice_status_value(track.single_title) + voice_status_value(track.authors_string)
    voice = NS(
        name="call", guild=NS(default_role=None), permissions_for=lambda role: NS(view_channel=True, connect=True)
    )
    player.guild.me.voice = NS(channel=voice)
    player.current, player.paused = track, False
    entry = entry_for(NS(user=NS(display_name="bot", id=1)), player)
    panel = md_link(entry.title, entry.url)
    assert entry.url is None
    for text in (skin, status, panel):
        assert "@everyone" not in text and "<@&" not in text and "](" not in text
        assert "discord.gg/" not in text and "127.0.0.1" not in text and "‮" not in text


# ---------------------------------------------------------------------- o passo de confirmação do pedido em R (Bússola §2, desvio 4)


def confirm_world(preview, *, radio=True):
    jukebox = NS(
        leave_preview=(
            AsyncMock(side_effect=preview) if isinstance(preview, Exception) else AsyncMock(return_value=preview)
        ),
        view=lambda player: NS(name="Rádio"),
        leave_for_music=AsyncMock(),
    )
    player = NS(radio_mode=object() if radio else None, tr=tr, guild=NS(get_member=lambda i: None))
    sent = NS(edit=AsyncMock())
    inter = NS(id=555, author=NS(id=10), message=NS(reply=AsyncMock(return_value=sent)))  # prefixo: resposta à mensagem
    return NS(radio=NS(jukebox=jukebox)), player, inter, sent


def click(choice, author=10):
    return NS(
        data=NS(custom_id=f"jukebox_radio_leave:555:{choice}"),
        author=NS(id=author),
        response=NS(edit_message=AsyncMock()),
        edit_original_message=AsyncMock(),
    )


def answered(answer):
    async def wait_for(event, *, timeout, check):
        assert event == "button_click" and timeout == radio_guard.CONFIRM_TIMEOUT_S
        assert not check(click("go", author=99))  # só o autor responde [s3]
        if answer is None:
            raise asyncio.TimeoutError
        assert check(answer)
        return answer

    return wait_for


@pytest.mark.critical
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("preview", "choice", "goes_on", "leaves", "closing"),
    [
        (Decision.DIRECT, "go", True, True, "jukebox.radio.leave.stopped"),
        (Decision.VOTE, "vote", False, True, "jukebox.radio.vote.opened"),
        (Decision.DIRECT, "cancel", False, False, "jukebox.radio.leave.cancelled"),
        (Decision.DIRECT, None, False, False, "jukebox.radio.leave.timeout"),
    ],
)
async def test_the_confirmation_step_redoes_the_gate_on_the_click(preview, choice, goes_on, leaves, closing):
    pool, player, inter, sent = confirm_world(preview)
    if preview is Decision.VOTE:
        pool.radio.jukebox.leave_for_music.side_effect = Refused("jukebox.radio.vote.opened")
    answer = click(choice) if choice else None
    result = await radio_guard.confirm_leave_radio(pool, None, inter, player, wait_for=answered(answer))
    assert result is goes_on
    assert pool.radio.jukebox.leave_for_music.await_count == int(leaves)
    buttons = inter.message.reply.await_args.kwargs["components"]
    assert [b.custom_id.rsplit(":", 1)[-1] for b in buttons] == ["vote" if preview is Decision.VOTE else "go", "cancel"]
    assert all(UUID not in b.custom_id for b in buttons)  # o alvo nunca vai no botão
    edit = sent.edit if answer is None else answer.edit_original_message
    assert edit.await_args.kwargs == {"content": closing, "components": []}


@pytest.mark.critical
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refusal", ["jukebox.radio.restricted", "jukebox.radio.vote.running", "jukebox.radio.vote.cooldown"]
)
async def test_direct_refusals_skip_the_confirmation(refusal):
    pool, player, inter, _ = confirm_world(Refused(refusal))
    with pytest.raises(GenericError) as error:
        await radio_guard.confirm_leave_radio(pool, None, inter, player, wait_for=answered(None))
    assert error.value.text == refusal
    inter.message.reply.assert_not_awaited()


@pytest.mark.critical
@pytest.mark.asyncio
async def test_outside_radio_mode_the_request_goes_on_untouched():
    pool, player, inter, _ = confirm_world(Decision.DIRECT, radio=False)
    assert await radio_guard.confirm_leave_radio(pool, None, inter, player, wait_for=answered(None)) is True
    inter.message.reply.assert_not_awaited()


@pytest.mark.critical
@pytest.mark.asyncio
async def test_the_radio_already_off_at_the_click_lets_the_request_go_on():
    pool, player, inter, _ = confirm_world(Decision.DIRECT)
    answer = click("go")

    async def wait_for(event, *, timeout, check):
        player.radio_mode = None  # a rádio caiu enquanto o autor decidia
        return answer

    assert await radio_guard.confirm_leave_radio(pool, None, inter, player, wait_for=wait_for) is True
    pool.radio.jukebox.leave_for_music.assert_not_awaited()
    assert answer.edit_original_message.await_args.kwargs["content"] == "jukebox.radio.leave.already_off"


@pytest.mark.critical
@pytest.mark.asyncio
async def test_a_slash_request_gets_the_confirmation_as_an_ephemeral_followup():
    pool, player, _, _ = confirm_world(Decision.DIRECT)
    inter = MagicMock(spec=disnake.ApplicationCommandInteraction)
    inter.id, inter.author = 555, NS(id=10)
    inter.response.is_done.return_value = True
    inter.followup.send = AsyncMock(return_value=NS(edit=AsyncMock()))
    await radio_guard.confirm_leave_radio(pool, None, inter, player, wait_for=answered(click("cancel")))
    assert inter.followup.send.await_args.kwargs["ephemeral"] is True


@pytest.mark.critical
@pytest.mark.asyncio
@pytest.mark.parametrize(("clicker", "answer"), [(99, "jukebox.radio.leave.not_author"), (10, None)])
async def test_someone_else_clicking_the_public_prefix_reply_gets_an_ephemeral_refusal(clicker, answer):
    radio_guard._PENDING["777"] = (10, float("inf"))
    stray = NS(data=NS(custom_id="jukebox_radio_leave:777:go"), author=NS(id=clicker), send=AsyncMock())
    try:
        assert await radio_guard.answer_stray_click(stray, tr) is (answer is not None)
    finally:
        radio_guard._PENDING.pop("777", None)
    if answer is None:
        stray.send.assert_not_awaited()  # o clique do autor é do ``wait_for``
    else:
        stray.send.assert_awaited_once_with(answer, ephemeral=True)


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_a_jukebox_channel_request_in_radio_mode_stops_at_the_gate_before_searching(monkeypatch):
    """Sentinela Minor 1: o pedido digitado no canal da Jukebox enfileirava direto, sem a porta do modo rádio."""
    voice = NS(id=5)
    guild = NS(id=1, get_channel=lambda i: voice, me=NS(voice=NS(channel=voice)))
    player = NS(radio_mode=object())
    cog = NS(
        bot=NS(get_guild=lambda i: guild, get_channel=lambda i: None, music=NS(players={1: player}), pool=NS()),
        get_tracks=AsyncMock(),
    )
    message = NS(guild=guild, author=NS(voice=NS(channel=voice)), channel=NS(), content="música")
    req = NS(
        data={"check_other_bots_in_vc": False},
        message=message,
        source=None,
        text_channel=NS(id=9),
        destroy_message=None,
        stopped=False,
    )
    monkeypatch.setattr(jukebox_steps, "can_connect", lambda **kw: None)
    monkeypatch.setattr(jukebox_steps, "check_player_perm", AsyncMock())
    gate = AsyncMock(return_value=False)
    monkeypatch.setattr(jukebox_steps, "confirm_leave_radio", gate)
    await jukebox_steps._pr_checks(cog, req)
    assert req.stopped is True
    gate.assert_awaited_once_with(cog.bot.pool, cog.bot, message, player)
    cog.get_tracks.assert_not_awaited()


@pytest.mark.critical
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("who", "humans", "expected"),
    [
        ("creator", 3, Decision.DIRECT),
        ("member", 1, Decision.DIRECT),
        ("member", 3, Decision.VOTE),
        ("outside", 3, None),
    ],
)
async def test_the_leave_preview_only_picks_the_confirmation_text(world, who, humans, expected):
    """A prévia escolhe a variante (A = parar e tocar, B = abrir votação) sem efeito nenhum; fora de R não há passo."""
    world.members.clear()
    world.channel.members.clear()
    for i in range(humans):
        world.add(member(10 + i, world.channel))
    player = world.player(creator=10)
    if who != "outside":
        await world.radio.request_live(world.inter(world.members[10]), UUID)
    someone = world.members[10] if who == "creator" else world.members[10 + humans - 1]
    if who == "member":
        player.player_creator = 99
    assert await world.radio.leave_preview(someone, player) is expected
    assert player.radio_vote is None and world.relay.revoked == []


@pytest.mark.critical
@pytest.mark.asyncio
@pytest.mark.parametrize(("creator", "busy", "warns"), [(99, True, True), (10, True, False), (99, False, False)])
async def test_the_vote_warning_shows_only_when_the_click_would_open_a_vote(world, creator, busy, warns):
    world.add(member(11, world.channel))
    world.add(member(12, world.channel))
    player = world.player(current="música" if busy else None, creator=creator)
    assert await world.radio.live_preview(world.members[10], player) is warns
    assert await world.radio.live_preview(world.members[10], None) is False


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["back", "readd", "embed"])
async def test_back_readd_and_embed_requests_in_radio_mode_stop_at_the_gate(monkeypatch, surface):
    """Sentinela Minor 1: ⏮, ``/readd`` e os botões do embed (tocar agora, adicionar na fila, playlist) mexiam na fila sem a porta."""
    from empiremuse.music_cog import player_controller, queue_edit, skip
    from empiremuse.music_cog.player_controller import PlayerControllerMixin
    from empiremuse.music_cog.queue_edit import QueueEditMixin
    from empiremuse.music_cog.skip import SkipMixin
    from empiremuse.utils.others import PlayerControls

    live = live_track()
    player = NS(
        radio_mode=object(), current=live, queue=deque(), played=deque([faixa_youtube(1)]), failed_tracks=deque()
    )
    player.seek, player.process_next = AsyncMock(), AsyncMock()
    bot = NS(music=NS(players={1: player}), pool=NS(), bot_ready=True, is_ready=lambda: True)
    gate = AsyncMock(return_value=False)
    inter = NS(music_bot=bot, guild_id=1, guild=NS(id=1))
    cog = NS(bot=bot)
    if surface == "back":
        monkeypatch.setattr(skip, "confirm_leave_radio", gate)
        await SkipMixin.back.callback(cog, inter)
    elif surface == "readd":
        monkeypatch.setattr(queue_edit, "confirm_leave_radio", gate)
        await QueueEditMixin.readd_songs.callback(cog, inter)
    else:

        async def resolved(self, req):
            req.player, req.bot = player, bot
            return False

        track = AsyncMock()
        monkeypatch.setattr(player_controller, "confirm_leave_radio", gate)
        monkeypatch.setattr(player_controller, "_pc_embed_resolve", resolved)
        monkeypatch.setattr(player_controller, "_pc_embed_track", track)
        cog.player_interaction_concurrency = NS(release=AsyncMock())
        await PlayerControllerMixin.player_controller(cog, inter, PlayerControls.embed_forceplay)
        track.assert_not_awaited()
        cog.player_interaction_concurrency.release.assert_awaited_once()  # o limite de concorrência é liberado
    gate.assert_awaited_once()
    assert list(player.queue) == [] and len(player.played) == 1 and player.current is live
    player.process_next.assert_not_awaited()
