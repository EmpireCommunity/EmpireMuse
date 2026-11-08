"""T-418: mover o próprio bot atualiza o canal usado pelo status e pela retomada."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import disnake
import pytest

from empiremuse.music_cog import player_lifecycle
from empiremuse.music_cog.player_lifecycle import PlayerLifecycleMixin
from empiremuse.utils.music.models import LavalinkPlayer
from tests.support.formato_sessao import gravar
from tests.support.formatos_reais import INSTANTE, cog_de_sessoes, jogador_falso
from tests.support.voz_auto import canal_de_voz, palco


class _Lifecycle(PlayerLifecycleMixin):
    pass


def montar(destino):
    origem = canal_de_voz(1001)
    player = jogador_falso()
    player._last_channel = origem
    player._last_channel_id = origem.id
    player.last_channel = origem
    player.bot.user = SimpleNamespace(id=42)
    player.guild.me.voice.channel = destino
    player.bot.get_global_data = AsyncMock(return_value={})
    player.voice_status_apply = None
    player.disable_voice_status = AsyncMock()
    player.apply_permanent_voice_status = AsyncMock()
    player.start_members_timeout = Mock()
    player.reconnect_voice_channel_task = Mock()
    player.reconnect_voice_channel = AsyncMock()
    player.bot.loop = SimpleNamespace(create_task=Mock(side_effect=lambda coro: coro.close()))
    cog = _Lifecycle()
    cog.bot = player.bot
    membro = SimpleNamespace(id=42, bot=True, guild=player.guild)
    return cog, player, membro, origem


@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize("destino", [canal_de_voz(1002), palco(1003)], ids=["voz", "palco"])
async def test_bot_moved_updates_fallback_before_follow_move_and_session_save(destino, monkeypatch):
    monkeypatch.setattr(disnake.utils, "utcnow", lambda: INSTANTE)
    cog, player, membro, origem = montar(destino)
    canais_no_apply = []

    async def aplicar(_cfg):
        # Exercita o fallback real, como quando o cache de voz fica indisponível.
        voice = player.guild.me.voice
        player.guild.me.voice = None
        try:
            canais_no_apply.append(LavalinkPlayer.last_channel.fget(player))
        finally:
            player.guild.me.voice = voice

    player.apply_permanent_voice_status.side_effect = aplicar
    await cog.player_vc_disconnect(membro, SimpleNamespace(channel=origem), SimpleNamespace(channel=destino))

    assert player._last_channel is destino
    assert player._last_channel_id == destino.id
    assert canais_no_apply == [destino]
    player.disable_voice_status.assert_awaited_once_with(channel=origem)
    dados = await gravar(cog_de_sessoes(), player)
    assert dados["voice_channel"] == destino.id
    assert dados["last_voice_channel_id"] == destino.id


@pytest.mark.asyncio
@pytest.mark.parametrize("evento", ["outro-bot", "membro", "saida", "mesmo-canal"])
async def test_other_voice_events_keep_last_channel(evento, monkeypatch):
    cog, player, membro, origem = montar(canal_de_voz(1002))
    destino = player.guild.me.voice.channel
    if evento == "outro-bot":
        membro.id = 99
    elif evento == "membro":
        membro.id, membro.bot = 99, False
        player.auto_pause = True
    elif evento == "saida":
        destino = None
        player.guild.me.voice = None
    else:
        destino = origem
    dormir = AsyncMock()
    monkeypatch.setattr(player_lifecycle.asyncio, "sleep", dormir)

    await cog.player_vc_disconnect(membro, SimpleNamespace(channel=origem), SimpleNamespace(channel=destino))

    assert player._last_channel is origem
    assert player._last_channel_id == origem.id
    if evento == "saida":
        dormir.assert_awaited_once_with(3)
        player.reconnect_voice_channel.assert_called_once_with()
        player.disable_voice_status.assert_awaited_once_with(channel=origem)
    else:
        dormir.assert_not_awaited()
    player.apply_permanent_voice_status.assert_not_awaited()
