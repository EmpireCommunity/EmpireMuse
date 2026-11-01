"""F3-6b, etapa 0 — caracterização: retomar o player SEM ``voice_state`` reconecta por ``connect`` (insumo da etapa 5).

A etapa 5 deixa de persistir ``voice_state`` (evento de voz do Discord, efêmero e por conexão) e ``extra_info``: o
``resume_player`` passa a receber a sessão sem esses campos. Estes testes fixam o que já é verdade HOJE com o
``PlayerSession.resume_player`` REAL (dublês de canal, servidor, nó e player) e com o ``LavalinkPlayer._wait_for_new_node``
REAL:

* sem ``voice_state`` (ausente, ``None`` ou ``{}``) o ``resume_player`` reconecta por ``player.connect(voice_channel.id)``,
  no nó v3 e no v4, e o player nasce com ``_voice_state`` ``None``: a reconexão nunca dependeu do campo gravado;
* com ``voice_state`` ou ``extra_info`` na entrada (a etapa 5 não os grava nem lê) o resultado é o mesmo e o player nasce sem eles;
* o código de hoje já descarta o ``voice_state`` quando o canal de voz muda;
* ``_wait_for_new_node`` com ``_voice_state`` falso troca de nó (``change_node``) e, fora da voz, conecta
  (``connect``); só com ``_voice_state`` verdadeiro reenvia a atualização de voz.
"""

from collections import deque
from types import SimpleNamespace

import disnake
import pytest

from empiremuse.i18n import get_i18n
from empiremuse.cogs import player_resume
from empiremuse.utils.music import models
from empiremuse.utils.music.models import LavalinkPlayer
from tests.support.formatos_reais import (
    CANAL_DE_TEXTO,
    CANAL_DE_VOZ,
    INSTANTE,
    SERVIDOR,
    cog_de_sessoes,
)
from tests.support.formato_sessao import gravar, jogador_completo


@pytest.fixture(autouse=True)
def _ambiente(monkeypatch):
    monkeypatch.setattr(disnake.utils, "utcnow", lambda: INSTANTE)
    monkeypatch.setattr(player_resume, "can_send_message", lambda *args, **kwargs: None)
    monkeypatch.setattr(player_resume, "can_connect", lambda *args, **kwargs: None)


# ============================================================================ dublês do resume_player


class JogadorRetomado:
    """O que ``resume_player`` e ``update_player`` pedem ao player; guarda cada chamada e cada atributo atribuído."""

    def __init__(self, node, **kwargs):
        self.node, self.kwargs = node, kwargs
        self.queue, self.played, self.queue_autoplay = deque(), deque(), deque()
        self.failed_tracks, self.command_log_list = [], deque(maxlen=10)
        self.filters, self.volume, self.controller_mode = {}, 100, True
        self.keep_connected, self.auto_pause, self.current, self.current_encoded = False, False, None, None
        self.guild = kwargs["guild"]
        self.conexoes, self.chamadas = [], []
        self.message = None

    def setup_hints(self):
        self.chamadas.append("setup_hints")

    def process_hint(self):
        self.chamadas.append("process_hint")

    async def apply_permanent_voice_status(self, cfg, *, jitter=False):
        self.chamadas.append(("apply_permanent_voice_status", cfg, jitter))

    async def connect(self, channel_id, *args, **kwargs):
        self.conexoes.append(channel_id)

    async def update_filters(self):
        self.chamadas.append("update_filters")

    def set_command_log(self, **kwargs):
        self.chamadas.append("set_command_log")

    async def process_next(self, start_position=0):
        self.chamadas.append(("process_next", start_position))

    async def invoke_np(self, **kwargs):
        self.chamadas.append("invoke_np")

    async def set_pause(self, valor):
        self.chamadas.append(("set_pause", valor))

    def start_members_timeout(self, check=True):
        self.chamadas.append("start_members_timeout")


class No:
    def __init__(self, versao):
        self.identifier, self.version, self.atualizacoes = "no-1", versao, []

    async def update_player(self, guild_id, data):
        self.atualizacoes.append((guild_id, data))


async def buscar_mensagem(message_id):
    return SimpleNamespace(id=message_id)


def canal_de_voz(canal_id=CANAL_DE_VOZ):
    membro = SimpleNamespace(bot=False, voice=SimpleNamespace(deaf=False, self_deaf=False))
    permissoes = SimpleNamespace(connect=True, read_message_history=False)
    return SimpleNamespace(
        id=canal_id,
        mention=f"<#{canal_id}>",
        name="voz",
        members=[membro],
        permissions_for=lambda quem: permissoes,
        fetch_message=buscar_mensagem,
    )


def montar(versao_do_no: int, *, canais=None):
    """Cog real com bot, servidor, canais e nó de mentira; devolve ``(cog, nó, jogadores criados)``."""
    cog = cog_de_sessoes()
    servidor = SimpleNamespace(id=SERVIDOR, name="servidor", me=SimpleNamespace(voice=object(), id=1))
    canais = (
        canais if canais is not None else {CANAL_DE_VOZ: canal_de_voz(), CANAL_DE_TEXTO: canal_de_voz(CANAL_DE_TEXTO)}
    )
    for canal in canais.values():
        canal.guild = servidor
    no, criados, jogadores = No(versao_do_no), [], {}

    def get_player(**kwargs):
        jogador = JogadorRetomado(no, **kwargs)
        criados.append(jogador)
        jogadores[kwargs["guild_id"]] = jogador
        return jogador

    async def get_data(*args, **kwargs):
        return {"player_controller": {"channel": None, "message_id": None}}

    cog.bot.pool.i18n = get_i18n()  # o player retomado recebe o tradutor do servidor
    cog.bot.user = SimpleNamespace(id=1)
    cog.bot.get_channel = canais.get
    cog.bot.get_guild = lambda guild_id: servidor

    async def get_global_data(*args, **kwargs):
        return {}

    cog.bot.get_data = get_data
    cog.bot.get_global_data = get_global_data
    cog.bot.get_color = lambda *args: 0
    cog.bot.music = SimpleNamespace(players=jogadores, get_best_node=lambda: no, get_player=get_player)
    cog.bot.players_resumed = {}
    return cog, no, criados


async def sessao_gravada(**campos) -> dict:
    return await gravar(cog_de_sessoes(), jogador_completo(**campos))


async def retomar(versao_do_no, dados, *, canais=None):
    cog, no, criados = montar(versao_do_no, canais=canais)
    await cog.resume_player(dados, hints=[])
    assert len(criados) == 1, "o resume_player não criou o player"
    return criados[0], no


# ============================================================================ sem voice_state: reconecta por connect


# ============================================================================ com voice_state na entrada: é ignorado


class TestVoiceStateNaEntradaEIgnorado:
    """Desde a etapa 5 a sessão não leva ``voice_state`` (não é gravado nem lido); se um dicionário o trouxer mesmo assim, o
    ``resume_player`` o ignora: o player nasce com ``_voice_state`` ``None`` e a reconexão é por ``connect``."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("versao", [3, 4])
    async def test_o_voice_state_que_vier_na_entrada_nao_e_copiado_para_o_player(self, versao):
        dados = await sessao_gravada()
        dados["voice_state"] = {
            "sessionId": "sessao-de-voz",
            "event": {"token": "t", "endpoint": "voz.exemplo.invalid"},
        }
        jogador, _ = await retomar(versao, dados)
        assert jogador._voice_state is None
        assert jogador.conexoes == [CANAL_DE_VOZ]


# ============================================================================ _wait_for_new_node com _voice_state falso


class JogadorDeTeste(LavalinkPlayer):
    """``LavalinkPlayer`` real sem ``__init__``; só substitui o que fala com Discord e com o nó."""

    position = 0
    last_channel = SimpleNamespace(id=CANAL_DE_VOZ)

    def set_command_log(self, *args, **kwargs):
        self.registros.append("set_command_log")

    async def change_node(self, identificador):
        self.registros.append(("change_node", identificador))

    async def _dispatch_voice_update(self):
        self.registros.append("_dispatch_voice_update")

    async def connect(self, channel_id, *args, **kwargs):
        self.registros.append(("connect", channel_id))

    async def process_next(self, start_position=0):
        self.registros.append(("process_next", start_position))
