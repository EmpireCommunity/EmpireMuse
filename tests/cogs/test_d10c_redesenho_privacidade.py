"""D10: o player redesenha a mensagem quando a visibilidade da call muda no meio da faixa."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import disnake
import pytest

from empiremuse.music_cog import player_lifecycle
from empiremuse.music_cog.player_lifecycle import PlayerLifecycleMixin
from empiremuse.utils.jukebox_panel import player_hides_details
from tests.support.voz_auto import canal_de_voz

PUBLICA = disnake.Permissions(view_channel=True, connect=True)  # o @everyone vê e entra
PRIVADA = disnake.Permissions.none()
TEXTO_FORA = 9001  # um canal de texto que não é o chat da call


class _Ciclo(PlayerLifecycleMixin):
    pass


@pytest.fixture
def espera(monkeypatch):
    """A espera do redesenho vira um evento: nada de relógio."""
    liberar = asyncio.Event()

    async def esperar():
        await liberar.wait()

    monkeypatch.setattr(player_lifecycle, "_privacy_wait", esperar)
    return liberar


def montar(*, publica: bool, desenhado: bool | None):
    voz = canal_de_voz(1001)
    voz.permissions_for.return_value = PUBLICA if publica else PRIVADA
    voz.guild = SimpleNamespace(id=42, default_role=object())
    jogador = SimpleNamespace(
        guild=SimpleNamespace(id=42, me=SimpleNamespace(voice=SimpleNamespace(channel=voz))),
        guild_id=42,
        is_closing=False,
        text_channel=SimpleNamespace(id=TEXTO_FORA),
        details_hidden_rendered=desenhado,
        privacy_rerender_task=None,
        invoke_np=AsyncMock(),
    )
    # o invoke_np real confirma o que desenhou
    jogador.invoke_np.side_effect = lambda: setattr(jogador, "details_hidden_rendered", player_hides_details(jogador))
    cog = _Ciclo()
    cog.bot = SimpleNamespace(music=SimpleNamespace(players={42: jogador}))
    return cog, jogador, voz


def mudou(voz, antes="a", depois="b"):
    return SimpleNamespace(overwrites=antes, guild=voz.guild, id=voz.id), SimpleNamespace(
        overwrites=depois, guild=voz.guild, id=voz.id
    )


async def rodar(jogador, liberar):
    tarefa = jogador.privacy_rerender_task
    assert tarefa is not None
    liberar.set()
    await tarefa


@pytest.mark.regression
@pytest.mark.asyncio
async def test_call_que_vira_privada_redesenha_escondendo(espera):
    cog, jogador, voz = montar(publica=True, desenhado=False)
    voz.permissions_for.return_value = PRIVADA  # o canal já vem atualizado no cache quando o evento chega
    await cog.privacy_channel_update(*mudou(voz))
    await rodar(jogador, espera)
    jogador.invoke_np.assert_awaited_once_with()
    assert player_hides_details(jogador) is True


@pytest.mark.regression
@pytest.mark.asyncio
async def test_call_que_vira_publica_redesenha_mostrando(espera):
    cog, jogador, voz = montar(publica=False, desenhado=True)
    voz.permissions_for.return_value = PUBLICA
    await cog.privacy_channel_update(*mudou(voz))
    await rodar(jogador, espera)
    jogador.invoke_np.assert_awaited_once_with()
    assert player_hides_details(jogador) is False


@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize("privada", [False, True])
async def test_sem_virada_nao_redesenha(privada):
    cog, jogador, voz = montar(publica=not privada, desenhado=privada)
    await cog.privacy_channel_update(*mudou(voz))  # a permissão mudou, o resultado não
    assert jogador.privacy_rerender_task is None
    jogador.invoke_np.assert_not_awaited()


@pytest.mark.regression
@pytest.mark.asyncio
async def test_bot_movido_para_call_privada_fora_do_chat_dela_esconde(espera):
    cog, jogador, publica = montar(publica=True, desenhado=False)
    privada = canal_de_voz(1002)
    privada.permissions_for.return_value = PRIVADA
    privada.guild = publica.guild
    jogador.guild.me.voice.channel = privada
    jogador._last_channel = publica
    jogador.bot = SimpleNamespace(user=SimpleNamespace(id=7))
    jogador.disable_voice_status = AsyncMock()
    jogador.apply_permanent_voice_status = AsyncMock()
    jogador.voice_status_apply = None
    jogador.stage_title_event = False
    jogador.bot.get_global_data = AsyncMock(return_value={})
    membro = SimpleNamespace(id=7, bot=True, guild=jogador.guild)
    await cog.player_vc_disconnect(membro, SimpleNamespace(channel=publica), SimpleNamespace(channel=privada))
    await rodar(jogador, espera)
    jogador.invoke_np.assert_awaited_once_with()
    assert player_hides_details(jogador) is True


@pytest.mark.regression
@pytest.mark.asyncio
async def test_varias_mudancas_seguidas_viram_um_redesenho(espera):
    cog, jogador, voz = montar(publica=True, desenhado=False)
    voz.permissions_for.return_value = PRIVADA
    await cog.privacy_channel_update(*mudou(voz))
    primeira = jogador.privacy_rerender_task
    await cog.privacy_channel_update(*mudou(voz, "b", "c"))
    assert jogador.privacy_rerender_task is primeira
    await rodar(jogador, espera)
    jogador.invoke_np.assert_awaited_once_with()


@pytest.mark.regression
@pytest.mark.asyncio
async def test_virada_desfeita_durante_a_espera_nao_redesenha(espera):
    cog, jogador, voz = montar(publica=True, desenhado=False)
    voz.permissions_for.return_value = PRIVADA
    await cog.privacy_channel_update(*mudou(voz))
    voz.permissions_for.return_value = PUBLICA  # voltou a ser pública antes da espera acabar
    await rodar(jogador, espera)
    jogador.invoke_np.assert_not_awaited()


@pytest.mark.regression
@pytest.mark.asyncio
async def test_permissao_do_everyone_que_vira_privada_redesenha(espera):
    cog, jogador, voz = montar(publica=True, desenhado=False)
    voz.permissions_for.return_value = PRIVADA
    antes = SimpleNamespace(is_default=lambda: True, permissions="a")
    depois = SimpleNamespace(is_default=lambda: True, permissions="b", guild=voz.guild)
    await cog.privacy_role_update(antes, depois)
    await rodar(jogador, espera)
    jogador.invoke_np.assert_awaited_once_with()


@pytest.mark.regression
@pytest.mark.asyncio
async def test_invoke_np_registra_o_que_desenhou(monkeypatch):
    """Sem esse registro o gatilho não tem com o que comparar e a mensagem velha fica como está."""
    from empiremuse.utils.music.models import player_message

    _, jogador, _ = montar(publica=False, desenhado=None)
    jogador.static = False
    jogador.retry_setup_hints = False
    jogador.controller_mode = True
    monkeypatch.setattr(player_message, "_np_guards", AsyncMock(return_value=False))
    monkeypatch.setattr(player_message, "_np_data", AsyncMock(return_value=True))
    await player_message.PlayerMessageMixin.invoke_np(jogador)
    assert jogador.details_hidden_rendered is True


@pytest.mark.regression
@pytest.mark.asyncio
async def test_virada_durante_o_redesenho_nao_se_perde(espera):
    """Um gatilho que chega com o redesenho em andamento é descartado pelo guarda; o laço confere de novo no fim."""
    cog, jogador, voz = montar(publica=True, desenhado=False)
    voz.permissions_for.return_value = PRIVADA
    chamadas = []

    async def invoke():
        chamadas.append(player_hides_details(jogador))
        if len(chamadas) == 1:
            voz.permissions_for.return_value = PUBLICA  # a call volta a ser pública durante a edição
            jogador.details_hidden_rendered = True  # a edição saiu escondendo
        else:
            jogador.details_hidden_rendered = False

    jogador.invoke_np = invoke
    await cog.privacy_channel_update(*mudou(voz))
    await rodar(jogador, espera)
    assert chamadas == [True, False]


@pytest.mark.regression
@pytest.mark.asyncio
async def test_desenho_que_falhou_nao_confirma_o_registro(monkeypatch):
    from empiremuse.utils.music.models import player_message

    _, jogador, _ = montar(publica=False, desenhado=False)
    jogador.static = False
    jogador.retry_setup_hints = False
    jogador.controller_mode = False
    monkeypatch.setattr(player_message, "_np_guards", AsyncMock(return_value=False))
    monkeypatch.setattr(player_message, "_np_data", AsyncMock(return_value=False))
    monkeypatch.setattr(player_message, "_np_plain", AsyncMock())  # não marca ``published``: a mensagem não saiu
    await player_message.PlayerMessageMixin.invoke_np(jogador)
    assert jogador.details_hidden_rendered is False


@pytest.mark.regression
@pytest.mark.asyncio
async def test_virada_ainda_por_sair_depois_das_tentativas_pede_o_atualizador(espera):
    """Os desenhos não saem (ou a call vira durante o último): sobra ``player.update`` para o atualizador da mensagem."""
    cog, jogador, voz = montar(publica=True, desenhado=False)
    jogador.update = False
    voz.permissions_for.return_value = PRIVADA
    jogador.invoke_np = AsyncMock()  # nenhum desenho confirma o registro
    await cog.privacy_channel_update(*mudou(voz))
    await rodar(jogador, espera)
    assert jogador.invoke_np.await_count == player_lifecycle.PRIVACY_RERENDER_ATTEMPTS
    assert jogador.update is True


@pytest.mark.regression
@pytest.mark.asyncio
async def test_player_fechado_durante_a_espera_nao_redesenha(espera):
    cog, jogador, voz = montar(publica=True, desenhado=False)
    voz.permissions_for.return_value = PRIVADA
    await cog.privacy_channel_update(*mudou(voz))
    jogador.is_closing = True
    await rodar(jogador, espera)
    jogador.invoke_np.assert_not_awaited()


@pytest.mark.regression
@pytest.mark.asyncio
async def test_canal_de_outro_servidor_ou_de_outra_call_nao_compara_overwrites():
    """O evento chega por qualquer canal: sem player ou com outra call, nem os ``overwrites`` são montados."""
    cog, jogador, voz = montar(publica=True, desenhado=False)

    class Explosivo:
        @property
        def overwrites(self):
            raise AssertionError("os overwrites foram lidos")

    outro = Explosivo()
    outro.guild, outro.id = SimpleNamespace(id=99), voz.id  # servidor sem player
    await cog.privacy_channel_update(outro, outro)
    mesmo_servidor = Explosivo()
    mesmo_servidor.guild, mesmo_servidor.id = voz.guild, 123456  # outro canal do servidor
    await cog.privacy_channel_update(mesmo_servidor, mesmo_servidor)
    assert jogador.privacy_rerender_task is None
