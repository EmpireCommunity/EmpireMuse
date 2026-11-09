"""Apagar meus dados (E1), contrato §3.3: a sepultura (T5) e os cinco ganchos que descartam o que foi lido antes do apagamento (R2),
mais o "Desvincular" que também zera o usuário do Last.fm (T14, P3) e o scrobble que não sai depois de apagar (T4).

Os ganchos são os do código real (``LastFmCog``, ``_pc_lastfm``, ``PlayerLifecycleMixin``) com bot, player e Last.fm falsos e o serviço real
sobre bancos TinyMongo em ``tmp_path``. O relógio é o falso do mundo de teste: ``avancar`` separa a leitura que vem depois do apagamento.
"""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from empiremuse.cogs.lastfm import LastFmCog, clear_lastfm_link
from empiremuse.i18n.service import get_i18n
from empiremuse.music_cog.controller_buttons import _pc_lastfm
from empiremuse.music_cog.player_lifecycle import PlayerLifecycleMixin
from empiremuse.rules.erasure import ERASE_TOMBSTONE_S
from empiremuse.utils.db import global_db_models
from empiremuse.utils.erasure import ErasureTombstones
from tests.support.erasure_world import (
    ALVO,
    PREFIXO_DO_ALVO,
    RelogioFalso,
    gravar_usuario,
    montar_mundo,
)

pytestmark = pytest.mark.critical

CHAVE = "SK-SEGREDO"


@pytest_asyncio.fixture
async def mundo(tmp_path):
    mundo = montar_mundo(tmp_path)
    mundo.pool.i18n = get_i18n()
    yield mundo
    for tarefa in list(mundo.service._tasks):
        tarefa.cancel()
    await asyncio.gather(*mundo.service._tasks, return_exceptions=True)


async def apagar(mundo, user_id: int = ALVO):
    return await mundo.service.erase_user(user_id, source="slash")


# ------------------------------------------------------------------------------------------------ T5 sepultura


def test_t5_stale_so_para_leitura_anterior_ao_apagamento_vale_pelo_ttl_e_nao_afeta_outro_usuario():
    relogio = RelogioFalso(100.0)
    sepulturas = ErasureTombstones(clock=relogio)
    leitura_antes = sepulturas.now()
    relogio.avancar(5)
    sepulturas.mark(ALVO)
    relogio.avancar(1)
    leitura_depois = sepulturas.now()

    assert sepulturas.stale(ALVO, leitura_antes) is True
    assert sepulturas.stale(ALVO, leitura_depois) is False  # uso novo, com leitura depois do apagamento
    assert sepulturas.stale(PREFIXO_DO_ALVO, leitura_antes) is False  # outro usuário

    relogio.avancar(ERASE_TOMBSTONE_S - 2)
    assert sepulturas.stale(ALVO, leitura_antes) is True  # ainda dentro do prazo
    relogio.avancar(1)
    assert sepulturas.stale(ALVO, leitura_antes) is False  # expirou


def test_t5_mark_poda_as_sepulturas_expiradas():
    relogio = RelogioFalso()
    sepulturas = ErasureTombstones(ttl_s=10, clock=relogio)
    sepulturas.mark(1)
    relogio.avancar(11)

    sepulturas.mark(2)

    assert list(sepulturas._erased_at) == [2]


# ------------------------------------------------------------------------------------------------ gancho (b): save_scrobble


def _faixa():
    return SimpleNamespace(duration=200000)


def _cog(mundo, **bot):
    cog = LastFmCog.__new__(LastFmCog)
    cog.bot = SimpleNamespace(pool=mundo.pool, **bot)
    return cog


async def _fila(mundo):
    doc = await mundo.local.find_doc(ALVO, db_name="users", collection="scrobbles")
    return doc["tracks"] if doc else []


@pytest.mark.asyncio
async def test_r2_save_scrobble_com_leitura_anterior_ao_apagamento_descarta_sem_gravar(mundo, monkeypatch):
    cog = _cog(mundo)
    ler = mundo.local.get_data

    async def le_e_o_apagamento_acontece(*args, **kwargs):
        doc = await ler(*args, **kwargs)
        mundo.pool.erasure_tombstones.mark(ALVO)
        return doc

    monkeypatch.setattr(mundo.local, "get_data", le_e_o_apagamento_acontece)

    await cog.save_scrobble("faixa", _faixa(), [SimpleNamespace(id=ALVO)])

    assert await _fila(mundo) == []  # nada foi regravado

    monkeypatch.setattr(mundo.local, "get_data", ler)
    mundo.relogio.avancar(1)
    await cog.save_scrobble("faixa nova", _faixa(), [SimpleNamespace(id=ALVO)])  # uso novo, lido depois: grava
    assert [t["query"] for t in await _fila(mundo)] == ["faixa nova"]


# ------------------------------------------------------------------------------------------------ gancho (c): startscrooble


def _player():
    return SimpleNamespace(guild=SimpleNamespace(me=SimpleNamespace(voice=True)), lastfm_users={})


def _musica():
    return SimpleNamespace(
        is_stream=False,
        duration=200000,
        info={"sourceName": "spotify"},
        album_name="Album",
        author="Artista",
        single_title="Musica",
        url="https://x/y",
        autoplay=False,
        requester=ALVO,
    )


def _bot_do_lastfm(mundo, **extra):
    async def dados_globais(user_id, *, db_name):
        return await mundo.database.get_data(
            user_id, db_name=db_name, collection="global", default_model=global_db_models
        )

    fm = SimpleNamespace(update_nowplaying=AsyncMock(), track_scrobble=AsyncMock())
    return dict(get_global_data=dados_globais, last_fm=fm, **extra), fm


async def _tocar(cog, *membros):
    await cog.startscrooble(
        _player(), _musica(), update_np=True, users=[SimpleNamespace(id=m, bot=False) for m in membros]
    )


@pytest.mark.asyncio
async def test_r2_preenchimento_em_andamento_nao_religa_o_scrobble_depois_do_apagamento(mundo):
    await gravar_usuario(mundo, ALVO, lastfm_sessionkey=CHAVE, lastfm_scrobble=True)
    bot, fm = _bot_do_lastfm(mundo)
    cog = _cog(mundo, **bot)
    leu, liberar = asyncio.Event(), asyncio.Event()
    ler = bot["get_global_data"]

    async def le_e_espera(user_id, *, db_name):
        doc = await ler(user_id, db_name=db_name)
        leu.set()
        await liberar.wait()  # o preenchimento já leu o documento antigo e está parado
        return doc

    cog.bot.get_global_data = le_e_espera
    preenchendo = asyncio.create_task(_tocar(cog, ALVO))
    await leu.wait()

    await apagar(mundo)
    liberar.set()
    await preenchendo

    assert mundo.pool.lastfm_sessions == {}
    fm.update_nowplaying.assert_not_called()
    fm.track_scrobble.assert_not_called()


@pytest.mark.asyncio
async def test_r2_apagado_durante_a_busca_de_metadados_nenhum_scrobble_sai_com_a_chave_antiga(mundo):
    outro = PREFIXO_DO_ALVO
    for membro in (ALVO, outro):
        await gravar_usuario(mundo, membro, lastfm_sessionkey=f"{CHAVE}-{membro}", lastfm_scrobble=True)
    bot, fm = _bot_do_lastfm(mundo)
    cog = _cog(mundo, **bot)

    async def envio_do_primeiro(**kwargs):  # enquanto o 1º membro é atendido, o 2º apaga os dados
        await apagar(mundo, outro)

    fm.update_nowplaying.side_effect = envio_do_primeiro

    await _tocar(cog, ALVO, outro)

    assert [c.kwargs["session_key"] for c in fm.update_nowplaying.await_args_list] == [f"{CHAVE}-{ALVO}"]


@pytest.mark.asyncio
async def test_t4_depois_de_apagar_nenhum_scrobble_sai_e_o_cache_esta_vazio(mundo):
    await gravar_usuario(mundo, ALVO, lastfm_sessionkey=CHAVE, lastfm_scrobble=True)
    mundo.pool.lastfm_sessions[ALVO] = {"scrobble": True, "sessionkey": CHAVE}
    mundo.pool.user_prefix_cache[ALVO] = "!"
    bot, fm = _bot_do_lastfm(mundo)
    cog = _cog(mundo, **bot)

    await apagar(mundo)
    assert ALVO not in mundo.pool.lastfm_sessions and ALVO not in mundo.pool.user_prefix_cache

    mundo.relogio.avancar(1)
    await _tocar(cog, ALVO)  # o próximo uso lê o padrão

    assert mundo.pool.lastfm_sessions[ALVO]["sessionkey"] == ""
    fm.update_nowplaying.assert_not_called()
    fm.track_scrobble.assert_not_called()
    padrao = await mundo.database.get_data(ALVO, db_name="users", collection="global", default_model=global_db_models)
    assert padrao["lastfm"] == global_db_models["users"]["lastfm"]


# ------------------------------------------------------------------------------------------------ gancho (d): botão do player


def _requisicao():
    interacao = SimpleNamespace(
        author=SimpleNamespace(id=ALVO),
        guild=None,
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_message=AsyncMock(),
    )
    return SimpleNamespace(cmd=None, interaction=interacao), interacao


def _contexto_do_botao(mundo):
    async def dados_globais(user_id, *, db_name):
        return await mundo.database.get_data(
            user_id, db_name=db_name, collection="global", default_model=global_db_models
        )

    async def grava(user_id, data, *, db_name):
        return await mundo.database.update_data(
            user_id, data, db_name=db_name, collection="global", default_model=global_db_models
        )

    bot = SimpleNamespace(
        pool=mundo.pool,
        get_global_data=dados_globais,
        update_global_data=grava,
        get_global_command_named=lambda *args, **kwargs: None,
        get_color=lambda: 0,
    )
    return SimpleNamespace(bot=bot), dados_globais


@pytest.mark.asyncio
async def test_r2_botao_do_scrobble_com_leitura_anterior_ao_apagamento_nao_grava_nem_enche_o_cache(mundo):
    await gravar_usuario(mundo, ALVO, lastfm_username="fulano", lastfm_sessionkey=CHAVE, lastfm_scrobble=False)
    contexto, ler = _contexto_do_botao(mundo)

    async def le_e_o_apagamento_acontece(user_id, *, db_name):
        doc = await ler(user_id, db_name=db_name)
        await apagar(mundo)
        return doc

    contexto.bot.get_global_data = le_e_o_apagamento_acontece
    requisicao, interacao = _requisicao()

    sem_conta = await _pc_lastfm(contexto, requisicao)

    assert sem_conta is True  # responde como quem não tem conta vinculada
    assert mundo.pool.lastfm_sessions == {}
    assert await mundo.database.find_doc(ALVO, db_name="users", collection="global") is None
    interacao.edit_original_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_botao_do_scrobble_sem_apagamento_liga_e_grava_normalmente(mundo):
    await gravar_usuario(mundo, ALVO, lastfm_username="fulano", lastfm_sessionkey=CHAVE, lastfm_scrobble=False)
    contexto, _ = _contexto_do_botao(mundo)
    requisicao, _interacao = _requisicao()

    sem_conta = await _pc_lastfm(contexto, requisicao)

    assert sem_conta is False and mundo.pool.lastfm_sessions[ALVO]["scrobble"] is True
    gravado = await mundo.database.find_doc(ALVO, db_name="users", collection="global")
    assert gravado["lastfm"]["scrobble"] is True


# ------------------------------------------------------------------------------------------------ gancho (a): last_tracks


def _lifecycle(mundo, **bot):
    mixin = PlayerLifecycleMixin()
    mixin.bot = SimpleNamespace(pool=mundo.pool, **bot)
    return mixin


async def _registrar(mixin, mundo, *, user_data=None, user_read_at=None):
    async def dados_globais(user_id, *, db_name):
        return await mundo.database.get_data(
            user_id, db_name=db_name, collection="global", default_model=global_db_models
        )

    async def grava(user_id, data, *, db_name):
        return await mundo.database.update_data(
            user_id, data, db_name=db_name, collection="global", default_model=global_db_models
        )

    mixin.bot.get_global_data, mixin.bot.update_global_data = dados_globais, grava
    player = SimpleNamespace(current=object(), set_command_log=Mock(), update=False)
    inter = SimpleNamespace(author=SimpleNamespace(id=ALVO))
    await mixin.process_music(
        inter=inter,
        player=player,
        user_data=user_data,
        user_read_at=user_read_at,
        reg_query={"name": "Faixa", "url": "https://x/f"},
    )


async def _ultimas(mundo):
    doc = await mundo.database.find_doc(ALVO, db_name="users", collection="global")
    return doc["last_tracks"] if doc else []


@pytest.mark.asyncio
@pytest.mark.parametrize("minutos_depois", [2, 6])
async def test_r2_last_tracks_lido_antes_e_gravado_aos_2_e_6_minutos_nao_restaura_a_faixa(mundo, minutos_depois):
    await gravar_usuario(mundo, ALVO, last_tracks=[{"name": "Antiga", "url": "https://x/a"}])
    lido_em = mundo.pool.erasure_tombstones.now()
    user_data = await mundo.database.get_data(
        ALVO, db_name="users", collection="global", default_model=global_db_models
    )
    user_data = deepcopy(user_data)
    await apagar(mundo)
    mundo.relogio.avancar(minutos_depois * 60)

    await _registrar(_lifecycle(mundo), mundo, user_data=user_data, user_read_at=lido_em)

    assert await _ultimas(mundo) == []


@pytest.mark.asyncio
async def test_last_tracks_sem_user_read_at_conta_a_partir_da_entrada_em_process_music(mundo):
    await gravar_usuario(mundo, ALVO, last_tracks=[{"name": "Antiga", "url": "https://x/a"}])
    doc = await mundo.database.get_data(ALVO, db_name="users", collection="global", default_model=global_db_models)
    await apagar(mundo)
    mundo.relogio.avancar(1)

    await _registrar(_lifecycle(mundo), mundo, user_data=deepcopy(doc))  # entrou depois do apagamento: passa

    assert [t["name"] for t in await _ultimas(mundo)] == ["Antiga", "Faixa"]


@pytest.mark.asyncio
async def test_last_tracks_leitura_propria_depois_do_apagamento_grava_so_o_uso_novo(mundo):
    await gravar_usuario(mundo, ALVO, last_tracks=[{"name": "Antiga", "url": "https://x/a"}])
    await apagar(mundo)
    mundo.relogio.avancar(1)

    await _registrar(_lifecycle(mundo), mundo)  # sem user_data: lê agora

    assert [t["name"] for t in await _ultimas(mundo)] == ["Faixa"]


# ------------------------------------------------------------------------------------------------ gancho (e) e T14: vínculo do Last.fm


def _fluxo(mundo, *, desvincular=False, chave="", usuario=""):
    async def grava(user_id, *, data, db_name):
        return await mundo.database.update_data(
            user_id, data, db_name=db_name, collection="global", default_model=global_db_models
        )

    cog = _cog(mundo, update_global_data=grava)
    visao = SimpleNamespace(
        clear_session=desvincular, session_key=chave, scrobble_enabled=True, username=usuario, interaction=None
    )
    interacao = SimpleNamespace(author=SimpleNamespace(id=ALVO), guild=None, edit_original_message=AsyncMock())
    return cog, visao, interacao


def test_clear_lastfm_link_zera_usuario_chave_e_liga_desliga():
    doc = {"lastfm": {"username": "fulano", "sessionkey": CHAVE, "scrobble": True}, "token": "t"}

    assert clear_lastfm_link(doc) == {"username": "", "sessionkey": "", "scrobble": False}
    assert doc["token"] == "t" and clear_lastfm_link({}) == {"username": "", "sessionkey": "", "scrobble": False}


@pytest.mark.asyncio
@pytest.mark.regression
async def test_t14_desvincular_zera_usuario_e_chave_no_banco_e_tira_o_cache(mundo):
    await gravar_usuario(mundo, ALVO, lastfm_username="fulano", lastfm_sessionkey=CHAVE, lastfm_scrobble=True)
    mundo.pool.lastfm_sessions[ALVO] = {"scrobble": True, "sessionkey": CHAVE}
    cog, visao, interacao = _fluxo(mundo, desvincular=True)
    lido_em = mundo.pool.erasure_tombstones.now()
    doc = await mundo.database.get_data(ALVO, db_name="users", collection="global", default_model=global_db_models)

    assert await cog._save_link(interacao, visao, None, doc, lido_em) is True

    gravado = await mundo.database.find_doc(ALVO, db_name="users", collection="global")
    assert gravado["lastfm"] == {"username": "", "sessionkey": "", "scrobble": False}
    assert ALVO not in mundo.pool.lastfm_sessions


@pytest.mark.asyncio
async def test_vincular_grava_a_chave_e_o_usuario_novos_e_enche_o_cache(mundo):
    cog, visao, interacao = _fluxo(mundo, chave="NOVA", usuario="novo")
    doc = await mundo.database.get_data(ALVO, db_name="users", collection="global", default_model=global_db_models)

    assert await cog._save_link(interacao, visao, None, doc, mundo.pool.erasure_tombstones.now()) is True

    gravado = await mundo.database.find_doc(ALVO, db_name="users", collection="global")
    assert gravado["lastfm"] == {"username": "novo", "sessionkey": "NOVA", "scrobble": True}
    assert mundo.pool.lastfm_sessions[ALVO]["sessionkey"] == "NOVA"


@pytest.mark.asyncio
@pytest.mark.parametrize("desvincular", [False, True])
async def test_r2_fluxo_de_vinculo_iniciado_antes_e_concluido_depois_do_apagamento_aborta(mundo, desvincular):
    await gravar_usuario(mundo, ALVO, lastfm_username="fulano", lastfm_sessionkey=CHAVE)
    cog, visao, interacao = _fluxo(mundo, desvincular=desvincular, chave="" if desvincular else "NOVA", usuario="novo")
    lido_em = mundo.pool.erasure_tombstones.now()
    doc = deepcopy(
        await mundo.database.get_data(ALVO, db_name="users", collection="global", default_model=global_db_models)
    )
    await apagar(mundo)

    assert await cog._save_link(interacao, visao, None, doc, lido_em) is False

    assert await mundo.database.find_doc(ALVO, db_name="users", collection="global") is None  # nada foi regravado
    assert mundo.pool.lastfm_sessions == {}
    interacao.edit_original_message.assert_awaited_once()
    texto = interacao.edit_original_message.await_args.kwargs["content"]
    assert texto == (await get_i18n().for_guild(None))("legacy.lastfm.erased_while_linking") and texto
