"""F3-6b, etapa 0 — caracterização da fila salva do usuário no formato ATUAL (sem produção alterada).

Roda o ``Music.save_queue`` REAL (o callback do comando, com faixas das classes reais) e o ramo da fila salva do
``Music.play`` REAL, que lê o arquivo e entrega ``data["tracks"]`` ao ``process_track_cls``. O ``play`` tem muita coisa
antes e depois; aqui as dependências de Discord são dublês e o ``check_player_queue`` com as faixas lidas é o ponto de
parada (guarda o que recebeu e interrompe).

* vale antes e depois da migração para JSON: o mínimo de 3 faixas, o caminho por usuário, o conteúdo (``tracks``,
  ``created_at`` em UTC, ``guild_id``) e as faixas que voltam como as classes reais;
* o formato gravado é JSON em ``saved_queues_json/``, sem envelope; o ``.pkl`` antigo não é lido; os testes do JSON estão em
  ``tests/persistence/test_fila_salva.py``.
"""

import copy
from types import SimpleNamespace

import disnake
import pytest

from empiremuse.cogs.music import Music
from empiremuse.music_cog import play_command  # depois de ``Music``: importar a Cog recarrega os mixins
from empiremuse.utils.client import BotPool
from tests.support.formatos_reais import (
    DONO,
    INSTANTE,
    SERVIDOR,
    faixa_deezer,
    faixa_parcial,
    faixa_youtube,
    letras_sincronizadas,
)

CONSULTA = ">> [💾 Fila Salva 💾] <<"
PADROES_DO_PLAY = dict(
    position=0, force_play="no", mix=False, manual_selection=False, options=False, server=None, manual_bot_choice="no"
)


class Parou(Exception):
    """Interrompe o ``Music.play`` logo depois de ler a fila salva e entregá-la ao ``check_player_queue``."""

    def __init__(self, faixas):
        self.faixas = faixas


@pytest.fixture(autouse=True)
def _ambiente(tmp_path, monkeypatch):
    monkeypatch.setenv("EMPIREMUSE_HOME", str(tmp_path))
    monkeypatch.setattr(disnake.utils, "utcnow", lambda: INSTANTE)


def caminho_da_fila(tmp_path, usuario=DONO):
    """O ``.pkl`` do formato antigo (o bot não o lê)."""
    return tmp_path / "local_database" / "saved_queues_v1" / "users" / f"{usuario}.pkl"


class Resposta:
    def __init__(self):
        self.enviados = []

    async def defer(self, **kwargs):
        self.enviados.append(("defer", kwargs))

    def is_done(self):
        return True


class Interacao:
    def __init__(self, bot, guild, usuario=DONO):
        self.bot, self.guild, self.guild_id = bot, guild, SERVIDOR
        self.author = SimpleNamespace(id=usuario, mention=f"<@{usuario}>", voice=None)
        self.channel = SimpleNamespace(id=777)
        self.response = Resposta()
        self.embeds = []

    async def edit_original_response(self, **kwargs):
        self.embeds.append(kwargs["embed"])


def montar_cenario(faixas_atual, faixas_fila):
    """Cog ``Music`` real (sem ``__init__``) com um bot e um player mínimos; devolve ``(cog, interação)``."""
    guild = SimpleNamespace(id=SERVIDOR, me=SimpleNamespace(voice=SimpleNamespace(channel=None)))
    player = SimpleNamespace(current=faixas_atual, queue=list(faixas_fila), node=SimpleNamespace())
    bot = SimpleNamespace(
        music=SimpleNamespace(players={SERVIDOR: player}),
        user=SimpleNamespace(id=1),
        get_guild=lambda guild_id: guild,
        get_color=lambda *args: 0,
        get_global_command_named=lambda *args, **kwargs: None,
        default_prefix="!!",
        config={"ENABLE_DISCORD_URLS_PLAYBACK": False},
        pool=SimpleNamespace(
            process_track_cls=lambda *args, **kwargs: BotPool.process_track_cls(None, *args, **kwargs)
        ),
    )

    async def dados_globais(*args, **kwargs):
        return {"prefix": ""}

    bot.get_global_data = dados_globais
    cog = Music.__new__(Music)
    cog.bot = bot
    return cog, Interacao(bot, guild)


async def salvar(faixas, *, atual=None):
    cog, inter = montar_cenario(atual, faixas)
    await Music.save_queue.callback(cog, inter)
    return cog, inter


def tres_faixas():
    return [faixa_youtube(1, letras=letras_sincronizadas(5)), faixa_deezer(2), faixa_parcial(3)]


def infos(faixas):
    return [copy.deepcopy(faixa.info) for faixa in faixas]


def montar_leitura(monkeypatch, faixas_lidas):
    """Prepara o ``Music.play`` para ler a fila salva; devolve ``executar()`` que roda e devolve as faixas recebidas."""
    cog, inter = montar_cenario(None, [])
    guild_data = {"player_controller": {"channel": "0", "message_id": "0", "fav_links": {}}}

    async def sem_canal(*args, **kwargs):
        return False

    async def nada(*args, **kwargs):
        return None

    async def dados(*args, **kwargs):
        return {"fav_links": {}, "integration_links": {}, "last_tracks": []}

    async def parar(autor, bot, guild_id, faixas=None):
        if faixas is not None:
            raise Parou(faixas)

    inter.channel.id = 777
    inter.author.voice = SimpleNamespace(channel=SimpleNamespace(id=555, voice_states={1: object()}, mention="<#555>"))
    inter.guild.me.voice = SimpleNamespace(channel=inter.author.voice.channel)
    inter.guild.voice_client = object()
    inter.guild.get_member = lambda user_id: inter.author
    inter.message = None
    inter.music_bot = inter.bot
    inter.music_guild = inter.guild
    cog.is_jukebox_channel = sem_canal
    cog.check_player_queue = parar
    cog.bot.get_data = lambda *args, **kwargs: _devolver(guild_data)
    cog.bot.get_global_data = dados
    cog.bot.get_channel = lambda channel_id: SimpleNamespace(id=channel_id)
    monkeypatch.setattr(play_command, "can_send_message", lambda *args, **kwargs: None)
    monkeypatch.setattr(play_command, "check_player_perm", nada)
    monkeypatch.setattr(play_command, "check_channel_limit", lambda *args, **kwargs: True)

    async def rodar():
        """Roda o ``Music.play`` sem capturar nada: o teste vê o ``Parou`` (leitura ok) ou o erro que a leitura levantou."""
        await Music.play.callback(cog, inter, CONSULTA, **PADROES_DO_PLAY)

    async def executar():
        with pytest.raises(Parou) as parada:
            await rodar()
        return parada.value.faixas

    executar.rodar = rodar
    executar.inter = inter
    return executar


async def _devolver(valor):
    return valor


async def ler(monkeypatch):
    return await montar_leitura(monkeypatch, None)()
