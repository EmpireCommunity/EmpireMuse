"""Estação da Jukebox (T-434 J2b, §2.2, §2.6, A5): o mixin do controlador, o gancho do autoplay, o seletor e a contagem.

Cobre o §8 itens 1, 3, 4 (com S), 11.4 (fila descartada só quando passa) e 11.9 (estação de outro servidor). Só fakes, relógio e espera injetados;
``tr`` devolve a chave.
"""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from empiremuse.rules.radio_shuffle import Track
from empiremuse.utils.music import jukebox_ui
from empiremuse.utils.music.models import playback_steps
from empiremuse.utils.radio.custom_source import FAIL_STREAK_MAX
from empiremuse.utils.radio.jukebox_radio import Refused
from empiremuse.utils.radio.jukebox_station import STATION_MARK, StationNext, is_station_track
from empiremuse.utils.radio.station_chooser import BACK_VALUE, StationChooser
from empiremuse.utils.radio.stations import COUNT_TTL_S, StationStore
from tests.cogs.test_jukebox_radio_mode import UUID, World, member, on_air, settle

ROCK = "g:rock"
OTHER_GUILD_STATION = "a" * 24  # um id válido que o servidor 1 não tem (é de outro servidor)


def tr(key, **fields):
    return key


def song(requester):
    return NS(requester=requester, title="música", info={"extra": {}})


def resolving_player(player, *, playable=True):
    """``resolve_track`` falso: dá um id (tocável) ou nada (não tocável)."""

    async def resolve_track(partial):
        if playable:
            partial.id = f"enc:{partial.title}"

    player.resolve_track = resolve_track
    return player


def make_world(monkeypatch):
    """O mundo falso da estação (também usado em ``test_jukebox_station_fixes``)."""
    w = World(monkeypatch)
    w.bot.user = NS(id=900)
    w.hub.stations = NS(get=AsyncMock(side_effect=lambda gid, ref: StationStore.builtin(gid, ref)))
    w.hub.lastfm = NS(
        recipe_tracks=AsyncMock(
            return_value=NS(tracks=[Track(f"Artista {i}", f"Faixa {i}") for i in range(40)], unknown=())
        )
    )
    w.hub.premium.get = AsyncMock(return_value=object())
    w.hub.custom_favorites = NS(toggle=AsyncMock(return_value=NS(value="added")))
    return w


@pytest.fixture
def world(monkeypatch):
    return make_world(monkeypatch)


def genre_tr(key, **fields):
    return "Rock" if key == "radio.genre.rock" else key


def station_player(world, **kwargs):
    player = world.player(**kwargs)
    player.tr = genre_tr  # o nome mostrado do gênero pronto vem do i18n
    player.station = None
    player.process_next = AsyncMock()
    player.last_track = None
    return resolving_player(player)


# ---------------------------------------------------------------------- ligar (M → S), a porta e a fila (A5, G1)


@pytest.mark.critical
@pytest.mark.asyncio
async def test_an_idle_player_starts_the_station_now_without_a_vote(world):
    player = station_player(world, creator=99)
    key, fields = await world.radio.request_station(world.inter(world.members[10]), ROCK)
    assert key == "jukebox.station.started_now" and fields == {"station": "Rock"}
    assert player.station.ref == ROCK and player.radio_vote is None
    player.process_next.assert_awaited_once()  # ocioso: a 1ª faixa já sai (fora do lock)


@pytest.mark.critical
@pytest.mark.asyncio
async def test_a_station_with_only_my_songs_queued_starts_after_the_current_song_and_drops_the_queue(world):
    player = station_player(world, current="música", queue=[song(10), song(10)], creator=99)
    key, _ = await world.radio.request_station(world.inter(world.members[10]), ROCK)
    assert key == "jukebox.station.started_next"
    assert player.station.ref == ROCK and not player.queue and not player.queue_autoplay  # G1
    assert player.current == "música"  # a atual termina; a estação vem na próxima faixa
    player.process_next.assert_not_awaited()


@pytest.mark.critical
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dj", "expected"), [(True, "jukebox.station.started_next"), (False, "jukebox.radio.vote.opened")]
)
async def test_someone_elses_songs_in_the_queue_need_a_dj_or_a_vote(world, dj, expected):
    world.add(member(11, world.channel))
    world.add(member(12, world.channel))
    player = station_player(world, current="música", queue=[song(11)], creator=10 if dj else 99)
    key, _ = await world.radio.request_station(world.inter(world.members[10]), ROCK)
    assert key == expected
    if dj:
        assert player.station.ref == ROCK and not player.queue
        return
    assert player.station is None and len(player.queue) == 1  # nada muda até passar
    assert player.radio_vote.target.value == "station" and player.radio_vote.uuid == ROCK


@pytest.mark.critical
@pytest.mark.asyncio
async def test_a_passing_station_vote_redoes_the_checks_then_drops_the_queue(world):
    world.add(member(11, world.channel))
    world.add(member(12, world.channel))
    player = station_player(world, current="música", queue=[song(11)], creator=99)
    await world.radio.request_station(world.inter(world.members[10]), ROCK)
    key, _ = await world.radio.vote_click(world.inter(world.members[11]), player.radio_vote.nonce)
    assert key == "jukebox.radio.vote.counted"
    await settle()  # o pool e o efeito saem do lock (T-J2B-FIX): numa tarefa à parte
    assert player.station.ref == ROCK and not player.queue
    closed = player.messages[0].edit.await_args.kwargs
    # Bússola (T-J2B-TEXTS): com música tocando a estação começa depois dela, e o texto diz isso
    assert closed["content"] == "jukebox.radio.vote.passed_station_next" and closed["components"] == []


@pytest.mark.critical
@pytest.mark.asyncio
async def test_a_station_vote_that_passes_after_the_station_vanished_changes_nothing(world):
    world.add(member(11, world.channel))
    world.add(member(12, world.channel))
    player = station_player(world, current="música", queue=[song(11)], creator=99)
    await world.radio.request_station(world.inter(world.members[10]), ROCK)
    world.hub.stations.get = AsyncMock(return_value=None)  # apagada enquanto votavam
    await world.radio.vote_click(world.inter(world.members[11]), player.radio_vote.nonce)
    assert player.station is None and len(player.queue) == 1
    assert player.messages[0].edit.await_args.kwargs["content"] == "jukebox.radio.vote.failed"


def no_tracks(w):
    w.hub.lastfm.recipe_tracks = AsyncMock(return_value=NS(tracks=[], unknown=()))


@pytest.mark.critical
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ref", "setup", "expected"),
    [
        ("g:nao_existe", None, "jukebox.station.not_found"),
        (OTHER_GUILD_STATION, None, "jukebox.station.not_found"),  # 11.9: o store não devolve estação de outro servidor
        ("../x", None, "jukebox.station.not_found"),
        (ROCK, lambda w: setattr(w.hub.manager, "running_in", Mock(return_value=["24h"])), "jukebox.radio.has_24h"),
        (ROCK, lambda w: setattr(w.hub.premium, "fresh", AsyncMock(return_value=None)), "radio.refused.no_premium"),
        (ROCK, no_tracks, "jukebox.station.empty"),
    ],
)
async def test_station_preconditions_refuse_without_touching_the_player(world, ref, setup, expected):
    player = station_player(world, current="música", queue=[song(10)], creator=10)
    if setup is not None:
        setup(world)
    with pytest.raises(Refused) as refused:
        await world.radio.request_station(world.inter(world.members[10]), ref)
    assert refused.value.key == expected
    assert player.station is None and len(player.queue) == 1


@pytest.mark.critical
@pytest.mark.asyncio
async def test_restricted_mode_refuses_a_member_with_someone_elses_queue(world):
    world.add(member(11, world.channel))
    player = station_player(world, current="música", queue=[song(11)], creator=99, restrict=True)
    with pytest.raises(Refused) as refused:
        await world.radio.request_station(world.inter(world.members[10]), ROCK)
    assert refused.value.key == "jukebox.radio.restricted" and player.station is None


# ---------------------------------------------------------------------- R e S são exclusivos


@pytest.mark.critical
@pytest.mark.asyncio
async def test_choosing_a_station_in_radio_mode_ends_the_live_radio_and_starts_now(world):
    player = await on_air(world)
    player.station = None
    resolving_player(player)
    player.process_next = AsyncMock()
    world.clock.t += 10
    key, _ = await world.radio.request_station(world.inter(world.members[10]), ROCK)
    assert key == "jukebox.station.started_now"
    assert player.radio_mode is None and world.relay.revoked == ["tok1"] and player.station.ref == ROCK
    player.process_next.assert_awaited_once()


@pytest.mark.critical
@pytest.mark.asyncio
async def test_entering_radio_mode_turns_the_station_off(world):
    player = station_player(world, creator=10)
    await world.radio.request_station(world.inter(world.members[10]), ROCK)

    async def opens():
        player.current = player.queue.popleft() if player.queue else None

    player.process_next = opens
    world.clock.t += 10
    await world.radio.request_live(world.inter(world.members[10]), UUID)
    assert player.radio_mode is not None and player.station is None


# ---------------------------------------------------------------------- a próxima faixa (gancho do autoplay, §2.6)


async def started(world, **kwargs):
    player = station_player(world, creator=10, **kwargs)
    await world.radio.request_station(world.inter(world.members[10]), ROCK)
    return player


@pytest.mark.critical
@pytest.mark.asyncio
async def test_the_next_track_is_a_resolved_autoplay_partial_marked_with_the_station(world):
    player = await started(world)
    outcome, track = await world.radio.next_station_track(player, player.station)
    assert (
        outcome is StationNext.TRACK
        and track.id.startswith("enc:")
        and track.autoplay is True
        and track.requester == 900
    )
    assert is_station_track(track, ROCK) and track.info["extra"][STATION_MARK] == ROCK


@pytest.mark.critical
@pytest.mark.asyncio
async def test_five_tracks_in_a_row_without_playing_end_the_station_with_one_message(world):
    player = await started(world)
    resolving_player(player, playable=False)
    assert await world.radio.next_station_track(player, player.station) == (StationNext.ENDED, None)
    assert player.station is None
    assert [m.args for m in player.messages] == [("jukebox.station.ended",)]
    assert player.messages[0].kwargs["allowed_mentions"].everyone is False


@pytest.mark.critical
@pytest.mark.asyncio
async def test_only_real_progress_resets_the_streak(world):
    player = await started(world)
    active = player.station
    for _ in range(FAIL_STREAK_MAX - 1):
        await world.radio.next_station_track(player, active)
    assert active.session.streak == FAIL_STREAK_MAX - 1  # resolveram, mas nenhuma tocou de fato
    _, track = await world.radio.next_station_track(player, active)
    world.radio.station_track_ended(player, track, "FINISHED")
    assert active.session.streak == 0 and player.station is active


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "LOAD_FAILED"])
async def test_a_track_that_fails_to_load_counts_and_leaves_the_pool(world, failure):
    """Crivo m3 (T-J2B-FIX): o TrackStart sai antes de o stream abrir; a faixa que não carrega contava como tocada e a estação girava para sempre."""
    player = await started(world)
    active = player.station
    pool_size = len(active.session.pool)
    _, track = await world.radio.next_station_track(player, active)
    if failure == "exception":
        world.radio.station_track_failed(player, track)
    else:
        world.radio.station_track_ended(player, track, failure)
    assert active.session.streak == 1 and len(active.session.pool) == pool_size - 1
    assert (track.title, track.author) not in {(t.title, t.artist) for t in active.session.pool}


@pytest.mark.critical
@pytest.mark.asyncio
async def test_losing_premium_turns_the_station_off_at_the_next_track(world):
    player = await started(world)
    world.hub.premium.get = AsyncMock(return_value=None)  # Q4: confere a cada nova faixa (cache de 30 s)
    assert await world.radio.next_station_track(player, player.station) == (StationNext.ENDED, None)
    assert player.station is None


@pytest.mark.critical
@pytest.mark.asyncio
async def test_a_station_replaced_meanwhile_gives_no_track(world):
    player = await started(world)
    old = player.station
    player.station = None
    assert await world.radio.next_station_track(player, old) == (StationNext.ENDED, None)


@pytest.mark.critical
@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [StationNext.TRACK, StationNext.ENDED, StationNext.SUPERSEDED])
async def test_the_station_comes_before_the_autoplay_in_the_next_track_hook(outcome, monkeypatch):
    """Alicerce m1 (T-J2B-FIX): SUPERSEDED (R abrindo ou outra estação no meio) não cai no autoplay nem no ocioso."""
    partial = NS(name="da estação")

    async def next_station_track(player, active):
        if outcome is StationNext.ENDED:
            player.station = None  # S → M: a estação já saiu quando o gancho devolve ENDED
        return outcome, partial if outcome is StationNext.TRACK else None

    jukebox = NS(next_station_track=AsyncMock(side_effect=next_station_track))
    idle = AsyncMock()
    monkeypatch.setattr(playback_steps, "go_idle", idle)
    active = object()
    player = NS(
        queue=deque(),
        radio_id=None,
        radio_mode=None,
        station=active,
        autoplay=True,
        keep_connected=False,
        get_autoqueue_tracks=AsyncMock(return_value="recomendação"),
        bot=NS(pool=NS(radio=NS(jukebox=jukebox))),
        process_next=AsyncMock(),
    )
    req = NS(track=None)
    stop = await playback_steps._pn_next_track(player, req)
    jukebox.next_station_track.assert_awaited_once_with(player, active)
    if outcome is StationNext.TRACK:
        assert req.track is partial and stop is False
        player.get_autoqueue_tracks.assert_not_awaited()
    elif outcome is StationNext.ENDED:
        assert req.track == "recomendação"  # a estação acabou: o autoplay normal, se ligado
    else:
        assert stop is True and req.track is None
        player.get_autoqueue_tracks.assert_not_awaited()
        idle.assert_not_awaited()


# ---------------------------------------------------------------------- desligar pelo botão de autoplay e a estrela


@pytest.mark.critical
@pytest.mark.asyncio
async def test_the_autoplay_button_turns_the_station_off(world):
    player = await started(world)
    epoch = player.radio_epoch
    assert await world.radio.end_station(player) == "Rock"
    # T-J2B-FIX (decisão do Regente): S → M não descarta nada, então não mexe no epoch e um pedido de música em curso segue
    assert player.station is None and player.radio_epoch == epoch
    assert await world.radio.end_station(player) is None


@pytest.mark.critical
@pytest.mark.asyncio
async def test_the_player_star_saves_the_station_in_the_members_server_favorites(world):
    player = await started(world)
    key, fields = await world.radio.toggle_station_favorite(world.inter(world.members[10]), player)
    assert key == "radio.panel.custom.favorite_added" and fields["max"] == 24
    world.hub.custom_favorites.toggle.assert_awaited_once_with(
        1, 10, ROCK
    )  # a ref vem do player, a identidade da interação


@pytest.mark.critical
@pytest.mark.asyncio
async def test_the_view_and_the_autoplay_label_show_only_the_station_name(world):
    player = await started(world)
    view = world.radio.station_view(player)
    assert view.ref == ROCK and view.name == "Rock" and view.playing is False
    assert "session" not in repr(view) and "pool" not in repr(view)
    player.bot.pool = NS(radio=NS(jukebox=world.radio))
    player.autoplay = False
    assert jukebox_ui.autoplay_label(player) == "jukebox.station.autoplay_label"
    player.station = None
    assert jukebox_ui.autoplay_label(player) == "player.enable_autoplay.label"


# ---------------------------------------------------------------------- a contagem da entrada fixa (Q7)


@pytest.mark.critical
@pytest.mark.asyncio
async def test_the_station_count_is_cached_for_30s_and_forgotten_on_change():
    clock = NS(t=0.0)
    store = StationStore(NS(), NS(), clock=lambda: clock.t)
    store.list_for_guild = AsyncMock(side_effect=[[1, 2], [1, 2, 3], [1]])
    assert await store.count_for_guild(1) == 2
    clock.t += COUNT_TTL_S - 1
    assert await store.count_for_guild(1) == 2  # dentro do prazo: sem ler o banco
    clock.t += 1
    assert await store.count_for_guild(1) == 3
    store._forget_count(1)  # criar ou apagar uma estação
    assert await store.count_for_guild(1) == 1
    assert store.list_for_guild.await_count == 3


# ---------------------------------------------------------------------- o seletor "Rádios do Servidor" (A5)


def chooser(*, preview=(False, False, None), favorites=()):
    stations = [
        NS(id="b" * 24, name="Zeta", name_norm="zeta", kind="artists"),
        NS(id="c" * 24, name="Alfa", name_norm="alfa", kind="playlists"),
    ]
    jukebox = NS(request_station=AsyncMock(return_value=("jukebox.station.started_now", {"station": "Alfa"})))
    view = StationChooser(
        jukebox=jukebox,
        tr=tr,
        author_id=10,
        stations=stations,
        favorites=favorites,
        genre_groups=[("international", ["rock", "pop"]), ("brazilian", ["forro"])],
        preview=AsyncMock(return_value=preview),
    )
    return view, jukebox


def pick(value, author=10):
    return NS(
        values=[value],
        author=NS(id=author),
        response=NS(edit_message=AsyncMock(), defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_message=AsyncMock(),
    )


@pytest.mark.critical
@pytest.mark.asyncio
async def test_the_chooser_lists_my_favorites_first_then_the_two_genre_groups():
    view, _ = chooser(favorites=["b" * 24])
    view.render()
    assert [o.value for o in view.children[0].options] == ["b" * 24, "c" * 24, "f:international", "f:brazilian"]
    await view._on_select(pick("f:international"))
    assert [o.value for o in view.children[0].options] == [BACK_VALUE, "g:rock", "g:pop"]


@pytest.mark.critical
@pytest.mark.asyncio
@pytest.mark.parametrize(("preview", "plays_now"), [((False, False, None), True), ((True, False, None), False)])
async def test_choosing_plays_now_on_an_empty_queue_and_asks_first_with_songs_queued(preview, plays_now):
    view, jukebox = chooser(preview=preview)
    view.render()
    click = pick("c" * 24)
    await view._on_select(click)
    assert jukebox.request_station.await_count == int(plays_now)
    if not plays_now:
        assert click.edit_original_message.await_args.kwargs["content"] == "jukebox.station.warn.queue"
        await view._on_confirm(pick("c" * 24))
        jukebox.request_station.assert_awaited_once()
    assert jukebox.request_station.await_args.args[1] == "c" * 24


@pytest.mark.critical
@pytest.mark.asyncio
async def test_a_ref_the_chooser_never_offered_is_ignored_and_another_member_cannot_click():
    view, jukebox = chooser()
    view.render()
    await view._on_select(pick(OTHER_GUILD_STATION))
    jukebox.request_station.assert_not_awaited()
    stranger = pick("c" * 24, author=99)
    assert await view.interaction_check(stranger) is False
    stranger.response.send_message.assert_awaited_once_with("jukebox.radio.leave.not_author", ephemeral=True)


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize(("key", "left"), [("jukebox.station.empty", 2), ("jukebox.station.not_found", 1)])
async def test_a_station_that_cannot_play_becomes_a_note_and_the_chooser_stays_open(key, left):
    """Bússola (T-J2B-TEXTS, desvio 1): a recusa de UMA estação não fecha o seletor; a que sumiu do servidor sai da lista."""
    view, jukebox = chooser()
    jukebox.request_station.side_effect = Refused(key)
    view.render()
    click = pick("c" * 24)
    await view._on_select(click)
    shown = click.edit_original_message.await_args.kwargs
    assert shown["content"] == key and shown["view"] is view and not view.is_finished()
    assert view.chosen is None and len(view.children[0].options) == left + 2  # as estações e os dois grupos de gêneros


@pytest.mark.critical
@pytest.mark.regression
@pytest.mark.parametrize(
    ("count", "key"),
    [
        (0, "jukebox.select.server.description_none"),
        (1, "jukebox.select.server.description"),
        (3, "jukebox.select.server.description"),
    ],
)
def test_the_server_entry_never_says_zero_stations(count, key):
    """Bússola (T-J2B-TEXTS): "as 0 estações" saía num servidor só com os gêneros prontos."""
    seen = []
    jukebox_ui.server_description(lambda k, **fields: seen.append((k, fields)) or k, count)
    assert seen == [(key, {} if count == 0 else {"count": count})]


@pytest.mark.critical
@pytest.mark.asyncio
async def test_a_refusal_from_the_controller_is_shown_and_the_chooser_closes():
    view, jukebox = chooser()
    jukebox.request_station.side_effect = Refused("jukebox.radio.has_24h")
    view.render()
    await view._on_select(pick("f:international"))
    click = pick(ROCK)
    await view._on_select(click)
    assert click.edit_original_message.await_args.kwargs == {"content": "jukebox.radio.has_24h", "view": None}


@pytest.mark.critical
@pytest.mark.asyncio
@pytest.mark.parametrize("station_on", [True, False])
async def test_the_autoplay_command_turns_the_station_off_instead_of_toggling_autoplay(station_on):
    from empiremuse.music_cog.player_modes import PlayerModesMixin

    jukebox = NS(end_station=AsyncMock(return_value="Rock"))
    player = NS(station=object() if station_on else None, autoplay=False, current=None, tr=tr, process_next=AsyncMock())
    bot = NS(music=NS(players={1: player}), pool=NS(radio=NS(jukebox=jukebox)))
    cog = NS(bot=bot, interaction_message=AsyncMock())
    inter = NS(music_bot=bot, guild_id=1, author=NS(mention="<@10>"))
    await PlayerModesMixin.autoplay.callback(cog, inter)
    assert player.autoplay is (not station_on)  # com a estação, o autoplay fica como estava (S → M)
    assert jukebox.end_station.await_count == int(station_on)
    player.process_next.assert_awaited_once()


@pytest.mark.critical
@pytest.mark.asyncio
async def test_the_server_radios_entry_refuses_without_premium_and_redraws_the_select(monkeypatch):
    from empiremuse.utils.music import guild_pin
    from empiremuse.utils.radio import station_chooser

    monkeypatch.setattr(guild_pin, "load_guild_pin", AsyncMock(return_value=None))
    monkeypatch.setattr(guild_pin, "replace_guild_pin", lambda message, select: [])
    hub = NS(premium=NS(fresh=AsyncMock(return_value=None)), stations=NS(list_for_guild=AsyncMock()))
    bot = NS(pool=NS(radio=hub, i18n=NS(for_guild=AsyncMock(return_value=tr))))
    inter = NS(
        guild=NS(),
        guild_id=1,
        message=NS(),
        response=NS(defer=AsyncMock()),
        edit_original_message=AsyncMock(),
        followup=NS(send=AsyncMock()),
    )
    await station_chooser.open_server_stations(bot, inter)
    inter.followup.send.assert_awaited_once_with("radio.refused.no_premium", ephemeral=True)
    inter.edit_original_message.assert_awaited_once()  # o select fixo volta (sem a entrada, com o premium perdido)
    hub.stations.list_for_guild.assert_not_awaited()
