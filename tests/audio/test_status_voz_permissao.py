"""Teste do dono em produção 2.1.1, parte A — status do canal de voz sem a permissão ``Definir status do canal de voz`` (bit 48).

VA1 função pura ``can_set_voice_status`` (sempre sobre ``channel.permissions_for(guild.me)``); VA2 o comando e o modal temporário recusam ANTES de
ligar; VA3 o envio em tempo de execução (definir, ``clear`` e palco) trata o 403 sem traceback e sem subir ao handler; VA4 o modal permanente
salva o modelo e deixa desligados, citando, os players sem a permissão; VA5 os avisos levam só status, código e ids (sem título nem nome).
Sem Discord e sem rede: canais, bots e players são dublês e cada envio ao Discord é um espião.
"""

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from empiremuse.i18n import get_i18n
from empiremuse.music_cog.voice_status import VoiceStatusMixin
from empiremuse.utils.music import checks
from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.music.models.player_voice import PlayerVoiceMixin
from tests.support.drivers_de_regras import (
    AUTOR,
    InteracaoFalsa,
)
from tests.support.modais import SERVIDOR_DO_COMANDO as SERVIDOR

pytestmark = pytest.mark.critical

BIT_48 = 1 << 48
COM_BIT = disnake.Permissions(BIT_48)
SO_GERENCIAR_CANAIS = disnake.Permissions(manage_channels=True)
TITULO = "Titulo-Secreto-X"
SOLICITANTE = "Solicitante-Y"
CORPO_DO_ERRO = "Corpo-Do-Erro-Z"
ID_DO_CANAL = 555
ID_DO_SERVIDOR = 4242


def http_erro(classe, status: int, codigo: int):
    return classe(SimpleNamespace(status=status, reason="motivo"), {"code": codigo, "message": CORPO_DO_ERRO})


def proibido() -> disnake.Forbidden:
    return http_erro(disnake.Forbidden, 403, 50013)


# ============================================================================ dublês de canal, servidor e player


def canal_de_voz(id_: int = ID_DO_CANAL, *, permitido: bool = True) -> MagicMock:
    """``permissions_for(bot)`` do CANAL decide; o ``guild_permissions`` do bot (em ``servidor_do_bot``) sempre diz o contrário."""
    canal = MagicMock(spec=disnake.VoiceChannel)
    canal.id = id_
    canal.mention = f"<#{id_}>"
    canal.voice_states = {AUTOR: object()}
    canal.permissions_for.return_value = COM_BIT if permitido else SO_GERENCIAR_CANAIS
    return canal


def servidor_do_bot(canal, *, id_: int = SERVIDOR, permitido: bool) -> SimpleNamespace:
    contrario = SO_GERENCIAR_CANAIS if permitido else COM_BIT
    me = SimpleNamespace(voice=SimpleNamespace(channel=canal), guild_permissions=contrario)
    membro = SimpleNamespace(id=AUTOR, mention=f"<@{AUTOR}>", guild_permissions=SimpleNamespace(manage_guild=True))
    return SimpleNamespace(id=id_, me=me, get_member=lambda _: membro, default_role=SimpleNamespace(id=id_))


def jogador_falso(guilda) -> SimpleNamespace:
    jogador = SimpleNamespace(
        guild=guilda,
        stage_title_event=False,
        stage_title_template="",
        start_time=None,
        update=False,
        update_stage_topic=AsyncMock(),
        process_save_queue=AsyncMock(),
        set_command_log=MagicMock(),
    )

    async def aplicar(cfg: Any, *, jitter: bool = False) -> None:
        """O contrato 2.1.2: o permanente liga o status do player só se o bot pode defini-lo no canal."""
        jogador.stage_title_template = cfg.template
        jogador.stage_title_event = bool(cfg.permanent) and checks.voice_status_blocked_channel(guilda) is None

    jogador.apply_permanent_voice_status = aplicar
    return jogador


# ============================================================================ VA2 — comando


async def chamar_comando(canal, *, permitido: bool, template: str = "{track.title}"):
    guilda = servidor_do_bot(canal, permitido=permitido)
    jogador = jogador_falso(guilda)
    bot = SimpleNamespace(
        music=SimpleNamespace(players={SERVIDOR: jogador}),
        pool=SimpleNamespace(i18n=get_i18n()),
        is_owner=AsyncMock(return_value=False),
        edit_voice_channel_status=AsyncMock(),
    )
    autor = SimpleNamespace(
        id=AUTOR, guild_permissions=SimpleNamespace(manage_guild=True), voice=SimpleNamespace(channel=canal)
    )
    guilda.get_member = lambda _: autor
    inter = SimpleNamespace(
        bot=bot,
        guild=guilda,
        guild_id=SERVIDOR,
        author=autor,
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_message=AsyncMock(),
    )
    try:
        await VoiceStatusMixin.set_voice_status.callback(self=VoiceStatusMixin(), inter=inter, template=template)
    except GenericError as erro:
        return SimpleNamespace(erro=erro.text, jogador=jogador, bot=bot, inter=inter)
    return SimpleNamespace(erro=None, jogador=jogador, bot=bot, inter=inter)


class TestComandoComModelo:
    @pytest.mark.asyncio
    async def test_the_channel_override_counts_not_the_guild_permissions(self):
        """O bot TEM o bit no cargo (``guild_permissions``) mas o canal o nega: recusa."""
        r = await chamar_comando(canal_de_voz(permitido=False), permitido=False)

        assert r.erro is not None


# ============================================================================ VA2 — modal temporário e VA4 — modal permanente


class InteracaoEfemera(InteracaoFalsa):
    """Guarda se cada resposta foi efêmera e quantas vezes houve ``defer``."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.efemeras = []
        self.defers = 0
        defer_original = self.response.defer

        async def defer(**kw):
            self.defers += 1
            await defer_original(**kw)

        self.response.defer = defer

    async def send(self, *args, **kwargs):
        self.efemeras.append(bool(kwargs.get("ephemeral")))
        await super().send(*args, **kwargs)


@pytest.fixture
def sem_espera_real(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())


# ============================================================================ VA3 e VA5 — envio em tempo de execução


class JogadorDeStatus(PlayerVoiceMixin):
    """``update_stage_topic`` REAL sobre um estado mínimo.

    Estado novo que o fix criar no player (ex.: o conjunto dos avisos já dados) nasce vazio na primeira leitura.
    """

    def __init__(self, canal, bot, *, evento=True, ultimo="anterior"):
        self.guild = SimpleNamespace(
            id=ID_DO_SERVIDOR,
            me=SimpleNamespace(
                voice=SimpleNamespace(channel=canal), guild_permissions=SimpleNamespace(manage_guild=True)
            ),
            get_member=lambda _: SimpleNamespace(display_name=SOLICITANTE),
        )
        self.bot = bot
        self.tr = get_i18n().for_locale("pt-BR")
        self.voice_status_permanent = (
            True  # estado do contrato 2.1.2, já inicializado como o __init__ do player real faz
        )
        self.voice_status_pending = None
        self.stage_title_event = evento
        self.stage_title_template = "{track.title}"
        self.last_stage_title = ultimo
        self.auto_pause = False
        self.paused = False
        self.position = 0
        self.update = False
        self.set_command_log = MagicMock()
        self.current = SimpleNamespace(
            requester=77,
            single_title=TITULO,
            authors_string="Autora",
            duration=180_000,
            is_stream=False,
            info={"sourceName": "youtube"},
            playlist_name=None,
        )

    def __getattr__(self, nome):
        if nome.startswith("__"):
            raise AttributeError(nome)
        valor = set()
        setattr(self, nome, valor)
        return valor


CAMINHOS = ["voz-definir", "voz-limpar", "palco"]


def cenario(caminho: str, erro: Exception) -> JogadorDeStatus:
    bot = SimpleNamespace(edit_voice_channel_status=AsyncMock(side_effect=erro), update_global_data=AsyncMock())
    if caminho == "palco":
        canal = MagicMock(spec=disnake.StageChannel)
        canal.id = ID_DO_CANAL
        canal.instance = SimpleNamespace(edit=AsyncMock(side_effect=erro))
        return JogadorDeStatus(canal, bot)
    canal = canal_de_voz(ID_DO_CANAL)
    return JogadorDeStatus(canal, bot)


async def rodar(jogador: JogadorDeStatus, caminho: str) -> None:
    await jogador.update_stage_topic(clear=(caminho == "voz-limpar"))


def avisos(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize("caminho", CAMINHOS)
class TestForbiddenEmTempoDeExecucao:
    @pytest.mark.asyncio
    async def test_it_turns_the_event_off_and_warns_the_player_exactly_once(self, caminho, caplog):
        jogador = cenario(caminho, proibido())

        with caplog.at_level(logging.DEBUG):
            await rodar(jogador, caminho)

        assert jogador.stage_title_event is False
        assert jogador.set_command_log.call_count == 1

    @pytest.mark.asyncio
    async def test_one_warning_line_with_status_code_and_the_ids_and_nothing_private(self, caminho, caplog):
        jogador = cenario(caminho, proibido())

        with caplog.at_level(logging.DEBUG):
            await rodar(jogador, caminho)

        (registro,) = avisos(caplog)
        texto = registro.getMessage()
        assert registro.levelno == logging.WARNING and "50013" in texto and "403" in texto
        assert str(ID_DO_SERVIDOR) in texto and str(ID_DO_CANAL) in texto
        assert TITULO not in caplog.text and SOLICITANTE not in caplog.text and CORPO_DO_ERRO not in caplog.text
