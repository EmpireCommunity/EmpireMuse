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
async def mundo(tmp_path, monkeypatch):
    mundo = montar_mundo(tmp_path)
    mundo.pool.i18n = get_i18n()
    monkeypatch.setenv("EMPIREMUSE_HOME", str(tmp_path / "runtime"))  # a fila salva nunca é a do disco real
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
    assert (
        sepulturas.stale(ALVO, leitura_antes) is True
    )  # a sepultura expirou, mas a leitura tem idade >= TTL: nunca confiável
    assert sepulturas.stale(ALVO, sepulturas.now()) is False  # leitura nova, sem sepultura: passa


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
@pytest.mark.parametrize("apagar_antes", [False, True])
async def test_last_tracks_sem_user_read_at_nunca_grava_falha_fechado(mundo, apagar_antes):
    await gravar_usuario(mundo, ALVO, last_tracks=[{"name": "Antiga", "url": "https://x/a"}])
    doc = await mundo.database.get_data(ALVO, db_name="users", collection="global", default_model=global_db_models)
    if apagar_antes:
        await apagar(mundo)
        mundo.relogio.avancar(1)

    await _registrar(_lifecycle(mundo), mundo, user_data=deepcopy(doc))  # hora da leitura desconhecida: descarta

    assert [t["name"] for t in await _ultimas(mundo)] == ([] if apagar_antes else ["Antiga"])


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

    async def dados_globais(user_id, *, db_name):
        return await mundo.database.get_data(
            user_id, db_name=db_name, collection="global", default_model=global_db_models
        )

    cog = _cog(mundo, update_global_data=grava, get_global_data=dados_globais)
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

    assert await cog._save_link(interacao, visao, None, lido_em) is True

    gravado = await mundo.database.find_doc(ALVO, db_name="users", collection="global")
    assert gravado["lastfm"] == {"username": "", "sessionkey": "", "scrobble": False}
    assert ALVO not in mundo.pool.lastfm_sessions


@pytest.mark.asyncio
async def test_vincular_grava_a_chave_e_o_usuario_novos_e_enche_o_cache(mundo):
    cog, visao, interacao = _fluxo(mundo, chave="NOVA", usuario="novo")
    doc = await mundo.database.get_data(ALVO, db_name="users", collection="global", default_model=global_db_models)

    assert await cog._save_link(interacao, visao, None, mundo.pool.erasure_tombstones.now()) is True

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

    assert await cog._save_link(interacao, visao, None, lido_em) is False

    assert await mundo.database.find_doc(ALVO, db_name="users", collection="global") is None  # nada foi regravado
    assert mundo.pool.lastfm_sessions == {}
    interacao.edit_original_message.assert_awaited_once()
    texto = interacao.edit_original_message.await_args.kwargs["content"]
    assert texto == (await get_i18n().for_guild(None))("legacy.lastfm.erased_while_linking") and texto


# ------------------------------------------------------------------------------------------------ fiação real do read_at (Crivo m4/m5)


@pytest.mark.asyncio
async def test_play_command_captura_read_at_antes_de_ler_o_documento_do_usuario(mundo):
    from empiremuse.cogs.music import Music
    from empiremuse.music_cog.play_state import PlayState

    inicio = mundo.relogio()

    async def le_devagar(*args, **kwargs):
        mundo.relogio.avancar(5)  # a leitura leva tempo: o read_at é o de ANTES
        return deepcopy(global_db_models["users"])

    cog = Music.__new__(Music)
    cog.bot = SimpleNamespace(
        pool=mundo.pool, get_global_data=le_devagar, get_global_command_named=lambda *a, **k: None
    )
    req = PlayState(
        inter=SimpleNamespace(author=SimpleNamespace(id=ALVO)),
        query="q",
        position=0,
        force_play="no",
        mix=False,
        manual_selection=False,
        options=False,
        server=None,
        manual_bot_choice="no",
    )
    req.bot, req.ephemeral, req.guild_data = cog.bot, False, {}

    await cog._play_user_context(req)

    assert req.user_read_at == inicio and mundo.relogio() == inicio + 5


@pytest.mark.asyncio
async def test_play_enqueue_repassa_o_read_at_do_play_ao_process_music(mundo):
    from empiremuse.cogs.music import Music
    from empiremuse.music_cog.play_state import PlayState

    cog = Music.__new__(Music)
    cog.process_music = AsyncMock()
    req = PlayState(
        inter=SimpleNamespace(),
        query="q",
        position=0,
        force_play="no",
        mix=False,
        manual_selection=False,
        options=False,
        server=None,
        manual_bot_choice="no",
    )
    req.guild_data = {"check_other_bots_in_vc": False}
    req.player = SimpleNamespace(is_connected=True)
    req.user_data, req.user_read_at, req.reg_query = {"x": 1}, 123.5, {"name": "n", "url": "u"}

    await cog._play_connect_and_process(req)

    assert cog.process_music.await_args.kwargs["user_read_at"] == 123.5


@pytest.mark.asyncio
async def test_play_resolve_integracao_em_formato_antigo_nao_regrava_o_documento_lido_antes_do_apagamento(mundo):
    from empiremuse.cogs.music import Music

    await gravar_usuario(mundo, ALVO, integration_links={"k": "https://x/perfil"}, token="T" * 50)
    leitura = mundo.pool.erasure_tombstones.now()
    doc = deepcopy(
        await mundo.database.get_data(ALVO, db_name="users", collection="global", default_model=global_db_models)
    )
    gravacoes = AsyncMock()
    cog = Music.__new__(Music)
    cog.bot = SimpleNamespace(pool=mundo.pool, update_global_data=gravacoes)
    inter = SimpleNamespace(author=SimpleNamespace(id=ALVO))

    def requisicao():
        return SimpleNamespace(
            bot=cog.bot, guild_data={}, inter=inter, is_pin=None, query="> itg: k", user_data=doc, user_read_at=leitura
        )

    await cog._play_resolve_prefix(requisicao())  # sem apagamento: grava o formato novo
    assert gravacoes.await_count == 1

    doc["integration_links"]["k"] = "https://x/perfil"  # volta ao formato antigo
    await apagar(mundo)
    await cog._play_resolve_prefix(requisicao())
    assert gravacoes.await_count == 1  # apagado depois da leitura: não regrava


@pytest.mark.asyncio
async def test_lastfm_comando_captura_o_read_at_no_inicio_e_aborta_se_apagaram_durante_o_fluxo(mundo, monkeypatch):
    from empiremuse.cogs.lastfm import LastFMView

    ler = AsyncMock(side_effect=lambda *a, **k: deepcopy(global_db_models["users"]))
    gravar_ = AsyncMock()
    fm = SimpleNamespace(api_key="AK", get_token=AsyncMock(return_value="TK"))
    cog = _cog(
        mundo,
        get_global_data=ler,
        update_global_data=gravar_,
        get_cog=lambda nome: None,
        get_color=lambda *a: 0,
        last_fm=fm,
        loop=SimpleNamespace(create_task=Mock()),
    )
    mundo.pool.i18n = get_i18n()
    interacao = SimpleNamespace(
        author=SimpleNamespace(id=ALVO),
        guild=None,
        permissions=SimpleNamespace(embed_links=True),
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_message=AsyncMock(),
    )

    async def usuario_desvincula_e_os_dados_sao_apagados(self):
        self.clear_session = True
        await apagar(mundo)
        mundo.relogio.avancar(1)  # o fim da exclusão fica antes do que vier depois: só um read_at do INÍCIO é velho

    monkeypatch.setattr(LastFMView, "wait", usuario_desvincula_e_os_dados_sao_apagados)

    await LastFmCog.lastfm.callback(cog, interacao)

    usos = gravar_.await_args_list
    assert len(usos) == 1  # só a limpeza da chave inválida do começo; _save_link não gravou
    texto = interacao.edit_original_message.await_args.kwargs["content"]
    assert texto == (await get_i18n().for_guild(None))("legacy.lastfm.erased_while_linking")


# ------------------------------------------------------------------------------------------------ v4: TTL, releitura, ganchos f e g, leitor entre mark e delete


@pytest.mark.asyncio
async def test_r2_fluxo_de_vinculo_mais_velho_que_o_ttl_aborta_mesmo_sem_nenhum_apagamento(mundo):
    await gravar_usuario(mundo, ALVO, fav_links={"a": "https://x/y"})
    cog, visao, interacao = _fluxo(mundo, chave="NOVA", usuario="novo")
    lido_em = mundo.pool.erasure_tombstones.now()
    mundo.relogio.avancar(ERASE_TOMBSTONE_S)  # os cliques renovaram o prazo do view por mais de 10 minutos

    assert await cog._save_link(interacao, visao, None, lido_em) is False

    gravado = await mundo.database.find_doc(ALVO, db_name="users", collection="global")
    assert gravado["lastfm"]["sessionkey"] == "" and gravado["fav_links"] == {"a": "https://x/y"}
    interacao.edit_original_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_r2_save_link_relê_e_grava_so_o_lastfm_favorito_novo_fica_e_o_usuario_vem_do_relido(mundo):
    await gravar_usuario(mundo, ALVO, lastfm_username="do_inicio")
    cog, visao, interacao = _fluxo(mundo, chave="NOVA", usuario="")  # a visão não trouxe usuário novo
    lido_em = mundo.pool.erasure_tombstones.now()
    # durante os 5 minutos do fluxo, o membro adicionou um favorito e o usuário do Last.fm mudou
    await gravar_usuario(mundo, ALVO, fav_links={"novo": "https://x/n"}, lastfm_username="do_relido")

    assert await cog._save_link(interacao, visao, None, lido_em) is True

    gravado = await mundo.database.find_doc(ALVO, db_name="users", collection="global")
    assert gravado["fav_links"] == {"novo": "https://x/n"}  # não some
    assert gravado["lastfm"] == {"username": "do_relido", "sessionkey": "NOVA", "scrobble": True}


@pytest.mark.asyncio
async def test_r2_save_link_apagado_durante_a_releitura_aborta_sem_gravar(mundo):
    await gravar_usuario(mundo, ALVO, lastfm_sessionkey=CHAVE)
    cog, visao, interacao = _fluxo(mundo, chave="NOVA", usuario="novo")
    lido_em = mundo.pool.erasure_tombstones.now()
    ler = cog.bot.get_global_data

    async def le_e_o_apagamento_acontece(user_id, *, db_name):
        doc = await ler(user_id, db_name=db_name)
        mundo.pool.erasure_tombstones.mark(ALVO)
        return doc

    cog.bot.get_global_data = le_e_o_apagamento_acontece

    assert await cog._save_link(interacao, visao, None, lido_em) is False

    interacao.edit_original_message.assert_awaited_once()
    assert mundo.pool.lastfm_sessions == {}


def _menu_de_favoritos(mundo, doc, leitura):
    from empiremuse.cogs.music import Music

    gravacoes = AsyncMock()
    cog = Music.__new__(Music)
    cog.bot = SimpleNamespace(pool=mundo.pool, update_global_data=gravacoes)
    requisicao = SimpleNamespace(
        bot=cog.bot,
        emoji=None,
        guild=None,
        guild_data={},
        inter=SimpleNamespace(author=SimpleNamespace(id=ALVO)),
        is_pin=None,
        query=">> [💠 Integrações 💠] <<",
        user_data=doc,
        user_read_at=leitura,
    )
    return cog, requisicao, gravacoes


@pytest.mark.asyncio
async def test_r2_menu_de_favoritos_nao_regrava_documento_lido_antes_do_apagamento_e_a_opcao_continua(
    mundo, monkeypatch
):
    from empiremuse.music_cog import play_menus

    monkeypatch.setattr(play_menus, "music_source_emoji_url", lambda url, bot: (None, "plataforma"))
    leitura = mundo.pool.erasure_tombstones.now()

    def doc_antigo():
        return {"integration_links": {"k": "https://x/perfil"}}

    cog, req, gravacoes = _menu_de_favoritos(mundo, doc_antigo(), leitura)
    await cog._play_fav_options(req)  # sem apagamento: migra o formato e grava
    assert gravacoes.await_count == 1

    await apagar(mundo)
    cog, req, gravacoes = _menu_de_favoritos(mundo, doc_antigo(), leitura)
    await cog._play_fav_options(req)

    gravacoes.assert_not_awaited()
    assert [opcao["url"] for opcao in req.fav_opts] == ["https://x/perfil"]  # a faixa ainda toca


@pytest.mark.asyncio
async def test_r2_leitor_entre_o_primeiro_mark_e_o_delete_e_descartado_last_tracks_nao_sobrevive(mundo, monkeypatch):
    await gravar_usuario(mundo, ALVO, last_tracks=[{"name": "Antiga", "url": "https://x/a"}], token="T" * 50)
    original = mundo.database.delete_data
    leituras: list = []

    async def delete_com_um_leitor_no_meio(id_, db_name, collection):
        if db_name == "users" and not leituras:
            mundo.relogio.avancar(1)  # depois do 1º mark e antes do fim da exclusão
            lido_em = mundo.pool.erasure_tombstones.now()
            doc = await mundo.database.get_data(
                id_, db_name="users", collection="global", default_model=global_db_models
            )
            leituras.append((lido_em, deepcopy(doc)))
            mundo.relogio.avancar(1)
        return await original(id_, db_name=db_name, collection=collection)

    monkeypatch.setattr(mundo.database, "delete_data", delete_com_um_leitor_no_meio)

    await apagar(mundo)
    lido_em, doc = leituras[0]

    assert mundo.pool.erasure_tombstones.stale(ALVO, lido_em) is True  # o 2º mark é o fim da exclusão
    await _registrar(_lifecycle(mundo), mundo, user_data=doc, user_read_at=lido_em)
    assert await _ultimas(mundo) == []  # a faixa velha não voltou


@pytest.mark.asyncio
async def test_r2_preenchedor_do_scrobble_lido_entre_o_mark_e_o_delete_nao_enche_o_cache_nem_scrobbla(
    mundo, monkeypatch
):
    await gravar_usuario(mundo, ALVO, lastfm_sessionkey=CHAVE, lastfm_scrobble=True)
    bot, fm = _bot_do_lastfm(mundo)
    cog = _cog(mundo, **bot)
    leu, liberar = asyncio.Event(), asyncio.Event()
    ler = bot["get_global_data"]

    async def le_e_espera(user_id, *, db_name):
        doc = await ler(user_id, db_name=db_name)
        leu.set()
        await liberar.wait()
        return doc

    cog.bot.get_global_data = le_e_espera
    original = mundo.database.delete_data
    preenchendo: list = []

    async def delete_com_o_preenchimento_no_meio(id_, db_name, collection):
        if db_name == "users" and not preenchendo:
            mundo.relogio.avancar(1)  # o preenchedor lê o documento VELHO depois do 1º mark
            preenchendo.append(asyncio.create_task(_tocar(cog, ALVO)))
            await leu.wait()
            mundo.relogio.avancar(1)
        return await original(id_, db_name=db_name, collection=collection)

    monkeypatch.setattr(mundo.database, "delete_data", delete_com_o_preenchimento_no_meio)

    await apagar(mundo)
    liberar.set()
    await preenchendo[0]

    assert mundo.pool.lastfm_sessions == {}
    fm.update_nowplaying.assert_not_called()


def _comando_do_lastfm(mundo, monkeypatch, *, doc, get_global_data=None, user_info=None):
    from empiremuse.cogs.lastfm import LastFMView

    ler = get_global_data or AsyncMock(side_effect=lambda *a, **k: deepcopy(doc))
    gravar_ = AsyncMock()
    fm = SimpleNamespace(api_key="AK", get_token=AsyncMock(return_value="TK"), user_info=user_info)
    cog = _cog(
        mundo,
        get_global_data=ler,
        update_global_data=gravar_,
        get_cog=lambda nome: None,
        get_color=lambda *a: 0,
        last_fm=fm,
        loop=SimpleNamespace(create_task=Mock()),
    )
    interacao = SimpleNamespace(
        author=SimpleNamespace(id=ALVO),
        guild=None,
        permissions=SimpleNamespace(embed_links=True),
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_message=AsyncMock(),
    )

    async def sem_espera(self):
        return None

    monkeypatch.setattr(LastFMView, "wait", sem_espera)
    return cog, interacao, gravar_


@pytest.mark.asyncio
async def test_lastfm_comando_apagado_durante_a_leitura_o_read_at_e_o_de_antes_e_o_blank_out_nao_grava(
    mundo, monkeypatch
):
    doc = deepcopy(global_db_models["users"])  # sem chave: o caminho que limpa usuário e chave e grava
    leituras = []

    async def le_e_o_apagamento_acontece(user_id, *, db_name):
        leituras.append(True)
        await apagar(mundo)
        mundo.relogio.avancar(1)
        return deepcopy(doc)

    cog, interacao, gravar_ = _comando_do_lastfm(
        mundo, monkeypatch, doc=doc, get_global_data=le_e_o_apagamento_acontece
    )

    await LastFmCog.lastfm.callback(cog, interacao)

    assert leituras and gravar_.await_count == 0  # read_at foi capturado ANTES da leitura: o documento lido é velho


@pytest.mark.asyncio
async def test_lastfm_comando_apagado_durante_o_user_info_o_blank_out_do_codigo_9_nao_grava(mundo, monkeypatch):
    from empiremuse.utils.music.lastfm_tools import LastFmException

    doc = documento_com_chave = deepcopy(global_db_models["users"])
    documento_com_chave["lastfm"]["sessionkey"] = CHAVE

    async def user_info_que_demora(chave):
        await apagar(mundo)  # os dados somem enquanto o Last.fm responde
        mundo.relogio.avancar(1)
        raise LastFmException({"error": 9, "message": "sessão inválida"})

    cog, interacao, gravar_ = _comando_do_lastfm(mundo, monkeypatch, doc=doc, user_info=user_info_que_demora)

    await LastFmCog.lastfm.callback(cog, interacao)

    assert gravar_.await_count == 0  # sem o if stale o documento velho voltaria ao banco


# ------------------------------------------------------------------------------------------------ revisão T-481c


@pytest.mark.asyncio
async def test_r2_save_link_apagado_durante_a_gravacao_nao_enche_o_cache_do_lastfm(mundo):
    await gravar_usuario(mundo, ALVO, lastfm_sessionkey=CHAVE)
    cog, visao, interacao = _fluxo(mundo, chave="NOVA", usuario="novo")
    lido_em = mundo.pool.erasure_tombstones.now()
    grava = cog.bot.update_global_data

    async def grava_e_o_apagamento_acontece(user_id, *, data, db_name):
        await grava(user_id, data=data, db_name=db_name)
        mundo.pool.erasure_tombstones.mark(ALVO)

    cog.bot.update_global_data = grava_e_o_apagamento_acontece

    assert await cog._save_link(interacao, visao, None, lido_em) is False

    assert mundo.pool.lastfm_sessions == {}
    interacao.edit_original_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_r2_modal_de_integracao_apagado_durante_a_consulta_externa_nao_grava_e_avisa(mundo, monkeypatch):
    from empiremuse.utils.music.interactions import fav_modals

    await gravar_usuario(mundo, ALVO, token="T" * 50)
    gravacoes = AsyncMock()
    modal = fav_modals.FavModalAdd.__new__(fav_modals.FavModalAdd)
    traduzir = await get_i18n().for_guild(None)
    modal.view = SimpleNamespace(
        tr=traduzir,
        bot=SimpleNamespace(
            pool=mundo.pool,
            get_global_data=lambda *a, **k: mundo.database.get_data(
                ALVO, db_name="users", collection="global", default_model=global_db_models
            ),
            update_global_data=gravacoes,
            get_cog=lambda nome: SimpleNamespace(
                fav_import_export_cd=SimpleNamespace(
                    get_bucket=lambda inter: SimpleNamespace(update_rate_limit=lambda: None)
                )
            ),
        ),
    )

    async def consulta_externa_demorada(inter, url, source):
        await apagar(mundo)  # os dados somem enquanto o serviço externo responde
        mundo.relogio.avancar(1)
        return {"title": "Canal", "url": "https://x/canal", "avatar": None}

    monkeypatch.setattr(modal, "_lookup_profile", consulta_externa_demorada, raising=False)
    monkeypatch.setattr(fav_modals, "_profile_source", lambda url: "youtube")
    monkeypatch.setattr(fav_modals, "quota_for", AsyncMock(return_value=10))
    interacao = SimpleNamespace(
        author=SimpleNamespace(id=ALVO),
        response=SimpleNamespace(defer=AsyncMock()),
        text_values={"user_integration_url": "https://www.youtube.com/@canal"},
        edit_original_message=AsyncMock(),
    )

    assert await modal._add_integration(interacao) is False

    gravacoes.assert_not_awaited()
    assert interacao.edit_original_message.await_args.kwargs["content"] == traduzir("fav.erased_while_saving")


@pytest.mark.asyncio
@pytest.mark.regression
async def test_save_link_espera_o_lock_do_apagamento_e_nao_recria_o_documento(mundo):
    """Apagamento em andamento (segurando o lock do usuário): o fluxo do /lastfm espera, vê a sepultura e aborta, sem travar."""
    await gravar_usuario(mundo, ALVO, lastfm_username="fulano", lastfm_sessionkey=CHAVE)
    cog, visao, interacao = _fluxo(mundo, chave="NOVA", usuario="novo")
    lido_em = mundo.pool.erasure_tombstones.now()

    async with mundo.pool.user_locks.lock(ALVO):  # o que ``erase_user`` faz do início ao fim
        tarefa = asyncio.ensure_future(cog._save_link(interacao, visao, None, lido_em))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not tarefa.done()  # esperando o lock: nada foi relido nem gravado
        mundo.pool.erasure_tombstones.mark(ALVO)
        await mundo.database.delete_data(ALVO, db_name="users", collection="global")
        mundo.pool.erasure_tombstones.mark(ALVO)

    assert await asyncio.wait_for(tarefa, timeout=2) is False  # sem deadlock: o lock foi solto e o fluxo seguiu
    assert await mundo.database.find_doc(ALVO, db_name="users", collection="global") is None  # não recriou
    assert mundo.pool.lastfm_sessions == {}
    interacao.edit_original_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_save_link_sem_apagamento_nao_trava_no_proprio_lock(mundo):
    cog, visao, interacao = _fluxo(mundo, chave="NOVA", usuario="novo")
    lock = mundo.pool.user_locks.lock(
        ALVO
    )  # a referência mantém o mesmo objeto vivo (o dicionário só guarda referência fraca)

    assert (
        await asyncio.wait_for(cog._save_link(interacao, visao, None, mundo.pool.erasure_tombstones.now()), 2) is True
    )
    assert mundo.pool.user_locks.lock(ALVO) is lock and not lock.locked()  # era o mesmo lock e soltou ao sair
