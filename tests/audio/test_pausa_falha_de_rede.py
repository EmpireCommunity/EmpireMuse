"""V-142-2 (D6): faixas SEGUIDAS abandonadas pela causa genérica de rede pausam o player, com a fila guardada e UM aviso.

O orçamento por faixa não muda (coberto em ``test_playback_retry_youtube``). Aqui só a sequência: zera quando uma faixa toca, e ao
chegar em ``NETWORK_ABANDON_PAUSE_LIMIT`` o player para de tentar até alguém retomar.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import empiremuse.wavelink as wavelink
from empiremuse.utils.music.models import LavalinkPlayer, event_steps
from empiremuse.utils.music.models.event_steps import (
    NETWORK_ABANDON_PAUSE_LIMIT,
    _he_dispatch,
    resume_after_network_pause,
    store_network_resume_position,
)
from tests.audio.test_playback_retry_youtube import fail, make_player, make_track

pytestmark = [pytest.mark.asyncio, pytest.mark.regression]

REDE = "java.net.SocketTimeoutException: Connect timed out"
CARGAS_POR_FAIXA = 7  # 6 novas tentativas e a desistência


@pytest.fixture(autouse=True)
def sem_espera(monkeypatch):
    monkeypatch.setattr(event_steps.asyncio, "sleep", AsyncMock())


def mundo(faixas: int):
    tracks = [make_track(source="soundcloud", uri=f"https://example.invalid/{i}") for i in range(faixas)]
    player = make_player(tracks[0])
    player.text_channel = SimpleNamespace(
        send=AsyncMock(), permissions_for=lambda me: SimpleNamespace(send_messages=True)
    )
    player.update_message = AsyncMock()
    player.paused = False
    return player, tracks


async def abandonar(player, track) -> None:
    """A faixa falha por rede até o orçamento dela acabar (o ``process_next`` de mentira é a troca manual da faixa atual)."""
    player.current = track
    for _ in range(CARGAS_POR_FAIXA):
        await _he_dispatch(player, fail(player, REDE))
        if not player.queue or player.queue[0] is not track:
            return
        player.current = player.queue.popleft()


def avisos(player) -> list[str]:
    return [call.args[0] for call in player.text_channel.send.await_args_list if call.args]


async def test_consecutive_network_abandons_pause_the_player_keep_the_queue_and_warn_once():
    player, tracks = mundo(NETWORK_ABANDON_PAUSE_LIMIT + 1)
    restante = tracks[-1]
    player.queue.append(restante)

    for track in tracks[:NETWORK_ABANDON_PAUSE_LIMIT]:
        await abandonar(player, track)
    proximas_antes, relatos_antes = player.process_next.await_count, player.report_error.await_count

    assert player.paused is True and player.locked is False
    assert list(player.queue) == [restante]
    assert avisos(player) == [player.tr("player.network_pause")]
    ultima = tracks[NETWORK_ABANDON_PAUSE_LIMIT - 1]
    assert player.current is ultima and ultima not in player.failed_tracks  # fica à espera: a retomada tenta de novo
    assert player.failed_tracks == tracks[: NETWORK_ABANDON_PAUSE_LIMIT - 1]

    # o eco da faixa abandonada (fim por falha de carga) não tira o player da pausa nem gera outro aviso
    await _he_dispatch(
        player, wavelink.TrackEnd({"track": ultima, "player": player, "node": player.node, "reason": "loadFailed"})
    )
    assert player.paused is True and len(avisos(player)) == 1
    assert (player.process_next.await_count, player.report_error.await_count) == (proximas_antes, relatos_antes)


@pytest.mark.parametrize("reason", ["finished", "stopped", "replaced"])
async def test_a_track_that_played_resets_the_streak(reason):
    player, tracks = mundo(NETWORK_ABANDON_PAUSE_LIMIT + 1)

    for track in tracks[: NETWORK_ABANDON_PAUSE_LIMIT - 1]:
        await abandonar(player, track)
    await _he_dispatch(
        player, wavelink.TrackEnd({"track": tracks[0], "player": player, "node": player.node, "reason": reason})
    )
    await abandonar(player, tracks[-1])

    assert player.paused is False
    assert avisos(player) == []
    assert tracks[-1] in player.failed_tracks


async def test_resuming_puts_the_waiting_track_back_and_plays():
    player, tracks = mundo(NETWORK_ABANDON_PAUSE_LIMIT)
    for track in tracks:
        await abandonar(player, track)
    player.process_next.reset_mock()

    assert await resume_after_network_pause(player) is True

    assert player.paused is False and player.current is None
    assert list(player.queue) == [tracks[-1]]
    player.process_next.assert_awaited_once()
    assert await resume_after_network_pause(player) is False  # pausa comum: quem retoma é o nó


async def test_seek_while_network_paused_is_the_restart_point_of_the_resume():
    """Revisão do PR 255: o seek na pausa por rede não vai ao nó (não há faixa lá); a posição é onde a retomada recomeça."""
    player, tracks = mundo(NETWORK_ABANDON_PAUSE_LIMIT)
    for track in tracks:
        await abandonar(player, track)
    player.process_next.reset_mock()

    assert store_network_resume_position(player, 42_000) is True
    assert await resume_after_network_pause(player) is True

    player.process_next.assert_awaited_once_with(start_position=42_000)
    assert store_network_resume_position(player, 1_000) is False  # fora da pausa por rede o seek vai ao nó


async def test_node_change_during_the_pause_sends_nothing_and_set_pause_resumes_from_the_queue():
    """Pela entrada real (``LavalinkPlayer.set_pause(False)``): trocar de nó na pausa por rede não reenvia a faixa (não há faixa nossa
    no nó), e retomar não despausa o nó: a faixa que esperava volta à frente da fila e toca de novo."""
    player, tracks = mundo(NETWORK_ABANDON_PAUSE_LIMIT)
    for track in tracks:
        await abandonar(player, track)
    player.process_next.reset_mock()
    novo = SimpleNamespace(identifier="N2", is_available=True, players={}, version=4, update_player=AsyncMock())
    player.guild_id, player.auto_pause, player._voice_state, player.volume = player.guild.id, False, {}, 100
    player.node = SimpleNamespace(
        identifier="N1",
        version=4,
        session_id=None,
        players={player.guild_id: player},
        _client=SimpleNamespace(get_node=lambda _: novo),
    )

    await wavelink.Player.change_node(player, "N2")
    await LavalinkPlayer.set_pause(player, False)

    assert player.node is novo and novo.players == {player.guild_id: player}
    novo.update_player.assert_not_awaited()
    assert player.paused is False and player.network_paused is False
    assert list(player.queue) == [tracks[-1]]
    player.process_next.assert_awaited_once_with(start_position=0)
    player.bot.dispatch.assert_called_with("player_resume", player=player)


async def test_a_seek_during_the_pause_survives_a_resume_that_waits_for_a_node():
    """CodeRabbit no PR 255: sem nó na retomada, ``process_next`` só agenda ``_wait_for_new_node``; a posição do seek não pode ter sido
    gasta antes, senão a faixa recomeça do zero quando o nó volta."""
    from empiremuse.utils.music.models.player_voice import PlayerVoiceMixin

    player, tracks = mundo(NETWORK_ABANDON_PAUSE_LIMIT)
    for track in tracks:
        await abandonar(player, track)
    store_network_resume_position(player, 42_000)
    await resume_after_network_pause(player)  # o process_next de mentira faz o papel de "sem nó: só agenda a espera"
    player.process_next.reset_mock()

    novo = SimpleNamespace(identifier="N2", is_available=True)
    player.guild_id = player.guild.id
    player.node = SimpleNamespace(identifier="N1", is_available=False, players={player.guild_id: player})
    player.bot.music.get_best_node = lambda ignore_node=None: novo
    player.guild.me.voice, player._voice_state, player.auto_pause = object(), None, False
    await PlayerVoiceMixin._wait_for_new_node(player)

    player.process_next.assert_awaited_once_with(start_position=42_000)
