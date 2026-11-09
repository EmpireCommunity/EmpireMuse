"""Dublês do status automático do canal de voz (2.1.2): relógio e esperas falsos, bot que registra cada PUT e player com os métodos REAIS.

O ``JogadorDeVoz`` herda o ``PlayerVoiceMixin`` e o ``PlayerStateMixin`` (``process_destroy``) de produção; só o que é do Discord e do
Lavalink é dublê. Convenções que o contrato não fixa e que os testes assumem (o Turbine segue estas, ou avisa para ajustarmos):

* o tempo é lido por ``time.monotonic()`` (``import time`` no módulo) e as esperas são ``asyncio.sleep(...)``; nada de ``loop.call_later``;
* o bot de produção cria o bucket por bot sob demanda: o ``BotDeVoz`` é um dublê comum, sem atributo do bucket;
* as tarefas de fundo nascem por ``self.bot.loop.create_task`` ou ``asyncio.create_task`` (o ``bot.loop`` do dublê é o laço em execução).
"""

import asyncio
import heapq
import itertools
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from empiremuse.utils.music.models.player_state import PlayerStateMixin
from empiremuse.utils.music.models.player_voice import PlayerVoiceMixin

SERVIDOR = 4242
ID_DO_BOT = 7
ID_DO_CANAL = 555
BIT_48 = disnake.Permissions(1 << 48)
SEM_BIT = disnake.Permissions(manage_channels=True)
REQUISITANTE = 7777777
ID_DO_EMOJI = 123456789012345678
TEMPO_DE_ASSENTAR = 2000  # voltas (não é espera): limite de segurança do detector de laço ocioso

_SLEEP_REAL = asyncio.sleep


class RelogioFalso:
    """``time.monotonic`` e ``asyncio.sleep`` falsos: o tempo só anda em ``avancar`` e ninguém espera de verdade."""

    def __init__(self, inicio: float = 1000.0) -> None:
        self.agora = inicio
        self._esperas: list[tuple[float, int, asyncio.Future[None]]] = []
        self._ordem = itertools.count()
        self.pedidos: list[float] = []

    def monotonic(self) -> float:
        return self.agora

    async def sleep(self, atraso: float, result: Any = None) -> Any:
        self.pedidos.append(atraso)
        if atraso <= 0:
            await _SLEEP_REAL(0)
            return result
        futuro: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._esperas, (self.agora + atraso, next(self._ordem), futuro))
        await futuro
        return result

    async def avancar(self, segundos: float) -> None:
        """Anda ``segundos``, soltando cada espera vencida na ordem e deixando as tarefas rodarem até ficarem ociosas."""
        alvo = self.agora + segundos
        await assentar()
        while self._esperas and self._esperas[0][0] <= alvo:
            prazo, _, futuro = heapq.heappop(self._esperas)
            self.agora = max(self.agora, prazo)
            if not futuro.done():
                futuro.set_result(None)
            await assentar()
        self.agora = alvo
        await assentar()


async def executar(relogio: RelogioFalso, corotina: Any, limite: float = 30.0) -> Any:
    """Roda ``corotina`` até o fim; se ela esperar o relógio falso (bucket, jitter), anda o relógio aos poucos em vez de travar o teste."""
    tarefa = asyncio.ensure_future(corotina)
    gasto = 0.0
    await assentar()
    while not tarefa.done():
        if gasto >= limite:
            tarefa.cancel()
            raise AssertionError(f"a chamada não terminou em {limite} s de relógio falso")
        await relogio.avancar(0.25)
        gasto += 0.25
    return tarefa.result()


def _bloqueada(tarefa: "asyncio.Task[Any]") -> bool:
    """A tarefa espera um futuro AINDA não resolvido (um futuro já resolvido significa que ela vai rodar no próximo passo do laço)."""
    esperando = getattr(tarefa, "_fut_waiter", None)
    return esperando is not None and not esperando.done()


async def assentar() -> None:
    """Cede ao laço até que toda outra tarefa esteja esperando um futuro (ou tenha terminado); sem relógio, sem adivinhar voltas."""
    for _ in range(TEMPO_DE_ASSENTAR):
        outras = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        if all(_bloqueada(t) for t in outras):
            return
        await _SLEEP_REAL(0)
    raise AssertionError("as tarefas de fundo nunca ficaram ociosas")


def instalar_relogio(monkeypatch: pytest.MonkeyPatch) -> RelogioFalso:
    """Troca ``time.monotonic`` e ``asyncio.sleep`` pelo relógio falso até o fim do teste (cada arquivo expõe isto como a fixture ``relogio``)."""
    falso = RelogioFalso()
    monkeypatch.setattr(time, "monotonic", falso.monotonic)
    monkeypatch.setattr(asyncio, "sleep", falso.sleep)
    return falso


@dataclass(frozen=True)
class Put:
    canal: int
    status: str | None
    quando: float


class BotDeVoz:
    """O bot do servidor: cada ``PUT /voice-status`` vira um ``Put``; ``erros`` é consumida uma a uma e ``porta`` segura o envio."""

    def __init__(self, *, id_: int = ID_DO_BOT) -> None:
        self.user = SimpleNamespace(id=id_)
        self.loop = asyncio.get_running_loop()
        self.puts: list[Put] = []
        self.erros: list[Exception] = []
        self.porta: asyncio.Event | None = None
        self.reter_canal: int | None = (
            None  # PUT de TEXTO para este canal fica preso em ``porta_texto`` até o teste soltar
        )
        self.porta_texto = asyncio.Event()
        self.app_emojis: dict[str, str] = {}
        self.pool = SimpleNamespace(emoji_data={"music_sources": {}}, get_all_bots=lambda: [self])
        self.config: dict[str, Any] = {}
        self.dispatch = MagicMock()
        self.documentos: dict[int, dict[str, Any]] = {}
        self.leituras = 0  # quantas vezes o global do servidor foi lido

    async def edit_voice_channel_status(
        self, status: str | None, *, channel_id: int, reason: str | None = None
    ) -> None:
        self.puts.append(Put(channel_id, status, time.monotonic()))
        if self.porta is not None and status is None:
            await self.porta.wait()
        if status is not None and channel_id == self.reter_canal:
            await self.porta_texto.wait()
        if self.erros:
            raise self.erros.pop(0)

    async def get_global_data(self, guild_id: int, *, db_name: Any = None) -> dict[str, Any]:
        self.leituras += 1
        return self.documentos.setdefault(guild_id, {})

    @property
    def textos(self) -> list[str]:
        return [p.status for p in self.puts if p.status is not None]

    @property
    def limpezas(self) -> list[Put]:
        return [p for p in self.puts if p.status is None]


def canal_de_voz(id_: int = ID_DO_CANAL, *, permitido: bool = True) -> MagicMock:
    canal = MagicMock(spec=disnake.VoiceChannel)
    canal.id = id_
    canal.mention = f"<#{id_}>"
    canal.members = []
    canal.permissions_for.return_value = BIT_48 if permitido else SEM_BIT
    return canal


def palco(id_: int = 888) -> MagicMock:
    canal = MagicMock(spec=disnake.StageChannel)
    canal.id = id_
    canal.mention = f"<#{id_}>"
    canal.members = []
    canal.instance = None
    canal.create_instance = AsyncMock()
    canal.permissions_for.return_value = BIT_48
    return canal


def servidor(canal: Any, *, id_: int = SERVIDOR) -> SimpleNamespace:
    membro = SimpleNamespace(id=REQUISITANTE, display_name="Fulano", mention=f"<@{REQUISITANTE}>")
    me = SimpleNamespace(
        id=ID_DO_BOT, voice=SimpleNamespace(channel=canal), guild_permissions=disnake.Permissions(manage_guild=True)
    )
    return SimpleNamespace(id=id_, me=me, get_member=lambda _uid: membro, voice_client=None)


def faixa(titulo: str = "Musica", autor: str = "Artista", fonte: str = "spotify", **extra: Any) -> SimpleNamespace:
    base = {
        "title": f"{autor} - {titulo}",
        "single_title": titulo,
        "authors_string": autor,
        "duration": 200_000,
        "is_stream": False,
        "requester": REQUISITANTE,
        "playlist_name": None,
        "info": {"sourceName": fonte},
        "track_loops": 0,
    }
    return SimpleNamespace(**(base | extra))


class _BaseDoLavalink:
    async def destroy(self, *, force: bool = False, guild: Any = None) -> None:
        return None


class JogadorDeVoz(PlayerVoiceMixin, PlayerStateMixin, _BaseDoLavalink):
    """Player de produção sem Discord: os métodos de voz e o ``process_destroy`` são os reais."""

    def __init__(self, guilda: Any, bot: BotDeVoz, *, atual: Any = None, template: str = "{track.title}") -> None:
        self.guild, self.bot, self.guild_id = guilda, bot, guilda.id
        self.current = self.last_track = atual
        self.stage_title_event = False
        self.stage_title_template = template
        self.last_stage_title = ""
        self.voice_status_permanent = True
        self.voice_status_pending = None
        self.auto_pause = self.paused = False
        self.position = 0
        self.update = False
        self.is_closing = False
        self.last_channel = getattr(guilda.me.voice, "channel", None)
        self.set_command_log = MagicMock()
        self.cleanup = AsyncMock()
        self.process_save_queue = AsyncMock()
        self.start_time = None
        self.message = self.text_channel = None
        self.static = self.has_thread = False
        self.set_volume = AsyncMock()

    @property
    def avisos(self) -> list[str]:
        return [str(c.kwargs.get("text") or (c.args[0] if c.args else "")) for c in self.set_command_log.call_args_list]


def jogador(
    *, canal: Any = None, atual: Any = None, template: str = "{track.title}", bot: BotDeVoz | None = None
) -> tuple[JogadorDeVoz, BotDeVoz]:
    bot = bot or BotDeVoz()
    canal = canal or canal_de_voz()
    return JogadorDeVoz(servidor(canal), bot, atual=atual if atual is not None else faixa(), template=template), bot
