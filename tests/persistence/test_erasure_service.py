"""Apagar meus dados (E1), contrato §3 e §10: ``ErasureService`` com bancos TinyMongo reais, relógio e conexão RPC falsos.

T1, T2, T6, T7, T8, T13, T15; R2 (ressurreição), R3 (lock), R6 (limites), R7 (alvo errado).
"""

import asyncio
import builtins
import logging
from copy import deepcopy

import pytest
import pytest_asyncio

from empiremuse.panel.services.service import SettingsService
from empiremuse.panel.sessions import PanelUser, SessionStore
from empiremuse.rules.erasure import ERASE_CONCURRENCY, ERASE_RECHECK_S, ErasureStatus
from empiremuse.utils.erasure import ErasureBusy, ErasureRateLimited
from empiremuse.utils.user_locks import UserLocks
from tests.panel.helpers import https_config, load_settings
from tests.support.erasure_world import (
    ALVO,
    BOT,
    PREFIXO_DO_ALVO,
    SERVIDOR,
    SERVIDOR_2,
    SUFIXO_DO_ALVO,
    existe,
    gravar,
    gravar_scrobbles,
    gravar_usuario,
    ids_da_colecao,
    montar_mundo,
)

pytestmark = pytest.mark.critical

LOGGER = "empiremuse.utils.erasure"
SEGREDO_PREFIXO, SEGREDO_CHAVE, SEGREDO_TOKEN = "SEGREDO-PREFIXO", "SK-SEGREDO", "T" * 50


@pytest_asyncio.fixture
async def mundo(tmp_path, monkeypatch):
    mundo = montar_mundo(tmp_path)
    monkeypatch.setenv("EMPIREMUSE_HOME", str(tmp_path / "runtime"))  # a fila salva nunca é a do disco real
    yield mundo
    for tarefa in list(mundo.service._tasks):
        tarefa.cancel()
    await asyncio.gather(*mundo.service._tasks, return_exceptions=True)


async def semear_membro(mundo, user_id: int) -> None:
    await gravar_usuario(
        mundo,
        user_id,
        custom_prefix=SEGREDO_PREFIXO,
        fav_links={"a": "https://x/y"},
        token=SEGREDO_TOKEN,
        last_tracks=[{"name": "n", "url": "https://x/z"}],
        lastfm_username="fulano",
        lastfm_sessionkey=SEGREDO_CHAVE,
        lastfm_scrobble=True,
    )
    await gravar_scrobbles(mundo, user_id, "faixa que nao casou")
    await gravar(mundo, "global", "radio_user", user_id, {"rev": 1, "filters": ["n1"]})


async def apagar(mundo, user_id: int = ALVO, **kwargs):
    return await mundo.service.erase_user(user_id, source=kwargs.pop("source", "slash"), **kwargs)


# ------------------------------------------------------------------------------------------------ T1, T2, R7


@pytest.mark.asyncio
async def test_t1_apaga_so_o_alvo_e_nenhum_vizinho_de_id(mundo):
    vizinhos = (PREFIXO_DO_ALVO, SUFIXO_DO_ALVO)
    for membro in (ALVO, *vizinhos):
        await semear_membro(mundo, membro)
    favoritas = {
        f"{SERVIDOR}:{ALVO}",
        f"{SERVIDOR_2}:{ALVO}",
        f"{SERVIDOR}:{PREFIXO_DO_ALVO}",
        f"{SERVIDOR}:{SUFIXO_DO_ALVO}",
        f"{ALVO}:{SERVIDOR}",  # o servidor tem o mesmo número que o alvo
        "lixo",
    }
    for chave in favoritas:
        await gravar(mundo, "global", "radio_custom_favorites", chave, {"rev": 1, "stations": []})
    await gravar(mundo, "scrobbles", "users", ALVO, {"tracks": [1]})  # no banco principal: não é a fila (§2 linha 2)

    relatorio = await apagar(mundo)

    assert relatorio.status is ErasureStatus.OK and relatorio.failed == ()
    assert {k: relatorio.counts[k] for k in ("users", "scrobbles", "radio_user", "radio_custom_favorites")} == {
        "users": 1,
        "scrobbles": 1,
        "radio_user": 1,
        "radio_custom_favorites": 2,
    }
    assert not await existe(mundo, "global", "users", ALVO)
    assert not await existe(mundo, "scrobbles", "users", ALVO, local=True)
    assert not await existe(mundo, "global", "radio_user", ALVO)
    assert await ids_da_colecao(mundo, "global", "radio_custom_favorites") == favoritas - {
        f"{SERVIDOR}:{ALVO}",
        f"{SERVIDOR_2}:{ALVO}",
    }
    assert await existe(mundo, "scrobbles", "users", ALVO)  # o banco principal não foi tocado
    for membro in vizinhos:
        assert await existe(mundo, "global", "users", membro)
        assert await existe(mundo, "scrobbles", "users", membro, local=True)
        assert await existe(mundo, "global", "radio_user", membro)


@pytest.mark.asyncio
async def test_t2_segunda_execucao_devolve_zeros_e_ok_e_colecao_inexistente_nao_da_erro(mundo):
    await gravar_usuario(mundo, ALVO, token=SEGREDO_TOKEN)  # sem radio_user: a coleção nem existe

    primeira = await apagar(mundo)
    segunda = await apagar(mundo)

    assert primeira.status is segunda.status is ErasureStatus.OK
    assert primeira.counts["users"] == 1 and primeira.counts["radio_user"] == 0
    assert all(valor == 0 for chave, valor in segunda.counts.items() if chave != "memory")  # o RPC falso sempre "fecha"


# ------------------------------------------------------------------------------------------------ T6, T7


@pytest.mark.asyncio
@pytest.mark.regression
async def test_t6_escritor_atrasado_que_regrava_durante_a_execucao_e_apagado_na_passada_1(mundo, monkeypatch):
    doc = await gravar_usuario(mundo, ALVO, token=SEGREDO_TOKEN)
    original = mundo.database.delete_data
    ressuscitou = []

    async def apaga_e_um_escritor_atrasado_regrava(id_, db_name, collection):
        resultado = await original(id_, db_name=db_name, collection=collection)
        if db_name == "users" and not ressuscitou:
            ressuscitou.append(True)
            await mundo.database.update_data(id_, deepcopy(doc), db_name="users", collection="global")
        return resultado

    monkeypatch.setattr(mundo.database, "delete_data", apaga_e_um_escritor_atrasado_regrava)

    relatorio = await apagar(mundo)

    assert relatorio.status is ErasureStatus.OK and ressuscitou
    assert not await existe(mundo, "global", "users", ALVO)


@pytest.mark.asyncio
async def test_t7_falha_de_um_armazenamento_vira_partial_e_nenhum_texto_de_excecao_aparece(mundo, monkeypatch, caplog):
    await semear_membro(mundo, ALVO)
    original = mundo.database.delete_data

    async def radio_user_falha(id_, db_name, collection):
        if db_name == "radio_user":
            raise RuntimeError("detalhe-interno-da-excecao")
        return await original(id_, db_name=db_name, collection=collection)

    monkeypatch.setattr(mundo.database, "delete_data", radio_user_falha)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        relatorio = await apagar(mundo)

    assert relatorio.status is ErasureStatus.PARTIAL and relatorio.failed == ("radio_user",)
    assert not await existe(mundo, "global", "users", ALVO)  # os demais foram apagados
    assert await existe(mundo, "global", "radio_user", ALVO)
    assert "detalhe-interno-da-excecao" not in repr(relatorio) + caplog.text


# ------------------------------------------------------------------------------------------------ T8


@pytest.mark.asyncio
async def test_t8_duas_chamadas_do_mesmo_usuario_uma_apaga_e_a_outra_devolve_zeros(mundo):
    await semear_membro(mundo, ALVO)

    primeira, segunda = await asyncio.gather(apagar(mundo), apagar(mundo))

    assert primeira.status is segunda.status is ErasureStatus.OK
    assert [primeira.counts["users"], segunda.counts["users"]] == [1, 0]


async def _bloquear_leitura_do_usuario(mundo, monkeypatch, usuarios: set[int]):
    """O passo 1 de quem está em ``usuarios`` fica parado até ``liberar.set()``; ``entraram`` diz quem já chegou."""
    liberar, entraram = asyncio.Event(), []
    original = mundo.database.find_doc

    async def parado(id_, *, db_name, collection):
        if db_name == "users" and collection == "global" and int(id_) in usuarios and int(id_) not in entraram:
            entraram.append(int(id_))
            await liberar.wait()
        return await original(id_, db_name=db_name, collection=collection)

    monkeypatch.setattr(mundo.database, "find_doc", parado)
    return liberar, entraram


async def _esperar(condicao) -> None:
    for _ in range(200):
        if condicao():
            return
        await asyncio.sleep(0)
    raise AssertionError("a condição não chegou")


@pytest.mark.asyncio
async def test_t8_so_duas_execucoes_ao_mesmo_tempo_a_terceira_recebe_busy_e_nada_e_apagado(mundo, monkeypatch):
    primeiro, segundo, terceiro = ALVO, PREFIXO_DO_ALVO, SUFIXO_DO_ALVO
    for membro in (primeiro, segundo, terceiro):
        await gravar_usuario(mundo, membro, token=SEGREDO_TOKEN)
    liberar, entraram = await _bloquear_leitura_do_usuario(mundo, monkeypatch, {primeiro, segundo})
    tarefas = [asyncio.create_task(apagar(mundo, membro)) for membro in (primeiro, segundo)]
    await _esperar(lambda: len(entraram) == ERASE_CONCURRENCY)

    with pytest.raises(ErasureBusy):
        await apagar(mundo, terceiro)
    assert await existe(mundo, "global", "users", terceiro)

    liberar.set()
    assert all(r.status is ErasureStatus.OK for r in await asyncio.gather(*tarefas))
    assert (await apagar(mundo, terceiro)).counts["users"] == 1  # liberou a vaga


@pytest.mark.asyncio
async def test_t8_teto_global_de_60_execucoes_por_hora_nada_e_apagado_ao_exceder(mundo):
    for numero in range(60):
        assert (await apagar(mundo, 100000000000000000 + numero)).status is ErasureStatus.OK
    await gravar_usuario(mundo, ALVO, token=SEGREDO_TOKEN)

    with pytest.raises(ErasureRateLimited) as erro:
        await apagar(mundo)

    assert 0 < erro.value.retry_after_s <= 3600
    assert await existe(mundo, "global", "users", ALVO)
    mundo.relogio.avancar(3601)
    assert (await apagar(mundo)).counts["users"] == 1


# ------------------------------------------------------------------------------------------------ R6


@pytest.mark.asyncio
async def test_r6_tres_ok_por_hora_depois_nada_e_apagado_e_a_hora_seguinte_libera(mundo):
    for _ in range(3):
        await apagar(mundo)
    await gravar_usuario(mundo, ALVO, token=SEGREDO_TOKEN)

    with pytest.raises(ErasureRateLimited) as erro:
        await apagar(mundo)

    assert erro.value.retry_after_s == pytest.approx(3600)
    assert await existe(mundo, "global", "users", ALVO)
    mundo.relogio.avancar(3601)
    assert (await apagar(mundo)).status is ErasureStatus.OK


@pytest.mark.asyncio
async def test_r6_partial_nao_conta_nas_3_por_hora_mas_dez_tentativas_travam(mundo, monkeypatch):
    await gravar(mundo, "global", "radio_user", ALVO, {"rev": 1})
    original = mundo.database.delete_data

    async def radio_user_falha(id_, db_name, collection):
        if db_name == "radio_user":
            raise RuntimeError("fora do ar")
        return await original(id_, db_name=db_name, collection=collection)

    monkeypatch.setattr(mundo.database, "delete_data", radio_user_falha)
    for _ in range(5):  # mais que as 3 de OK: PARTIAL não conta
        assert (await apagar(mundo)).status is ErasureStatus.PARTIAL

    monkeypatch.setattr(mundo.database, "delete_data", original)
    assert (await apagar(mundo)).status is ErasureStatus.OK  # a nova tentativa imediata é permitida

    monkeypatch.setattr(mundo.database, "delete_data", radio_user_falha)
    await gravar(mundo, "global", "radio_user", ALVO, {"rev": 1})
    for _ in range(4):  # chega a 10 tentativas (5 + 1 + 4)
        assert (await apagar(mundo)).status is ErasureStatus.PARTIAL
    with pytest.raises(ErasureRateLimited):
        await apagar(mundo)


@pytest.mark.asyncio
async def test_origem_e_dono_invalidos_sao_recusados(mundo):
    with pytest.raises(ValueError):
        await mundo.service.erase_user(ALVO, source="web")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await mundo.service.erase_user(ALVO, source="owner")  # sem owner_id e grant_id
    for dono, concessao in ((0, "g"), (42, ""), (None, "g"), (42, None)):  # vazio ou zero também é recusado
        with pytest.raises(ValueError):
            await mundo.service.erase_user(ALVO, source="owner", owner_id=dono, grant_id=concessao)


# ------------------------------------------------------------------------------------------------ T13


@pytest.mark.asyncio
async def test_t13_updated_by_so_o_id_exato_vira_deleted_e_o_premium_fica(mundo):
    radios = {
        f"{BOT}:{SERVIDOR}": {"rev": 3, "updated_by": str(ALVO), "enabled": True},
        f"{BOT}:{SERVIDOR_2}": {"rev": 1, "updated_by": str(PREFIXO_DO_ALVO)},
        f"{BOT + 1}:{SERVIDOR}": {"rev": 1, "updated_by": "system"},
    }
    estacoes = {
        "a" * 24: {"rev": 0, "updated_by": str(ALVO), "name": "Rádio"},
        "b" * 24: {"rev": 0, "updated_by": str(SUFIXO_DO_ALVO)},
    }
    for chave, doc in radios.items():
        await gravar(mundo, "global", "radios", chave, doc)
    for chave, doc in estacoes.items():
        await gravar(mundo, "global", "radio_stations", chave, doc)
    await gravar(mundo, "global", "premium", SERVIDOR, {"granted_by": str(ALVO), "until": 1})

    relatorio = await apagar(mundo)

    async def ler(colecao, chave):
        return await mundo.database.find_doc(chave, db_name=colecao, collection="global")

    anonimizada = await ler("radios", f"{BOT}:{SERVIDOR}")
    assert anonimizada["updated_by"] == "deleted" and anonimizada["rev"] == 4 and anonimizada["enabled"] is True
    assert (await ler("radios", f"{BOT}:{SERVIDOR_2}"))["updated_by"] == str(PREFIXO_DO_ALVO)
    assert (await ler("radios", f"{BOT + 1}:{SERVIDOR}"))["updated_by"] == "system"
    estacao = await ler("radio_stations", "a" * 24)
    assert estacao["updated_by"] == "deleted" and estacao["name"] == "Rádio"
    assert (await ler("radio_stations", "b" * 24))["updated_by"] == str(SUFIXO_DO_ALVO)
    assert (await ler("premium", SERVIDOR))["granted_by"] == str(ALVO)
    assert (relatorio.counts["radios"], relatorio.counts["radio_stations"]) == (1, 1)


@pytest.mark.asyncio
async def test_updated_by_de_documento_sem_rev_nao_da_para_anonimizar_com_cas_e_vira_partial(mundo):
    await gravar(mundo, "global", "radios", f"{BOT}:{SERVIDOR}", {"updated_by": str(ALVO)})

    relatorio = await apagar(mundo)

    assert relatorio.status is ErasureStatus.PARTIAL and relatorio.failed == ("radios",)


# ------------------------------------------------------------------------------------------------ T15


@pytest.mark.asyncio
async def test_t15_auditoria_leva_origem_estado_e_contagens_sem_valor_apagado_e_nao_abre_logs(
    mundo, monkeypatch, caplog
):
    await semear_membro(mundo, ALVO)
    abertos: list[str] = []
    real = builtins.open

    def espiao(arquivo, *args, **kwargs):
        abertos.append(str(arquivo))
        return real(arquivo, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", espiao)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        await apagar(mundo, source="panel")
        await apagar(mundo, source="owner", owner_id=42, grant_id="concessao-1")

    linhas = [r.getMessage() for r in caplog.records if r.getMessage().startswith("erasure ")]
    assert linhas[0].startswith(f"erasure source=panel user={ALVO} status=OK users=1 scrobbles=1 radio_user=1")
    assert linhas[1].startswith(f"erasure source=owner user={ALVO} status=OK") and linhas[1].endswith(
        "owner=42 grant=concessao-1"
    )
    assert "owner=" not in linhas[0]
    for segredo in (SEGREDO_PREFIXO, SEGREDO_CHAVE, SEGREDO_TOKEN, "fulano"):
        assert segredo not in caplog.text
    assert not [caminho for caminho in abertos if ".logs" in caminho]


# ------------------------------------------------------------------------------------------------ memória, RPC e sessões (T9, T10)


def _loja_de_sessoes() -> SessionStore:
    return SessionStore(load_settings(https_config()), clock=lambda: 0.0)


def _usuario(user_id: int) -> PanelUser:
    return PanelUser(id=str(user_id), username=f"u{user_id}", global_name=None, avatar=None)


@pytest.mark.asyncio
async def test_memoria_caches_rpc_e_sessoes_do_painel_so_do_alvo(mundo):
    outro = PREFIXO_DO_ALVO
    loja = mundo.pool.panel_sessions = _loja_de_sessoes()
    ids = [loja.create(_usuario(ALVO), ()) for _ in range(3)] + [loja.create(_usuario(outro), ())]
    mundo.pool.lastfm_sessions.update({ALVO: {"sessionkey": SEGREDO_CHAVE}, outro: {"sessionkey": "x"}})
    mundo.pool.user_prefix_cache.update({ALVO: "!", outro: "?"})

    relatorio = await apagar(mundo)

    assert mundo.rpc_fechados == [ALVO, ALVO]  # passo 2 e passo 5
    assert mundo.pool.lastfm_sessions == {outro: {"sessionkey": "x"}} and mundo.pool.user_prefix_cache == {outro: "?"}
    assert [loja.get(sid) is None for sid in ids] == [True, True, True, False]
    assert relatorio.counts["memory"] == 7  # 2 caches + RPC + 3 sessões, e o RPC falso "fecha" de novo no passo 5


@pytest.mark.asyncio
async def test_sessao_do_painel_criada_durante_a_execucao_tambem_e_destruida_no_passo_6(mundo, monkeypatch):
    loja = mundo.pool.panel_sessions = _loja_de_sessoes()
    limpar = mundo.service._clear_caches

    def limpar_e_um_login_chega(user_id):
        loja.create(_usuario(user_id), ())
        limpar(user_id)

    monkeypatch.setattr(mundo.service, "_clear_caches", limpar_e_um_login_chega)

    await apagar(mundo)

    assert loja.destroy_user(str(ALVO)) == 0


# ------------------------------------------------------------------------------------------------ R2 (ressurreição)


@pytest.mark.asyncio
async def test_r2_preenchimento_em_andamento_entre_os_passos_2_e_3_e_limpo_no_passo_5(mundo, monkeypatch):
    await semear_membro(mundo, ALVO)
    apagar_armazenamento = mundo.service._erase_store

    async def apaga_e_um_preenchimento_atrasado_acontece(store, user_id):
        resultado = await apagar_armazenamento(store, user_id)
        mundo.pool.lastfm_sessions[user_id] = {"scrobble": True, "sessionkey": SEGREDO_CHAVE}
        mundo.pool.user_prefix_cache[user_id] = SEGREDO_PREFIXO
        return resultado

    monkeypatch.setattr(mundo.service, "_erase_store", apaga_e_um_preenchimento_atrasado_acontece)

    await apagar(mundo)

    assert mundo.pool.lastfm_sessions == {} and mundo.pool.user_prefix_cache == {}


@pytest.mark.asyncio
async def test_r2_passada_2_remove_valor_identico_mantem_valor_novo_e_apaga_scrobbles(mundo):
    original = await gravar_usuario(
        mundo, ALVO, fav_links={"a": "https://x/y"}, token=SEGREDO_TOKEN, lastfm_sessionkey=SEGREDO_CHAVE
    )
    await apagar(mundo)
    # escritores atrasados: o mesmo documento de antes, mas o membro já tinha um token NOVO; e a fila de scrobbles de volta
    ressuscitado = deepcopy(original) | {"token": "N" * 50}
    await mundo.database.update_data(ALVO, ressuscitado, db_name="users", collection="global")
    await gravar_scrobbles(mundo, ALVO, "faixa velha")
    mundo.pool.lastfm_sessions[ALVO] = {"sessionkey": SEGREDO_CHAVE}

    mundo.gate.set()
    await mundo.service.wait_rechecks()

    doc = await mundo.database.find_doc(ALVO, db_name="users", collection="global")
    assert doc["fav_links"] == {} and doc["lastfm"]["sessionkey"] == ""  # idênticos ao apagado: voltaram ao padrão
    assert doc["token"] == "N" * 50  # valor novo: é uso do membro e fica
    assert not await existe(mundo, "scrobbles", "users", ALVO, local=True)
    assert mundo.pool.lastfm_sessions == {}


@pytest.mark.asyncio
async def test_r2_passada_2_apaga_o_documento_quando_so_sobra_o_padrao(mundo):
    original = await gravar_usuario(mundo, ALVO, fav_links={"a": "https://x/y"}, lastfm_sessionkey=SEGREDO_CHAVE)
    await apagar(mundo)
    await mundo.database.update_data(ALVO, deepcopy(original), db_name="users", collection="global")

    mundo.gate.set()
    await mundo.service.wait_rechecks()

    assert not await existe(mundo, "global", "users", ALVO)


@pytest.mark.asyncio
async def test_r2_passada_2_espera_60_s_e_roda_sob_o_lock_do_usuario(mundo):
    await gravar_usuario(mundo, ALVO, token=SEGREDO_TOKEN)
    dormiu: list[float] = []

    async def dormir(segundos):
        dormiu.append(segundos)
        await mundo.gate.wait()

    mundo.service._sleep = dormir
    await apagar(mundo)
    await _esperar(lambda: dormiu)

    mundo.gate.set()
    async with mundo.pool.user_locks.lock(ALVO):  # com o lock tomado, a passada 2 não anda
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert mundo.service._tasks  # ainda pendente
    await mundo.service.wait_rechecks()
    assert dormiu == [ERASE_RECHECK_S] and not mundo.service._tasks


@pytest.mark.asyncio
async def test_r2_falha_na_passada_2_vira_aviso_e_nao_levanta(mundo, monkeypatch, caplog):
    await gravar_usuario(mundo, ALVO, token=SEGREDO_TOKEN)
    await apagar(mundo)

    async def falha(*args, **kwargs):
        raise RuntimeError("banco fora do ar")

    monkeypatch.setattr(mundo.database, "find_doc", falha)
    mundo.gate.set()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await mundo.service.wait_rechecks()

    assert "ERASE-RECHECK users falhou (RuntimeError)" in caplog.text and "banco fora do ar" not in caplog.text


# ------------------------------------------------------------------------------------------------ R3 (m1)


def test_r3_o_lock_da_conta_no_painel_e_o_mesmo_objeto_do_apagamento():
    locks = UserLocks()
    servico = SettingsService(locks)

    assert servico.lock("users", ALVO) is locks.lock(ALVO)
    assert servico.lock("global", ALVO) is not servico.lock("users", ALVO)  # outros escopos seguem como eram
    assert SettingsService().lock("users", ALVO) is not locks.lock(ALVO)


@pytest.mark.asyncio
async def test_r3_gravacao_concorrente_da_conta_no_painel_espera_o_apagamento(mundo, monkeypatch):
    await gravar_usuario(mundo, ALVO, token=SEGREDO_TOKEN)
    painel = SettingsService(mundo.pool.user_locks)
    liberar, entraram = await _bloquear_leitura_do_usuario(mundo, monkeypatch, {ALVO})
    apagando = asyncio.create_task(apagar(mundo))
    await _esperar(lambda: entraram)
    gravou = []

    async def gravacao_da_conta():
        async with painel.lock("users", ALVO):
            gravou.append(True)

    gravando = asyncio.create_task(gravacao_da_conta())
    for _ in range(10):
        await asyncio.sleep(0)
    assert gravou == []  # esperando o lock do apagamento

    liberar.set()
    await apagando
    await gravando
    assert gravou == [True]


# ------------------------------------------------------------------------------------------------ revisão do Crivo (m1 a m3, m5)


def _sempre_ressuscita(mundo, monkeypatch, doc):
    original = mundo.database.delete_data

    async def apaga_e_regrava(id_, db_name, collection):
        resultado = await original(id_, db_name=db_name, collection=collection)
        if db_name == "users":
            await mundo.database.update_data(id_, deepcopy(doc), db_name="users", collection="global")
        return resultado

    monkeypatch.setattr(mundo.database, "delete_data", apaga_e_regrava)


@pytest.mark.asyncio
async def test_sobrou_de_novo_depois_da_segunda_tentativa_vira_partial(mundo, monkeypatch):
    doc = await gravar_usuario(mundo, ALVO, token=SEGREDO_TOKEN)
    _sempre_ressuscita(mundo, monkeypatch, doc)

    relatorio = await apagar(mundo)

    assert relatorio.status is ErasureStatus.PARTIAL and relatorio.failed == ("users",)


@pytest.mark.asyncio
async def test_falha_ao_fechar_o_rpc_vira_partial_na_memoria_e_o_resto_e_apagado(mundo):
    await semear_membro(mundo, ALVO)

    def falha(user_id):
        raise RuntimeError("rpc")

    mundo.service._rpc_closer = falha

    relatorio = await apagar(mundo)

    assert relatorio.status is ErasureStatus.PARTIAL and relatorio.failed == ("memory",)
    assert not await existe(mundo, "global", "users", ALVO)


@pytest.mark.asyncio
@pytest.mark.parametrize("falha_na_chamada", [1, 2])  # 1 = passo 2, 2 = passo 6 (sessão criada durante a execução)
async def test_falha_ao_destruir_sessoes_do_painel_vira_partial(mundo, falha_na_chamada):
    chamadas = []

    class LojaQueFalha:
        def destroy_user(self, user_id):
            chamadas.append(user_id)
            if len(chamadas) == falha_na_chamada:
                raise RuntimeError("loja")
            return 0

    mundo.pool.panel_sessions = LojaQueFalha()

    relatorio = await apagar(mundo)

    assert relatorio.status is ErasureStatus.PARTIAL and relatorio.failed == ("memory",) and len(chamadas) == 2


@pytest.mark.asyncio
async def test_falha_ao_ler_o_documento_do_usuario_para_a_passada_2_vira_partial_em_users(mundo, monkeypatch):
    await semear_membro(mundo, ALVO)
    original = mundo.database.find_doc
    falhou = []

    async def primeira_leitura_falha(id_, *, db_name, collection):
        if db_name == "users" and collection == "global" and not falhou:
            falhou.append(True)
            raise RuntimeError("banco")
        return await original(id_, db_name=db_name, collection=collection)

    monkeypatch.setattr(mundo.database, "find_doc", primeira_leitura_falha)

    relatorio = await apagar(mundo)

    assert relatorio.status is ErasureStatus.PARTIAL and relatorio.failed == ("users",)
    assert not await existe(mundo, "global", "users", ALVO)  # apagou; só não dá para garantir a passada 2


@pytest.mark.asyncio
async def test_cache_do_banco_cheio_no_meio_da_execucao_e_esvaziado_no_passo_5(mundo, monkeypatch):
    await semear_membro(mundo, ALVO)
    chave = f"global:users:{ALVO}"
    apagar_armazenamento = mundo.service._erase_store

    async def apaga_e_um_cache_atrasado_enche(store, user_id):
        resultado = await apagar_armazenamento(store, user_id)
        mundo.database.cache[chave] = {"token": SEGREDO_TOKEN}
        return resultado

    monkeypatch.setattr(mundo.service, "_erase_store", apaga_e_um_cache_atrasado_enche)

    await apagar(mundo)

    assert chave not in mundo.database.cache


@pytest.mark.asyncio
async def test_anonimizar_tenta_todos_um_documento_ruim_nao_deixa_o_bom_com_o_id(mundo):
    await gravar(mundo, "global", "radios", f"{BOT}:{SERVIDOR}", {"updated_by": str(ALVO)})  # sem rev
    await gravar(mundo, "global", "radios", f"{BOT}:{SERVIDOR_2}", {"updated_by": str(ALVO), "rev": 1})

    relatorio = await apagar(mundo)

    bom = await mundo.database.find_doc(f"{BOT}:{SERVIDOR_2}", db_name="radios", collection="global")
    assert bom["updated_by"] == "deleted"
    assert relatorio.status is ErasureStatus.PARTIAL and relatorio.failed == ("radios",)


@pytest.mark.asyncio
async def test_anonimizar_com_cas_em_conflito_relê_e_conclui(mundo, monkeypatch):
    chave = f"{BOT}:{SERVIDOR}"
    await gravar(mundo, "global", "radios", chave, {"updated_by": str(ALVO), "rev": 1})
    original = mundo.database.save_doc_cas
    tentativas = []

    async def primeiro_conflito(id_, doc, *, expected_rev, db_name, collection):
        tentativas.append(expected_rev)
        if len(tentativas) == 1:  # outro escritor passou na frente
            await original(
                id_,
                {**doc, "updated_by": str(ALVO), "rev": 2, "x": 1},
                expected_rev=1,
                db_name=db_name,
                collection=collection,
            )
            return False
        return await original(id_, doc, expected_rev=expected_rev, db_name=db_name, collection=collection)

    monkeypatch.setattr(mundo.database, "save_doc_cas", primeiro_conflito)

    relatorio = await apagar(mundo)

    final = await mundo.database.find_doc(chave, db_name="radios", collection="global")
    assert relatorio.status is ErasureStatus.OK and tentativas == [1, 2]
    assert final["updated_by"] == "deleted" and final["rev"] == 3 and final["x"] == 1


@pytest.mark.asyncio
async def test_anonimizar_com_cas_sempre_em_conflito_esgota_as_tentativas_e_vira_partial(mundo, monkeypatch):
    await gravar(mundo, "global", "radios", f"{BOT}:{SERVIDOR}", {"updated_by": str(ALVO), "rev": 1})

    async def sempre_conflito(*args, **kwargs):
        return False

    monkeypatch.setattr(mundo.database, "save_doc_cas", sempre_conflito)

    relatorio = await apagar(mundo)

    assert relatorio.status is ErasureStatus.PARTIAL and relatorio.failed == ("radios",)


@pytest.mark.asyncio
async def test_anonimizar_nao_mexe_em_documento_cujo_updated_by_mudou_no_meio(mundo, monkeypatch):
    chave = f"{BOT}:{SERVIDOR}"
    await gravar(mundo, "global", "radios", chave, {"updated_by": str(ALVO), "rev": 1})
    original = mundo.database.find_doc
    mudou = []

    async def outro_membro_assume_antes_da_releitura(id_, *, db_name, collection):
        if db_name == "radios" and id_ == chave and not mudou:
            mudou.append(True)  # entre a varredura e a releitura do CAS, outro membro mudou a rádio
            await mundo.database.update_data(
                id_, {"updated_by": "999", "rev": 2}, db_name="radios", collection="global"
            )
        return await original(id_, db_name=db_name, collection=collection)

    monkeypatch.setattr(mundo.database, "find_doc", outro_membro_assume_antes_da_releitura)

    relatorio = await apagar(mundo)

    assert relatorio.status is ErasureStatus.OK
    assert (await original(chave, db_name="radios", collection="global"))["updated_by"] == "999"  # o do outro fica


# ------------------------------------------------------------------------------------------------ v4: RPC, erro interno, lock


@pytest.mark.asyncio
async def test_r2_rpc_aberta_entre_os_passos_2_e_3_cai_no_passo_5_e_outra_depois_cai_na_passada_2(mundo):
    await semear_membro(mundo, ALVO)
    await apagar(mundo)
    assert mundo.rpc_fechados == [ALVO, ALVO]  # passos 2 e 5

    mundo.gate.set()  # uma conexão registrada depois do passo 5 cai na passada 2
    await mundo.service.wait_rechecks()

    assert mundo.rpc_fechados == [ALVO, ALVO, ALVO]


@pytest.mark.asyncio
async def test_erro_inesperado_em_um_passo_vira_partial_internal_ainda_e_auditado_e_contado(mundo, monkeypatch, caplog):
    await semear_membro(mundo, ALVO)

    async def quebra(user_id):
        raise ValueError("detalhe-interno-secreto")

    monkeypatch.setattr(mundo.service, "_erase_queue", quebra)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        relatorio = await apagar(mundo)

    assert relatorio.status is ErasureStatus.PARTIAL and relatorio.failed == ("internal",)
    linhas = [r.getMessage() for r in caplog.records if r.getMessage().startswith("erasure ")]
    assert linhas == [f"erasure source=slash user={ALVO} status=PARTIAL  failed=internal"]
    assert "detalhe-interno-secreto" not in caplog.text and "detalhe-interno-secreto" not in repr(relatorio)
    assert mundo.service._tries[ALVO]  # a tentativa contou
    assert mundo.pool.erasure_tombstones.stale(ALVO, mundo.relogio() - 1)  # a sepultura ficou marcada


@pytest.mark.asyncio
async def test_r3_erase_user_pega_o_lock_sozinho_quem_o_segura_so_espera_e_nada_e_reentrante(mundo):
    """O E3 não pode segurar o lock em volta de ``erase_user``: o ``asyncio.Lock`` não é reentrante e o serviço esperaria para sempre."""
    await gravar_usuario(mundo, ALVO, token=SEGREDO_TOKEN)
    lock = mundo.pool.user_locks.lock(ALVO)
    await lock.acquire()
    apagando = asyncio.create_task(apagar(mundo))
    for _ in range(20):
        await asyncio.sleep(0)
    assert not apagando.done() and await existe(mundo, "global", "users", ALVO)  # esperando o lock de quem o segura

    lock.release()
    assert (await apagando).status is ErasureStatus.OK


@pytest.mark.asyncio
async def test_o_primeiro_mark_ja_vale_durante_a_exclusao_leitura_anterior_e_descartada_no_meio_do_delete(
    mundo, monkeypatch
):
    await gravar_usuario(mundo, ALVO, token=SEGREDO_TOKEN)
    mundo.relogio.avancar(1)
    lido_antes = mundo.pool.erasure_tombstones.now()
    mundo.relogio.avancar(1)
    original = mundo.database.delete_data
    durante: list[bool] = []

    async def delete_que_confere_a_sepultura(id_, db_name, collection):
        if db_name == "users":
            durante.append(mundo.pool.erasure_tombstones.stale(ALVO, lido_antes))  # só o 1º mark existe aqui
        return await original(id_, db_name=db_name, collection=collection)

    monkeypatch.setattr(mundo.database, "delete_data", delete_que_confere_a_sepultura)

    await apagar(mundo)

    assert durante == [True]


@pytest.mark.asyncio
async def test_duas_auditorias_em_60_s_saem_inteiras_pelo_saneador_e_pelo_agregador_reais(mundo):
    """O agregador junta WARNING de mesmo template; a trilha de auditoria leva ``empiremuse_emit`` e nunca é resumida."""
    from empiremuse.logs.aggregate import RepeatAggregator
    from empiremuse.logs.redact import SanitizeAndRedact

    saidas: list[str] = []

    class Captura(logging.Handler):
        def emit(self, record):
            saidas.append(record.getMessage())

    captura = Captura()
    captura.addFilter(SanitizeAndRedact())
    captura.addFilter(RepeatAggregator(lambda record: saidas.append(record.getMessage()), clock=mundo.relogio))
    registro = logging.getLogger(LOGGER)
    registro.addHandler(captura)
    registro.setLevel(logging.WARNING)
    try:
        await gravar_usuario(mundo, ALVO, token=SEGREDO_TOKEN)
        await apagar(mundo)
        await apagar(mundo, PREFIXO_DO_ALVO)
        await apagar(mundo)  # o mesmo membro de novo, ainda dentro dos 60 s
    finally:
        registro.removeHandler(captura)

    auditoria = [linha for linha in saidas if linha.startswith("erasure ")]
    assert [linha.split()[2] for linha in auditoria] == [f"user={ALVO}", f"user={PREFIXO_DO_ALVO}", f"user={ALVO}"]
    assert all("status=OK" in linha and "users=" in linha for linha in auditoria)
    assert not [linha for linha in saidas if linha.startswith("↻")]  # nada virou "repetida N×"
