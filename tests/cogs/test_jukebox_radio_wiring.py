"""Modo rádio da Jukebox (T-434 J2a): os encaixes fora do controlador.

Entradas fixas do select só com premium (§3, §8 item 9), correção B do T-JB-BUG (``verify_on_boot`` redesenha a mensagem antiga), a retomada sem o
ao vivo (11.2, m1), a "próxima faixa" em R (§2.2), os eventos de queda (§2.4), o histórico e o RPC fora de R (m1) e o aviso da música descartada
numa transição (11.4, m3). Só fakes; ``tr`` devolve a chave.
"""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, Mock

import disnake
import pytest

import empiremuse.wavelink as wavelink
from empiremuse.music_cog.play_enqueue import PlayEnqueueMixin
from empiremuse.utils.jukebox_panel import JukeboxPanel
from empiremuse.utils.music import guild_pin
from empiremuse.utils.music.guild_pin import load_guild_pin, radio_entries
from empiremuse.utils.music.jukebox_ui import LIVE_VALUE, SERVER_VALUE
from empiremuse.utils.music.live_mark import mark_live
from empiremuse.utils.music.models import event_steps, playback_steps
from empiremuse.utils.music.models.player_events import PlayerEventsMixin
from empiremuse.utils.music.models.player_state import PlayerStateMixin
from empiremuse.utils.radio.jukebox_radio import RadioMode
from tests.support.formato_sessao import gravar
from tests.support.formatos_reais import cog_de_sessoes, faixa_youtube, jogador_falso


def tr(key, **fields):
    return key


ON_AIR = object()  # a faixa ao vivo de um modo já no ar (``mode.track`` posto)


def mode(track=ON_AIR):
    return RadioMode(uuid="u", name="Rádio", country="BR", starter_id=10, started_at=0.0, track=track)


# ---------------------------------------------------------------------- entradas fixas (§3, A1)


def bot_with(premium):
    get = AsyncMock(side_effect=premium) if isinstance(premium, Exception) else AsyncMock(return_value=premium)
    return NS(pool=NS(radio=NS(premium=NS(get=get))))


@pytest.mark.critical
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bot", "shown"),
    [
        (bot_with(object()), True),
        (bot_with(None), False),
        (bot_with(RuntimeError("db")), False),
        (NS(pool=NS()), False),
    ],
)
async def test_the_live_radio_entry_appears_only_for_a_premium_server(bot, shown):
    entries = await radio_entries(bot, tr, 1)
    assert [e.value for e in entries] == ([LIVE_VALUE, SERVER_VALUE] if shown else [])


@pytest.mark.critical
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("page", "radio", "first"), [(1, True, LIVE_VALUE), (2, True, "jb:page:1"), (1, False, "pl:L0")]
)
async def test_the_fixed_entry_is_only_on_the_public_first_page(monkeypatch, page, radio, first):
    from empiremuse.rules.playlists import Playlist

    items = tuple(Playlist(i, f"L{i}", "", (), f"https://example.com/{i}", "🎧") for i in range(40))
    monkeypatch.setattr(guild_pin, "safe_list", AsyncMock(return_value=items))
    select = await load_guild_pin(bot_with(object()), tr, 1, page, radio=radio)
    assert select.options[0].value == first
    assert len(select.options) <= 25
    assert sum(o.value == LIVE_VALUE for o in select.options) == (1 if first == LIVE_VALUE else 0)


# ---------------------------------------------------------------------- correção B (T-JB-BUG)


def boot_world(messages):
    guilds = [NS(id=g) for g in messages]
    bot = NS(guilds=guilds, user=NS(id=900))
    pool = NS(
        servers=NS(
            jukebox=NS(
                get=AsyncMock(return_value=NS(channel=5)),
                is_panel_bot=AsyncMock(return_value=True),
            )
        )
    )
    panel = JukeboxPanel.__new__(JukeboxPanel)
    panel.pool = pool
    panel.handoff = AsyncMock()
    panel.refresh = AsyncMock(side_effect=lambda gid, now=False: _maybe_fail(messages[gid]))
    return panel, bot


def _maybe_fail(message):
    if message == "boom":
        raise RuntimeError("edição falhou")


@pytest.mark.regression
@pytest.mark.asyncio
async def test_boot_redraws_an_existing_panel_once_and_a_failure_does_not_stop_the_others(monkeypatch):
    """T-JB-BUG: a mensagem fixada antes de uma atualização mantinha os componentes velhos (sem a entrada de rádio) até um evento."""
    ours = NS(author=NS(id=900))
    messages = {1: ours, 2: "boom", 3: None}
    panel, bot = boot_world(messages)

    async def fetch(pool, guild_id):
        value = messages[guild_id]
        return ours if value == "boom" else value

    monkeypatch.setattr("empiremuse.utils.jukebox_panel.fetch_panel_message", fetch)
    await panel.verify_on_boot(bot)
    assert [c.args[0] for c in panel.refresh.await_args_list] == [1, 2]
    assert all(c.kwargs == {"now": True} for c in panel.refresh.await_args_list)
    panel.handoff.assert_awaited_once_with(3)  # sem mensagem: a passagem manda um painel novo, sem redesenho


# ---------------------------------------------------------------------- retomada (11.2, m1)


@pytest.mark.critical
@pytest.mark.asyncio
async def test_the_saved_session_never_carries_the_live_track_or_its_encoded_id(tmp_path, monkeypatch):
    monkeypatch.setenv("EMPIREMUSE_HOME", str(tmp_path))
    live = faixa_youtube(0)
    player = jogador_falso(atual=live, fila=[], tocadas=[faixa_youtube(4)], radio_mode=mode(live))
    data = await gravar(cog_de_sessoes(), player)
    assert data["queue"] == [] and "current_encoded" not in data and data["position"] == 0
    assert "radio_mode" not in data and len(data["played"]) == 1  # recentes intactos
    assert "127.0.0.1" not in repr(data) and "/r/" not in repr(data)


# ---------------------------------------------------------------------- próxima faixa em R (§2.2)


def next_world(queue=(), live=ON_AIR, autoplay=True):
    jukebox = NS(release=AsyncMock(return_value=True))
    player = NS(
        queue=deque(queue),
        radio_id=None,
        radio_mode=mode(live),
        autoplay=autoplay,
        keep_connected=False,
        get_autoqueue_tracks=AsyncMock(return_value="recomendação"),
        bot=NS(pool=NS(radio=NS(jukebox=jukebox))),
        process_next=AsyncMock(),
    )
    return player, jukebox


@pytest.mark.critical
@pytest.mark.asyncio
async def test_skip_in_radio_mode_ends_it_and_goes_idle_without_autoplay(monkeypatch):
    player, jukebox = next_world()
    idle = AsyncMock()
    monkeypatch.setattr(playback_steps, "go_idle", idle)
    assert await playback_steps._pn_next_track(player, NS(track=None)) is True
    jukebox.release.assert_awaited_once_with(player, player.radio_mode)  # o modo que o player viu (rev. 3.3)
    idle.assert_awaited_once_with(player)
    player.get_autoqueue_tracks.assert_not_awaited()


@pytest.mark.critical
@pytest.mark.asyncio
@pytest.mark.parametrize("is_live", [True, False])
async def test_a_song_taking_the_place_of_the_live_track_ends_radio_mode(is_live):
    live = NS(name="ao vivo")
    song = live if is_live else NS(name="música")
    player, jukebox = next_world(queue=[song], live=live)
    req = NS(track=None)
    assert await playback_steps._pn_next_track(player, req) is False
    assert req.track is song
    assert jukebox.release.await_count == (0 if is_live else 1)


# ---------------------------------------------------------------------- eventos de queda (§2.4)


@pytest.mark.critical
@pytest.mark.parametrize(
    ("event_cls", "reason", "lost"),
    [
        (wavelink.TrackEnd, "FINISHED", True),
        (wavelink.TrackEnd, "LOAD_FAILED", True),
        (wavelink.TrackEnd, "STOPPED", False),
        (wavelink.TrackEnd, "REPLACED", False),
        (wavelink.TrackException, "", True),
        (wavelink.TrackStuck, "", True),
    ],
)
def test_stream_events_in_radio_mode_never_take_the_music_path(event_cls, reason, lost):
    jukebox = NS(on_stream_lost=Mock())
    node = NS(identifier="LOCAL")
    player = NS(radio_mode=mode(NS(id="enc")), node=node, bot=NS(pool=NS(radio=NS(jukebox=jukebox))))
    event = event_cls.__new__(event_cls)
    event.node = node
    if reason:
        event.reason = reason
    assert event_steps._he_jukebox_radio(player, event) is True
    assert jukebox.on_stream_lost.call_count == (1 if lost else 0)
    player.radio_mode = None
    assert event_steps._he_jukebox_radio(player, event) is False  # fora de R: o caminho de música de sempre


# ---------------------------------------------------------------------- histórico e RPC (m1)


@pytest.mark.critical
@pytest.mark.asyncio
@pytest.mark.parametrize("still_in_radio_mode", [True, False])
async def test_the_live_track_never_enters_the_history(monkeypatch, still_in_radio_mode):
    """Crivo MA1 (b): o fim do ao vivo chega DEPOIS que o modo foi limpo (DJ pediu música): a marca na faixa ainda a deixa de fora."""
    live = NS(is_stream=True, duration=0, autoplay=False, track_loops=0, info={"extra": {}})
    mark_live(live)
    player = NS(
        votes=set(), locked=False, last_track=live, loop=False, is_previows_music=True, played=deque(), queue=deque()
    )
    player.radio_mode = mode(live) if still_in_radio_mode else None
    monkeypatch.setattr("empiremuse.utils.music.models.player_events.asyncio.sleep", AsyncMock())
    await PlayerEventsMixin.track_end(player)
    assert list(player.played) == [] and list(player.queue) == [] and player.locked is False


@pytest.mark.critical
@pytest.mark.asyncio
async def test_the_rpc_is_not_sent_in_radio_mode():
    player = MagicMock()
    player.radio_id = None
    player.radio_mode = mode()
    await PlayerStateMixin.process_rpc(player)
    player._send_rpc_data.assert_not_called()
    player.bot.loop.create_task.assert_not_called()


# ---------------------------------------------------------------------- música descartada na transição (11.4, m3)


@pytest.mark.critical
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("epoch_changed", "still_queued", "warned"), [(True, False, True), (False, False, False), (True, True, False)]
)
async def test_a_song_discarded_by_a_radio_transition_warns_only_its_requester(epoch_changed, still_queued, warned):
    song = NS(name="música")
    player = NS(radio_epoch=2 if epoch_changed else 1, queue=deque([song] if still_queued else []), current=None, tr=tr)
    inter = NS(send=AsyncMock())
    req = NS(player=player, radio_epoch=1, tracks=[song], inter=inter)
    await PlayEnqueueMixin._warn_discarded(PlayEnqueueMixin, req)
    if warned:
        inter.send.assert_awaited_once_with("jukebox.radio.discarded", ephemeral=True)
    else:
        inter.send.assert_not_awaited()


def test_disnake_still_exposes_the_select_option_used_by_the_fixed_entry():
    assert disnake.SelectOption(label="x", value=LIVE_VALUE).value == LIVE_VALUE
