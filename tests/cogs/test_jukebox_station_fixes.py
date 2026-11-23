"""Revisão do PR 277 (T-J2B-FIX): os achados de Crivo, Alicerce e Sentinela na estação da Jukebox, um teste de regressão por defeito.

Só fakes, relógio e espera injetados (os mesmos de ``test_jukebox_station``); o texto de fim usa o catálogo real.
"""

from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from empiremuse.i18n.service import get_i18n
from empiremuse.music_cog import radio_guard
from empiremuse.rules.radio_station import CustomStation
from empiremuse.utils.music import jukebox_ui
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.music.models import player_events
from empiremuse.utils.music.models.player_state import PlayerStateMixin
from empiremuse.utils.radio.custom_source import FAIL_STREAK_MAX
from empiremuse.utils.radio.jukebox_radio import Refused
from empiremuse.utils.radio.jukebox_station import STATION_MARK, StationNext
from empiremuse.utils.radio.stations import StationStore
from tests.cogs.test_jukebox_radio_mode import member, settle
from tests.cogs.test_jukebox_station import ROCK, chooser, make_world, pick, song, started, station_player

HOSTILE = "@everyone <@&123> [clique](https://discord.gg/x) **negrito** `código`"
SERVER_STATION = "b" * 24


@pytest.fixture
def world(monkeypatch):
    return make_world(monkeypatch)


def hostile_store(world):
    """Uma estação do servidor com nome hostil (11.1): a store devolve como está gravado; o gênero continua o pronto."""
    hostile = CustomStation(
        id=SERVER_STATION, guild_id=1, name=HOSTILE, kind="artists", source={"artists": ["A"]}, rev=1
    )
    world.hub.stations = NS(
        get=AsyncMock(side_effect=lambda gid, ref: hostile if ref == SERVER_STATION else StationStore.builtin(gid, ref))
    )
    return hostile


def lastfm_calls(world) -> int:
    return world.hub.lastfm.recipe_tracks.await_count


# ---------------------------------------------------------------------- Crivo M1: o aviso de fim com o catálogo real


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.parametrize("locale", ["pt-BR", "en-US"])
def test_the_end_of_station_notice_renders_from_the_real_catalog(locale):
    tr = get_i18n().for_locale(locale)
    text = jukebox_ui.station_ended(tr, "Rock", FAIL_STREAK_MAX)
    assert "jukebox.station.ended" not in text and "Rock" in text and str(FAIL_STREAK_MAX) in text


# ---------------------------------------------------------------------- a porta antes do pool (Crivo m4, Sentinela F1, Alicerce s1)


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["restricted", "vote_opens", "vote_running"])
async def test_a_refused_or_voted_request_spends_no_pool_load(world, case):
    world.add(member(11, world.channel))
    world.add(member(12, world.channel))
    player = station_player(world, current="música", queue=[song(11)], creator=99, restrict=case == "restricted")
    if case == "vote_running":
        player.radio_vote = NS(task=None, message=None)
    if case == "vote_opens":
        key, _ = await world.radio.request_station(world.inter(world.members[10]), ROCK)
        assert key == "jukebox.radio.vote.opened"
    else:
        with pytest.raises(Refused):
            await world.radio.request_station(world.inter(world.members[10]), ROCK)
    assert lastfm_calls(world) == 0 and player.station is None


# ---------------------------------------------------------------------- o pool fora do lock (Crivo s1, Alicerce m3, Sentinela F2)


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_a_passing_station_vote_loads_the_pool_without_holding_the_lock(world):
    world.add(member(11, world.channel))
    world.add(member(12, world.channel))
    player = station_player(world, current="música", queue=[song(11)], creator=99)
    await world.radio.request_station(world.inter(world.members[10]), ROCK)
    gate, held = asyncio.Event(), []
    original = world.hub.lastfm.recipe_tracks

    async def slow(*args, **kwargs):
        held.append(player.radio_lock.locked())
        await gate.wait()
        return await original(*args, **kwargs)

    world.hub.lastfm.recipe_tracks = AsyncMock(side_effect=slow)
    await world.radio.vote_click(world.inter(world.members[11]), player.radio_vote.nonce)
    await settle()
    assert held == [False] and player.station is None  # carregando, sem o lock; nada mudou ainda
    gate.set()
    await settle()
    assert player.station.ref == ROCK


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["premium", "radio_24h"])
async def test_a_station_vote_redoes_premium_and_the_24h_check_when_it_passes(world, change):
    """§8 item 3 com S (Crivo m5): o que valia ao abrir o voto é refeito na passagem."""
    world.add(member(11, world.channel))
    world.add(member(12, world.channel))
    player = station_player(world, current="música", queue=[song(11)], creator=99)
    await world.radio.request_station(world.inter(world.members[10]), ROCK)
    if change == "premium":
        world.hub.premium.fresh = AsyncMock(return_value=None)
    else:
        world.hub.manager.running_in.return_value = ["uma rádio 24 h"]
    await world.radio.vote_click(world.inter(world.members[11]), player.radio_vote.nonce)
    await settle()
    assert player.station is None and len(player.queue) == 1
    closed = player.messages[0].edit.await_args.kwargs
    assert closed["content"] == "jukebox.radio.vote.failed" and closed["components"] == []


# ---------------------------------------------------------------------- releitura e cerca de fechamento (Alicerce m4, m5; Sentinela F3)


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_a_station_deleted_while_the_pool_loads_does_not_start(world):
    player = station_player(world, creator=10)
    original = world.hub.lastfm.recipe_tracks

    async def deleted_meanwhile(*args, **kwargs):
        world.hub.stations.get = AsyncMock(return_value=None)
        return await original(*args, **kwargs)

    world.hub.lastfm.recipe_tracks = AsyncMock(side_effect=deleted_meanwhile)
    with pytest.raises(Refused) as refused:
        await world.radio.request_station(world.inter(world.members[10]), ROCK)
    assert refused.value.key == "jukebox.station.not_found" and player.station is None


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_a_player_closed_during_the_checks_gets_no_station(world):
    player = station_player(world, creator=10)
    calls = []

    async def closes_on_the_second_check(guild_id):
        calls.append(guild_id)
        if len(calls) == 2:  # a conferência de dentro do lock, depois da carga
            player.is_closing = True
        return object()

    world.hub.premium.fresh = AsyncMock(side_effect=closes_on_the_second_check)
    with pytest.raises(Refused) as refused:
        await world.radio.request_station(world.inter(world.members[10]), ROCK)
    assert refused.value.key == "jukebox.radio.player_closed" and player.station is None


class _Stop(Exception):
    pass


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_destroying_a_player_with_only_a_station_forgets_it():
    jukebox = NS(forget=AsyncMock())

    def stop_here():
        raise _Stop

    player = NS(
        is_closing=False,
        radio_mode=None,
        radio_vote=None,
        station=object(),
        bot=NS(pool=NS(radio=NS(jukebox=jukebox))),
        _cancel_voice_status_tasks=stop_here,
    )
    with pytest.raises(_Stop):
        await PlayerStateMixin.process_destroy(player)
    jukebox.forget.assert_awaited_once_with(player)


# ---------------------------------------------------------------------- S → M sob o lock e sem epoch (Crivo m1, m2; Alicerce m2)


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_the_hook_ends_the_station_only_under_the_lock_and_closes_the_open_vote(world):
    world.add(member(11, world.channel))
    world.add(member(12, world.channel))
    player = await started(world, current="música")
    player.player_creator = 99
    world.clock.t += 10  # depois do intervalo entre trocas [s1]
    await world.radio.request_station(world.inter(world.members[10]), ROCK)  # S ligada: o membro abre voto
    vote_message = player.messages[0]
    world.hub.premium.get = AsyncMock(return_value=None)
    await player.radio_lock.acquire()
    hook = asyncio.ensure_future(world.radio.next_station_track(player, player.station))
    await settle()
    assert not hook.done() and player.station is not None  # espera o lock
    player.radio_lock.release()
    assert await hook == (StationNext.ENDED, None)
    assert player.station is None and player.radio_vote is None
    assert vote_message.edit.await_args.kwargs["components"] == []  # nenhum botão de voto órfão


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_turning_the_station_off_keeps_a_music_request_in_flight(world):
    player = await started(world)
    epoch_at_the_gate = player.radio_epoch
    await world.radio.end_station(player)
    radio_guard.enqueue_fence(player, epoch_at_the_gate)  # não levanta: S → M não descarta nada


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_starting_a_station_fences_a_music_request_in_flight(world):
    """§8 11.4 com S (Crivo m5): M → S descarta a fila; o pedido que passou pela porta antes recebe o aviso."""
    player = station_player(world, creator=10)
    epoch_at_the_gate = player.radio_epoch
    await world.radio.request_station(world.inter(world.members[10]), ROCK)
    with pytest.raises(GenericError):
        radio_guard.enqueue_fence(player, epoch_at_the_gate)


# ---------------------------------------------------------------------- o gancho tomado por outra transição (Alicerce m1)


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["radio", "other_station"])
async def test_a_transition_during_the_resolution_supersedes_the_hook(world, transition):
    player = await started(world)
    active = player.station
    gate = asyncio.Event()

    async def slow_resolve(partial):
        await gate.wait()
        partial.id = "enc:x"

    player.resolve_track = slow_resolve
    hook = asyncio.ensure_future(world.radio.next_station_track(player, active))
    await settle()
    player.station = None if transition == "radio" else NS(ref="g:pop")
    if transition == "radio":
        player.radio_mode = object()  # R abrindo: ainda sem faixa
    gate.set()
    assert await hook == (StationNext.SUPERSEDED, None)


# ---------------------------------------------------------------------- repetir fila (Alicerce m6)


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_repeat_queue_never_puts_a_station_track_in_the_queue(monkeypatch):
    monkeypatch.setattr(player_events.asyncio, "sleep", AsyncMock())
    track = NS(info={"extra": {STATION_MARK: ROCK}}, is_stream=False, duration=200_000, track_loops=0, autoplay=True)
    player = NS(
        votes=set(), locked=False, last_track=track, loop="queue", is_previows_music=False, queue=deque(), played=[]
    )
    await player_events.PlayerEventsMixin.track_end(player)
    assert list(player.queue) == [] and player.played == []


# ---------------------------------------------------------------------- nome hostil nos destinos da J2b (§8 11.1, Crivo m5, Sentinela F4)


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.parametrize(
    "render",
    [
        lambda tr, name: jukebox_ui.station_ended(tr, name, FAIL_STREAK_MAX),
        lambda tr, name: jukebox_ui.vote_passed_station(tr, name, now=True),
        lambda tr, name: jukebox_ui.station_warning(tr, station_name=name, queued=True),
        lambda tr, name: jukebox_ui.vote_payload(
            tr,
            nonce="ab12",
            author_id=10,
            target=jukebox_ui.Target.STATION,
            station_name=name,
            votes=1,
            needed=2,
            expires_at=0,
        )["content"],
        lambda tr, name: jukebox_ui.station_line(tr, NS(name=name)),
    ],
)
def test_a_hostile_server_station_name_is_inert_in_every_station_text(render):
    tr = get_i18n().for_locale("pt-BR")
    text = render(tr, HOSTILE)
    for active in ("@everyone", "<@&123>", "](https://", "https://discord.gg", "**negrito**", "`código`"):
        assert active not in text


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_a_hostile_server_station_name_is_inert_in_the_player_label_and_the_notice(world):
    hostile_store(world)
    player = station_player(world, creator=10)
    await world.radio.request_station(world.inter(world.members[10]), SERVER_STATION)
    player.bot.pool = NS(radio=NS(jukebox=world.radio))
    player.tr = get_i18n().for_locale("pt-BR")
    label = jukebox_ui.autoplay_label(player)
    assert "@everyone" not in label and "<@&" not in label and "https://" not in label
    await world.radio._station_ended_notice(player, player.station)
    notice = player.messages[-1]
    assert "@everyone" not in notice.args[0] and notice.kwargs["allowed_mentions"].everyone is False


# ---------------------------------------------------------------------- o seletor avisa da votação com S ligada (Crivo s4, Alicerce s3)


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_the_chooser_warns_before_a_vote_even_with_an_empty_queue():
    view, jukebox = chooser(preview=(False, True, None))  # S ligada, fila vazia, o clique abriria voto
    view.render()
    click = pick("c" * 24)
    await view._on_select(click)
    jukebox.request_station.assert_not_awaited()
    assert "jukebox.radio.warn.vote" in click.edit_original_message.await_args.kwargs["content"]


# ---------------------------------------------------------------------- uma playlist quebrada não derruba o pool (Crivo s3, Sentinela S4)


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_a_broken_radio_playlist_is_skipped_and_the_others_still_load():
    from empiremuse.utils.radio.playlist_tracks import radio_playlist_tracks

    async def get_tracks(url, *args, **kwargs):
        if url == "quebrada":
            raise RuntimeError("nó")
        return [NS(author="Artista", title="Faixa")], None

    playlists = {"a": NS(url="quebrada"), "b": NS(url="boa")}
    pool = NS(servers=NS(playlists=NS(find=AsyncMock(side_effect=lambda gid, name, scope: playlists[name]))))
    bot = NS(get_cog=lambda name: NS(get_tracks=get_tracks))
    guild = NS(id=1, me=NS())
    found = await radio_playlist_tracks(pool, bot, guild, None, None, ["a", "b"])
    assert [(t.artist, t.title) for t in found] == [("Artista", "Faixa")]


# ---------------------------------------------------------------------- revisão 2 do PR 277 (T-J2B-FIX2)


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("results", "raises", "found"),
    [
        ({"a": RuntimeError("nó"), "b": RuntimeError("nó")}, True, []),  # o nó fora: sobe, não vira estação vazia
        ({"a": [NS(author="", title="")]}, False, []),  # carregou, mas sem autor ou título: vazia de verdade
    ],
)
async def test_the_playlist_loader_raises_only_when_nothing_loaded_and_something_failed(results, raises, found):
    """Sentinela M-1 = Alicerce N2, com o ajuste do Crivo: a falha total do Lavalink não pode virar "estação sem faixas"."""
    from empiremuse.utils.radio.playlist_tracks import radio_playlist_tracks

    async def get_tracks(url, *args, **kwargs):
        outcome = results[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, None

    playlists = {name: NS(url=name) for name in results}
    pool = NS(servers=NS(playlists=NS(find=AsyncMock(side_effect=lambda gid, name, scope: playlists[name]))))
    bot = NS(get_cog=lambda name: NS(get_tracks=get_tracks))
    call = radio_playlist_tracks(pool, bot, NS(id=1, me=NS()), None, None, list(results))
    if raises:
        with pytest.raises(RuntimeError):
            await call
    else:
        assert await call == found


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_a_station_vote_does_not_start_after_the_station_was_turned_off_meanwhile(world):
    """Alicerce N1: S → M não incrementa o epoch; um desligar durante a carga do pool anula o voto S1 → S2."""
    world.add(member(11, world.channel))
    world.add(member(12, world.channel))
    player = await started(world, current="música")
    player.player_creator = 99
    world.clock.t += 10
    await world.radio.request_station(world.inter(world.members[10]), ROCK)  # S1 ligada: o membro abre voto para S2
    gate = asyncio.Event()
    original = world.hub.lastfm.recipe_tracks

    async def slow(*args, **kwargs):
        await gate.wait()
        return await original(*args, **kwargs)

    world.hub.lastfm.recipe_tracks = AsyncMock(side_effect=slow)
    await world.radio.vote_click(world.inter(world.members[11]), player.radio_vote.nonce)
    await settle()
    vote_message = player.messages[0]
    frozen = vote_message.edit.await_args.kwargs["components"]
    assert frozen and all(button.disabled for button in frozen)  # Crivo s-A: o botão desativado durante a carga
    await world.radio.end_station(player)  # alguém desliga a estação no meio
    gate.set()
    await settle()
    assert player.station is None
    assert vote_message.edit.await_args.kwargs["content"] == "jukebox.radio.vote.failed"


def handler_player(world, player, monkeypatch):
    """O player do mundo falso com o que ``_he_track_end`` e ``_he_track_stuck`` leem (o fim de verdade não roda: ``locked``)."""
    from empiremuse.utils.music.models import event_steps

    player.node = NS(identifier="LOCAL")
    player.bot.dispatch = lambda *args, **kwargs: None
    player.bot.pool = NS(radio=NS(jukebox=world.radio))
    player.network_paused = False
    player.network_abandon_streak = 0
    player.locked = True
    player.track_end = AsyncMock()
    player.set_command_log = lambda *args, **kwargs: None
    monkeypatch.setattr(event_steps, "_he_cancel_message_updater", lambda self: None)
    return event_steps


def end_event(track, reason):
    return NS(node=NS(identifier="LOCAL"), track={"encoded": track.id}, reason=reason)


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_a_finished_station_track_resets_the_streak_through_the_track_end_handler(world, monkeypatch):
    """Crivo m-A (1): a fiação do fim no handler; sem ela a sequência nunca zera e uma estação saudável para depois de 5 faixas."""
    player = await started(world)
    steps = handler_player(world, player, monkeypatch)
    active = player.station
    _, track = await world.radio.next_station_track(player, active)
    await steps._he_track_end(player, end_event(track, "FINISHED"))
    assert active.session.streak == 0 and active.pending is None


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_a_station_track_that_fails_to_load_leaves_the_pool_through_the_handler(world, monkeypatch):
    player = await started(world)
    steps = handler_player(world, player, monkeypatch)
    active = player.station
    size = len(active.session.pool)
    _, track = await world.radio.next_station_track(player, active)
    await steps._he_track_end(player, end_event(track, "LOAD_FAILED"))
    assert active.session.streak == 1 and len(active.session.pool) == size - 1


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
async def test_a_stuck_station_track_counts_and_the_replaced_end_does_not_reset(world, monkeypatch):
    """Crivo m-A (2): a travada sai do pool; o ``REPLACED`` dela chega quando ``last_track`` já é a faixa nova e não zera nada."""
    player = await started(world)
    steps = handler_player(world, player, monkeypatch)
    player.process_next = AsyncMock()
    active = player.station
    size = len(active.session.pool)
    _, stuck = await world.radio.next_station_track(player, active)
    player.current = stuck
    await steps._he_track_stuck(player)
    assert len(active.session.pool) == size - 1 and active.pending is None
    _, fresh = await world.radio.next_station_track(player, active)
    player.last_track = fresh
    await steps._he_track_end(player, end_event(stuck, "REPLACED"))
    assert active.session.streak == 2 and active.pending[1] is fresh
