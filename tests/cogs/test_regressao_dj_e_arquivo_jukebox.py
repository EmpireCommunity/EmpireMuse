"""Dois bugs antigos: ``/remove_dj`` de quem não é DJ não recusava (faltava o ``raise``) e o arquivo de formato inválido no pedido da Jukebox
mostrava o aviso do limite de 18 MB."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import disnake
import pytest

from empiremuse.cogs.music import Music
from empiremuse.i18n.service import get_i18n
from empiremuse.music_cog.jukebox_steps import _sr_empty_content
from empiremuse.utils.music.errors import GenericError

pytestmark = pytest.mark.regression

GUILD_ID = 10


def _guild(member):
    return SimpleNamespace(
        id=GUILD_ID, preferred_locale="pt-BR", get_member=lambda _id: member, me=SimpleNamespace(voice=None)
    )


@pytest.mark.asyncio
async def test_remove_dj_de_quem_nao_e_dj_e_recusado_e_nao_mexe_na_lista():
    membro = SimpleNamespace(id=5, mention="<@5>")
    guild = _guild(membro)
    player = SimpleNamespace(player_creator=1, dj={7}, static=False, controller_link="")
    bot = SimpleNamespace(
        music=SimpleNamespace(players={GUILD_ID: player}),
        get_channel=lambda _id: None,
        pool=SimpleNamespace(i18n=get_i18n()),
    )
    inter = SimpleNamespace(
        bot=bot,
        guild=guild,
        channel=SimpleNamespace(id=3),
        author=SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=False)),
        send=AsyncMock(),
    )
    cog = SimpleNamespace(interaction_message=AsyncMock())

    with pytest.raises(GenericError, match="não está na lista de DJ"):
        await Music.remove_dj.callback(cog, inter, user=SimpleNamespace(id=5))

    assert player.dj == {7}
    cog.interaction_message.assert_not_awaited()


def _mensagem(tipo_arquivo):
    anexo = SimpleNamespace(size=1000, content_type=tipo_arquivo, url="https://cdn.example/a.bin")
    return SimpleNamespace(
        content="",
        type=disnake.MessageType.default,
        is_system=lambda: False,
        attachments=[anexo],
        author=SimpleNamespace(mention="<@9>"),
        guild=SimpleNamespace(id=GUILD_ID, preferred_locale="pt-BR"),
        channel=SimpleNamespace(send=AsyncMock()),
    )


def _cog():
    return SimpleNamespace(
        bot=SimpleNamespace(pool=SimpleNamespace(i18n=get_i18n())),
        audio_formats=Music.audio_formats,
        delete_message=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_arquivo_de_formato_invalido_nao_fala_do_limite_de_18mb():
    mensagem = _mensagem("image/png")

    recusou = await _sr_empty_content(_cog(), SimpleNamespace(message=mensagem))

    texto = mensagem.channel.send.await_args.args[0]
    assert recusou is True
    assert "18mb" not in texto and "mp3" in texto


@pytest.mark.asyncio
async def test_arquivo_grande_demais_continua_falando_do_limite():
    mensagem = _mensagem("audio/mpeg")
    mensagem.attachments[0].size = 18_000_001

    await _sr_empty_content(_cog(), SimpleNamespace(message=mensagem))

    assert "18mb" in mensagem.channel.send.await_args.args[0]
