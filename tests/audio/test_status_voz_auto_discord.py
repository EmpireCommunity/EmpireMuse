"""2.1.2 — o permanente no Discord (contrato I5): modal permanente (VD1/VD2), ``/setvoicestatus <modelo>`` (VD3), um valor por servidor para todos os
bots do pool (VG1), a leitura nova do documento no envio (V-S6/VG2), o embed do menu e o dado novo (I1).

Funções de produção reais (``SetStageTitle.modal_handler``, ``VoiceStatusMixin.set_voice_status``); bots, servidor e banco são dublês. O banco de dois bots
compartilha o documento GLOBAL do servidor (``get_global_data``) e mantém o documento por bot separado: gravar no lugar errado não aparece para o outro bot.
"""

import asyncio
import copy
import importlib
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from empiremuse.utils.db import DBModel, global_db_models, update_values
from empiremuse.utils.music.interactions import SetStageTitle
from tests.audio.test_status_voz_permissao import InteracaoEfemera, canal_de_voz, servidor_do_bot
from tests.support.drivers_de_regras import AUTOR, BotDeComandos, ContextoDoStatus
from tests.support.modais import SERVIDOR_DO_COMANDO as SERVIDOR
from tests.support.voz_auto import assentar

pytestmark = [pytest.mark.critical, pytest.mark.asyncio]

MODELO = "Ouvindo {track.title}"
OUTRO_MODELO = "{track.author} - {track.title}"


@pytest.fixture(autouse=True)
def _sem_espera_real(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())


def regras() -> ModuleType:
    return importlib.import_module("empiremuse.rules.voice_status")


class BotDoPool(BotDeComandos):
    """Um bot do pool: o documento GLOBAL do servidor é compartilhado; o documento por bot é só deste."""

    def __init__(self, global_compartilhado: dict[tuple[str, str], dict[str, Any]], **kw: Any) -> None:
        super().__init__(**kw)
        self._globais = global_compartilhado

    def _doc(self, id_: Any, db_name: Any, *, escopo: str) -> dict[str, Any]:
        if escopo != "global":
            return super()._doc(id_, db_name, escopo=escopo)
        chave = (str(db_name), str(id_))
        if chave not in self._globais:
            self._globais[chave] = copy.deepcopy(global_db_models[db_name])
        return self._globais[chave]


def jogador_do_bot(canal: Any, *, permitido: bool = True) -> SimpleNamespace:
    guilda = servidor_do_bot(canal, permitido=permitido)
    return SimpleNamespace(
        guild=guilda,
        stage_title_event=False,
        stage_title_template="",
        apply_permanent_voice_status=AsyncMock(),
        disable_voice_status=AsyncMock(),
        update_stage_topic=AsyncMock(),
        process_save_queue=AsyncMock(),
        set_command_log=MagicMock(),
        start_time=None,
        update=False,
    )


def pool_de(n: int, *, permissoes: list[bool] | None = None) -> tuple[list[BotDoPool], list[SimpleNamespace], dict]:
    """``n`` bots no mesmo servidor, cada um com o SEU player e canal; o primeiro é o que recebe o modal."""
    globais: dict[tuple[str, str], dict[str, Any]] = {}
    bots = [BotDoPool(globais) for _ in range(n)]
    permissoes = permissoes or [True] * n
    jogadores = []
    for i, (bot, ok) in enumerate(zip(bots, permissoes, strict=True)):
        jogador = jogador_do_bot(canal_de_voz(100 + i, permitido=ok), permitido=ok)
        bot.guilds[SERVIDOR] = jogador.guild
        bot.music.players[SERVIDOR] = jogador
        jogadores.append(jogador)
    for bot in bots:
        bot.pool.get_all_bots = lambda todos=bots: list(todos)
    return bots, jogadores, globais


def documento_global(bot: BotDeComandos) -> dict[str, Any]:
    return bot.documento("global", DBModel.guilds, SERVIDOR)


def preparar_modal(
    bot: BotDeComandos, *, texto: str, aberto_com: dict[str, Any], autor_pode: bool = True, membro_nulo: bool = False
) -> tuple[SetStageTitle, InteracaoEfemera]:
    """A view e a interação do submit do modal permanente do ``SetStageTitle`` REAL; ``aberto_com`` é o documento do menu ao abrir."""
    guilda = bot.guilds[SERVIDOR]
    membro = SimpleNamespace(
        id=AUTOR, mention=f"<@{AUTOR}>", guild_permissions=SimpleNamespace(manage_guild=autor_pode)
    )
    guilda.get_member = lambda _uid: None if membro_nulo else membro
    view = SetStageTitle(ContextoDoStatus(), bot, guilda, copy.deepcopy(aberto_com))
    inter = InteracaoEfemera(guild_id=SERVIDOR, guild=guilda, custom_id="status_voice_channel_perm")
    inter.text_values = {"status_voice_value": texto}
    return view, inter


async def enviar_modal(
    bot: BotDeComandos, *, texto: str, aberto_com: dict[str, Any], autor_pode: bool = True, membro_nulo: bool = False
) -> InteracaoEfemera:
    view, inter = preparar_modal(
        bot, texto=texto, aberto_com=aberto_com, autor_pode=autor_pode, membro_nulo=membro_nulo
    )
    await view.modal_handler(inter)
    await assentar()  # os apply_permanent_voice_status são tarefas por player: deixa-as rodar
    return inter


# ============================================================================ I1 — o dado novo


class TestDadoNovo:
    def test_an_old_document_migrates_to_none_and_resolves_to_on(self) -> None:
        antigo = {"ver": 1.4, "prefix": "?", "voice_channel_status": ""}

        migrado = update_values(copy.deepcopy(global_db_models[DBModel.guilds]), antigo)

        assert migrado["voice_status_permanent"] is None and migrado["prefix"] == "?"
        assert regras().resolve_voice_status(migrado).permanent is True


# ============================================================================ VD1 — modal permanente


class TestModalPermanente:
    async def test_an_invalid_text_is_refused_and_nothing_is_saved_or_applied(self) -> None:
        bots, jogadores, _ = pool_de(1)

        inter = await enviar_modal(bots[0], texto="sem placeholder", aberto_com=documento_global(bots[0]))

        assert bots[0].gravacoes == [] and "placeholder" in inter.texto
        jogadores[0].apply_permanent_voice_status.assert_not_awaited()


# ============================================================================ VD2 — permissão no envio


class TestPermissaoNoEnvio:
    async def test_without_manage_guild_nothing_is_saved_or_applied(self) -> None:
        bots, jogadores, _ = pool_de(2)

        inter = await enviar_modal(bots[0], texto=MODELO, aberto_com=documento_global(bots[0]), autor_pode=False)

        assert bots[0].gravacoes == [] and "gerenciar servidor" in inter.texto
        for jogador in jogadores:
            jogador.apply_permanent_voice_status.assert_not_awaited()


# ============================================================================ autorização da sessão: o autor tem que estar no canal de um player


def pool_com_autor(no_canal_de: int | None) -> tuple[list[BotDoPool], list[SimpleNamespace]]:
    """Dois bots no servidor; o autor (com manage_guild) está no canal do bot ``no_canal_de`` (0 ou 1) ou em nenhum (``None``)."""
    bots, jogadores, _ = pool_de(2)
    membro = SimpleNamespace(id=AUTOR, mention=f"<@{AUTOR}>", guild_permissions=SimpleNamespace(manage_guild=True))
    for i, (bot, jogador) in enumerate(zip(bots, jogadores, strict=True)):
        jogador.guild.me.voice.channel.voice_states = {AUTOR: object()} if i == no_canal_de else {}
        jogador.guild.get_member = bot.guilds[SERVIDOR].get_member = lambda _uid: membro
        jogador.stage_title_event = True  # sessão ligada com um modelo de antes: nada pode mudar sem autorização
        jogador.stage_title_template = "modelo de antes {track.title}"
    return bots, jogadores


async def usar_sessao(bot: BotDoPool, modo: str) -> InteracaoEfemera:
    """``modo``: ``texto`` (modal temporário com texto), ``vazio`` (modal temporário vazio) ou ``botao`` (Usar modelo atual)."""
    guilda = bot.guilds[SERVIDOR]
    view = SetStageTitle(ContextoDoStatus(), bot, guilda, {"voice_channel_status": MODELO})
    if modo == "botao":
        inter = InteracaoEfemera(guild_id=SERVIDOR, guild=guilda, custom_id="status_voice_channel_temp_current")
        del inter.text_values  # o clique de botão não tem campos de modal
        await view.setstatus_callback(inter)
    else:
        inter = InteracaoEfemera(guild_id=SERVIDOR, guild=guilda, custom_id="status_voice_channel_temp")
        inter.text_values = {"status_voice_value": OUTRO_MODELO if modo == "texto" else ""}
        await view.modal_handler(inter)
    await assentar()
    return inter


def intacto(jogador: SimpleNamespace) -> bool:
    return (
        jogador.stage_title_event is True
        and jogador.stage_title_template == "modelo de antes {track.title}"
        and not jogador.update_stage_topic.await_count
        and not jogador.disable_voice_status.await_count
        and not jogador.process_save_queue.await_count
        and not jogador.set_command_log.call_count
    )


MODOS = ["texto", "vazio", "botao"]


class TestSessaoSoComOAutorNoCanal:
    @pytest.mark.parametrize("modo", MODOS)
    async def test_an_author_in_no_player_channel_is_refused_and_nothing_changes(self, modo: str) -> None:
        bots, jogadores = pool_com_autor(None)

        inter = await usar_sessao(bots[0], modo)

        assert "Não estou tocando música em um canal de voz/palco" in inter.texto
        assert "sucesso" not in inter.texto
        assert all(intacto(j) for j in jogadores)
        assert bots[0].gravacoes == []
