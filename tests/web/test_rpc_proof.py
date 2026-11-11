# ruff: noqa: F811 - os fixtures ``iniciar_servidor``, ``sessao`` e ``segredo`` são importados de test_rpc_websocket
"""T-533a (P1 a P12): o registro de usuário no ``/ws`` prova a posse do ID pelo token guardado.

Servidor Tornado real em porta efêmera (a infraestrutura de ``test_rpc_websocket``), banco e relógio falsos, sem ``sleep``: os pontos de
sincronização são eventos do próprio servidor (estado do banco falso, retorno da prova) e as esperas só têm um teto contra travamento.
"""

import asyncio
import json
import logging
from types import SimpleNamespace

import aiohttp
import pytest

from empiremuse import rpc_protocol, web_app
from empiremuse.rules.rpc_proof import LogThrottle, proof_ip_key
from empiremuse.rules.user_ban import RPC_BLOCKED_REASON
from empiremuse.utils.client import BotPool
from empiremuse.utils.rpc_proof import TokenProof
from tests.web import test_user_ban_rpc as ban_rpc
from tests.web.test_rpc_websocket import (  # noqa: F401 - fixtures e utilitários do servidor real
    _conectar,
    _mensagem_usuario,
    _receber,
    _registrar_bot,
    _snowflake,
    _token_do_app,
    iniciar_servidor,
    segredo,
    sessao,
)

pytestmark = [pytest.mark.critical, pytest.mark.regression]

TOKEN = "a" * 50
OUTRO_TOKEN = "b" * 50
AUTH_LIGADA = {"ENABLE_RPC_AUTH": "true"}
PROVA_NEGADA = {"op": "disconnect", "reason": rpc_protocol.PROOF_DENIED_REASON}


class BancoFalso:
    """``find_doc`` como o dos bancos reais, com portões (``gates``) e erros por ID; ``get_data`` e ``get_global_data`` quebram o teste."""

    def __init__(self):
        self.docs: dict[int, dict] = {}
        self.todos_com_token: str | None = None  # todo ID tem um documento com este token (vetores de paridade)
        self.calls: list[int] = []
        self.gates: dict[int, asyncio.Event] = {}
        self.erros: dict[int, Exception] = {}
        self.em_voo = 0
        self.pico = 0

    async def find_doc(self, id_, *, db_name, collection):
        assert (db_name, collection) == ("users", "global")
        self.calls.append(id_)
        self.em_voo += 1
        self.pico = max(self.pico, self.em_voo)
        try:
            if id_ in self.gates:
                await self.gates[id_].wait()
            if id_ in self.erros:
                raise self.erros[id_]
            if id_ in self.docs:
                return dict(self.docs[id_])
            return {"token": self.todos_com_token} if self.todos_com_token else None
        finally:
            self.em_voo -= 1

    async def get_data(self, *args, **kwargs):
        raise AssertionError("a prova nunca pode usar get_data (insere o padrão)")

    get_global_data = get_data


@pytest.fixture
def banco(monkeypatch):
    falso = BancoFalso()
    monkeypatch.setattr(BotPool, "database", property(lambda self: falso))
    return falso


@pytest.fixture
def provas(monkeypatch):
    """Cada ``_prove_ownership`` que começa (``handlers``) e termina (``terminadas``): os pontos de sincronização de quem testa o servidor."""
    registro = SimpleNamespace(handlers=[], terminadas=[])
    original = web_app.WebSocketHandler._prove_ownership

    async def espiar(self, ids, token):
        registro.handlers.append(self)
        resultado = await original(self, ids, token)
        registro.terminadas.append(resultado)
        return resultado

    monkeypatch.setattr(web_app.WebSocketHandler, "_prove_ownership", espiar)
    return registro


@pytest.fixture
def servidor(iniciar_servidor, banco, segredo):
    return iniciar_servidor({"RPC_SERVER_SECRET": segredo, **AUTH_LIGADA})


@pytest.fixture
def com_verificador(iniciar_servidor, banco, segredo, monkeypatch):
    """Sobe o servidor com o ``TokenProof`` com tempo e teto ajustados (o do código usa 4 s e 16)."""

    def iniciar(**ajustes):
        def fabricar(get_database):
            verificador = TokenProof(get_database, **ajustes)
            iniciar.verificadores.append(verificador)
            return verificador

        monkeypatch.setattr(web_app, "TokenProof", fabricar)
        return iniciar_servidor({"RPC_SERVER_SECRET": segredo, **AUTH_LIGADA})

    iniciar.verificadores = []
    return iniciar


def com_token(ids, token=TOKEN, **extra):
    return _mensagem_usuario(ids, token, **extra)


async def esperar(pronto, teto: float = 2.0):
    """Cede o loop até ``pronto()``: o servidor roda no mesmo loop, então o estado dele é o evento (o ``teto`` só barra travamento)."""
    async with asyncio.timeout(teto):
        while not pronto():
            await asyncio.sleep(0)


async def ceder(vezes: int = 200):
    """Dá ao servidor várias voltas do loop para agir (a prova de que algo NÃO aconteceu, sem relógio)."""
    for _ in range(vezes):
        await asyncio.sleep(0)


async def ate_fechar(ws) -> tuple[list[str], int | None]:
    """Os textos recebidos (crus) até o servidor fechar a conexão, e o código do fechamento."""
    textos = []
    while True:
        msg = await ws.receive(timeout=2.0)
        if msg.type == aiohttp.WSMsgType.TEXT:
            textos.append(msg.data)
        else:
            return textos, ws.close_code


async def registrar(servidor, sessao, ids, token=TOKEN, *, esperando_estado=True):
    """Conecta e registra um usuário; com ``esperando_estado`` espera o último ID aparecer em ``users_ws``."""
    ws = await _conectar(servidor, sessao)
    await ws.send_json(com_token(ids, token))
    if esperando_estado:
        await esperar(lambda: ids[-1] in web_app.users_ws)
    return ws


async def tentar(servidor, sessao, ids, token=TOKEN, **extra):
    """Registra e devolve (textos, código) da recusa."""
    ws = await _conectar(servidor, sessao)
    await ws.send_json(com_token(ids, token, **extra))
    return await ate_fechar(ws)


async def canario(servidor, sessao, ws_bot, banco):
    """Um usuário legítimo registrado depois: o PRIMEIRO repasse que o bot vê tem de ser o dele (nada de antes chegou ao bot)."""
    uid = _snowflake()
    banco.docs[uid] = {"token": TOKEN}
    await registrar(servidor, sessao, [uid])
    assert (await _receber(ws_bot))["user_ids"] == [uid]


# --- P1: ID alheio ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p1_a_foreign_id_is_refused_the_same_way_in_every_case_and_displaces_nobody(
    servidor, sessao, banco, segredo
):
    x, sem_documento, token_vazio = _snowflake(), _snowflake(), _snowflake()
    banco.docs[x] = {"token": TOKEN}
    banco.docs[token_vazio] = {"token": ""}
    ws_bot, _ = await _registrar_bot(servidor, sessao, segredo)
    ws_a = await registrar(servidor, sessao, [x])
    dono = web_app.users_ws[x]
    await _receber(ws_bot)  # o repasse do registro legítimo

    ws_sem_token = await _conectar(servidor, sessao)
    mensagem = com_token([x])
    del mensagem["token"]
    await ws_sem_token.send_json(mensagem)
    resultados = {
        "sem token": await ate_fechar(ws_sem_token),
        "token errado": await tentar(servidor, sessao, [x], OUTRO_TOKEN),
        "token vazio": await tentar(servidor, sessao, [x], ""),
        "ID sem documento": await tentar(servidor, sessao, [sem_documento], TOKEN),
        "documento com token vazio": await tentar(servidor, sessao, [token_vazio], TOKEN),
    }

    assert list(resultados.values()) == [([json.dumps(PROVA_NEGADA)], rpc_protocol.CLOSE_REFUSED)] * len(
        resultados
    )  # mesmos bytes e código
    assert web_app.users_ws == {x: dono}  # ninguém foi deslocado e nada entrou
    assert not ws_a.closed
    await canario(servidor, sessao, ws_bot, banco)  # nada foi aos bots


# --- P2: dono legítimo -----------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p2_the_legitimate_owner_displaces_the_previous_session(servidor, sessao, banco, segredo):
    x = _snowflake()
    banco.docs[x] = {"token": TOKEN}
    ws_bot, _ = await _registrar_bot(servidor, sessao, segredo)
    ws_a = await registrar(servidor, sessao, [x])
    primeira = web_app.users_ws[x]
    await _receber(ws_bot)

    ws_b = await registrar(servidor, sessao, [x])
    await esperar(lambda: web_app.users_ws[x] is not primeira)

    textos, codigo = await ate_fechar(ws_a)
    assert [json.loads(t)["op"] for t in textos] == ["disconnect"] and codigo == rpc_protocol.CLOSE_REFUSED
    assert not ws_b.closed
    assert (await _receber(ws_bot))["user_ids"] == [x]


# --- P3: paridade D6 (banido x livre) --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p3_banned_and_free_ids_give_the_same_observations_with_a_valid_token(
    servidor, sessao, banco, monkeypatch
):
    banco.todos_com_token = TOKEN
    monkeypatch.setattr(ban_rpc, "_token_do_app", lambda: TOKEN)
    banido, livre = _snowflake(), _snowflake()
    ban_rpc.banir(servidor, banido)

    assert await ban_rpc._resultado_do_vetor_a(servidor, sessao, banido) == await ban_rpc._resultado_do_vetor_a(
        servidor, sessao, livre
    )
    assert await ban_rpc._resultado_do_vetor_b(
        servidor, sessao, _snowflake(), banido
    ) == await ban_rpc._resultado_do_vetor_b(servidor, sessao, _snowflake(), livre)


@pytest.mark.asyncio
async def test_p3_banned_and_free_ids_get_the_same_refusal_and_the_proof_never_asks_about_the_ban(
    servidor, sessao, banco, caplog
):
    caplog.set_level(logging.DEBUG)
    banido, livre = _snowflake(), _snowflake()
    bans = ban_rpc.banir(servidor, banido)
    consultas = []
    original = bans.is_blocked
    monkey_is_blocked = lambda uid: consultas.append(uid) or original(uid)  # noqa: E731
    bans.is_blocked = monkey_is_blocked

    recusa_do_banido = await tentar(servidor, sessao, [banido], OUTRO_TOKEN)
    recusa_do_livre = await tentar(servidor, sessao, [livre], OUTRO_TOKEN)

    assert recusa_do_banido == recusa_do_livre == ([json.dumps(PROVA_NEGADA)], rpc_protocol.CLOSE_REFUSED)
    assert consultas == []
    assert str(banido) not in caplog.text and str(livre) not in caplog.text


# --- P4: a leitura não tem efeito ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p4_unknown_and_erased_ids_create_no_document(iniciar_servidor, sessao, segredo, tmp_path, monkeypatch):
    from empiremuse.utils.db import LocalDatabase

    banco_real = LocalDatabase(dir_=str(tmp_path / "banco"))
    monkeypatch.setattr(BotPool, "database", property(lambda self: banco_real))
    servidor = iniciar_servidor({"RPC_SERVER_SECRET": segredo, **AUTH_LIGADA})
    desconhecido, apagado = _snowflake(), _snowflake()
    tabela = banco_real._connect["global"]["users"]
    tabela.insert_one({"_id": str(apagado), "token": ""})

    for uid in (desconhecido, apagado):
        assert (await tentar(servidor, sessao, [uid], TOKEN))[1] == rpc_protocol.CLOSE_REFUSED

    assert tabela.find_one({"_id": str(desconhecido)}) is None  # nada foi criado
    assert tabela.find_one({"_id": str(apagado)}) == {"_id": str(apagado), "token": ""}  # e o apagado continua apagado


@pytest.mark.asyncio
async def test_p4_the_proof_only_reads_through_find_doc(servidor, sessao, banco):
    """``BancoFalso.get_data``/``get_global_data`` levantam: um uso delas derrubaria o registro como "indisponível"."""
    x = _snowflake()
    banco.docs[x] = {"token": TOKEN}

    await registrar(servidor, sessao, [x])

    assert banco.calls == [x] and x in web_app.users_ws


# --- P5: vários IDs --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p5_only_the_verified_ids_register_and_the_others_vanish_silently(servidor, sessao, banco, segredo):
    x, sem_documento, de_outro = _snowflake(), _snowflake(), _snowflake()
    banco.docs[x] = {"token": TOKEN}
    banco.docs[de_outro] = {"token": OUTRO_TOKEN}
    ws_bot, _ = await _registrar_bot(servidor, sessao, segredo)

    ws = await registrar(servidor, sessao, [sem_documento, x, de_outro], esperando_estado=False)
    await esperar(lambda: x in web_app.users_ws)

    assert set(web_app.users_ws) == {x} and web_app.users_ws[x].user_ids == [x]
    assert (await _receber(ws_bot))["user_ids"] == [x]
    assert not ws.closed


@pytest.mark.asyncio
async def test_p5_a_repeated_id_makes_one_query_and_the_rotated_token_is_refused_at_once(servidor, sessao, banco):
    x = _snowflake()
    banco.docs[x] = {"token": TOKEN}

    await registrar(servidor, sessao, [x, x])
    assert banco.calls == [x]

    banco.docs[x] = {"token": OUTRO_TOKEN}  # o token foi trocado entre dois registros: sem cache
    assert await tentar(servidor, sessao, [x], TOKEN) == ([json.dumps(PROVA_NEGADA)], rpc_protocol.CLOSE_REFUSED)


@pytest.mark.asyncio
async def test_p5_with_auth_off_the_registration_is_unchanged_and_queries_nothing(iniciar_servidor, sessao, banco):
    servidor = iniciar_servidor()  # ENABLE_RPC_AUTH=false
    x = _snowflake()

    await registrar(servidor, sessao, [x], token=_token_do_app())

    assert banco.calls == [] and x in web_app.users_ws


# --- P6: indisponível ------------------------------------------------------------------------------------------------


async def _assegurar_recusa_por_indisponibilidade(
    servidor, sessao, banco, ids, segredo, *, dono_x, preparar=lambda: lambda: None
):
    """Registrar ``ids`` falha com 1013, sem frame, sem efeito local e sem deslocar quem tem ``dono_x``."""
    banco.docs[dono_x] = {"token": TOKEN}
    ws_bot, _ = await _registrar_bot(servidor, sessao, segredo)
    ws_dono = await registrar(servidor, sessao, [dono_x])
    dono = web_app.users_ws[dono_x]
    await _receber(ws_bot)
    restaurar = preparar()

    textos, codigo = await tentar(servidor, sessao, ids)

    assert textos == [] and codigo == rpc_protocol.CLOSE_TRY_AGAIN_LATER  # sem ``op disconnect``: o app reconecta
    assert web_app.users_ws == {dono_x: dono} and not ws_dono.closed
    restaurar()
    await canario(servidor, sessao, ws_bot, banco)


@pytest.mark.asyncio
async def test_p6_a_database_error_is_a_retry_not_a_disconnect_and_leaks_nothing(
    servidor, sessao, banco, segredo, caplog
):
    x = _snowflake()
    banco.docs[x] = {"token": TOKEN}
    banco.erros[x] = RuntimeError("mongodb://usuario:senha-secreta@host/db")

    await _assegurar_recusa_por_indisponibilidade(servidor, sessao, banco, [x], segredo, dono_x=_snowflake())

    avisos = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(avisos) == 1 and "RuntimeError" in avisos[0].getMessage()
    assert "senha-secreta" not in caplog.text and str(x) not in caplog.text and TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_p6_a_timeout_is_a_retry_not_a_disconnect(com_verificador, sessao, banco, segredo):
    servidor = com_verificador()
    x = _snowflake()
    banco.gates[x] = asyncio.Event()  # nunca liberado

    def esgotar_o_tempo():  # tempo 0 esgota na próxima volta do loop: sem espera de relógio (o dono já se registrou)
        verificador = com_verificador.verificadores[0]
        verificador._timeout = 0
        return lambda: setattr(verificador, "_timeout", rpc_protocol.PROOF_TIMEOUT)

    await _assegurar_recusa_por_indisponibilidade(
        servidor, sessao, banco, [x], segredo, dono_x=_snowflake(), preparar=esgotar_o_tempo
    )


@pytest.mark.asyncio
async def test_p6_a_mixed_list_with_one_failing_id_refuses_the_whole_registration(servidor, sessao, banco, segredo):
    bom, falha = _snowflake(), _snowflake()
    banco.docs[bom] = {"token": TOKEN}
    banco.erros[falha] = ConnectionError("x")

    await _assegurar_recusa_por_indisponibilidade(servidor, sessao, banco, [bom, falha], segredo, dono_x=_snowflake())

    assert bom not in web_app.users_ws  # nada de registro parcial


# --- P7: a conexão fecha durante a prova -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p7_closing_during_the_proof_registers_nothing_displaces_nobody_and_raises_nothing(
    servidor, sessao, banco, segredo, provas, caplog
):
    """O servidor fecha a conexão com a prova pendente (``close_rpc_for``, tempo do ping): o token é válido, mas o registro não vale.

    Um fechamento do cliente só é visto depois da prova (o Tornado lê em série); este é o caso que a guarda cobre.
    """
    x = _snowflake()
    banco.docs[x] = {"token": TOKEN}
    ws_bot, _ = await _registrar_bot(servidor, sessao, segredo)
    ws_dono = await registrar(servidor, sessao, [x])
    dono = web_app.users_ws[x]
    await _receber(ws_bot)
    provas.terminadas.clear()
    banco.gates[x] = asyncio.Event()

    ws_b = await _conectar(servidor, sessao)
    await ws_b.send_json(com_token([x]))
    await esperar(lambda: len(banco.calls) == 2)  # a prova do invasor está pendente
    provas.handlers[-1].close()  # fechamento pelo servidor: ``ws_connection`` vira ``None``
    banco.gates[x].set()
    await esperar(lambda: len(provas.terminadas) == 1)  # a prova terminou depois do fechamento

    assert provas.terminadas == [None]
    assert web_app.users_ws == {x: dono} and not ws_dono.closed
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR] and "WebSocketClosedError" not in caplog.text
    await canario(servidor, sessao, ws_bot, banco)


# --- P8: amplificação ------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p8_proofs_in_flight_never_exceed_the_cap_and_the_excess_gets_1013_without_a_query(
    com_verificador, sessao, banco
):
    servidor = com_verificador(max_in_flight=2)
    ids = [_snowflake() for _ in range(3)]
    for uid in ids:
        banco.docs[uid] = {"token": TOKEN}
        banco.gates[uid] = asyncio.Event()
    sockets = []
    for uid in ids[:2]:
        ws = await _conectar(servidor, sessao)
        await ws.send_json(com_token([uid]))
        sockets.append(ws)
    await esperar(lambda: len(banco.calls) == 2)

    textos, codigo = await tentar(servidor, sessao, [ids[2]])

    assert (textos, codigo) == ([], rpc_protocol.CLOSE_TRY_AGAIN_LATER)
    assert banco.calls == ids[:2] and banco.pico == 2  # o excedente não consultou
    for uid in ids[:2]:
        banco.gates[uid].set()
    await esperar(lambda: set(web_app.users_ws) == set(ids[:2]))


@pytest.fixture
def ip_confiavel(monkeypatch):
    """O par do teste é 127.0.0.1 (privado): com proxy confiável ele vira chave, como atrás do Traefik."""
    monkeypatch.setattr(web_app, "proof_ip_key", lambda ip, trusted: proof_ip_key(ip, True))


@pytest.mark.asyncio
async def test_p8_denials_from_one_ip_are_refused_before_the_query_after_the_limit(
    servidor, sessao, banco, ip_confiavel
):
    x = _snowflake()
    banco.docs[x] = {"token": TOKEN}

    for _ in range(rpc_protocol.PROOF_DENIED_LIMIT):
        assert (await tentar(servidor, sessao, [x], OUTRO_TOKEN))[1] == rpc_protocol.CLOSE_REFUSED
    consultas = len(banco.calls)

    textos, codigo = await tentar(servidor, sessao, [x], TOKEN)  # até o token certo: o limite vem antes da consulta

    assert (textos, codigo) == ([], rpc_protocol.CLOSE_RATE_LIMITED)
    assert len(banco.calls) == consultas and x not in web_app.users_ws


@pytest.mark.asyncio
async def test_p8_unavailable_proofs_never_count_toward_the_limit(servidor, sessao, banco, ip_confiavel):
    x = _snowflake()
    banco.docs[x] = {"token": TOKEN}
    banco.erros[x] = RuntimeError("fora do ar")
    for _ in range(rpc_protocol.PROOF_DENIED_LIMIT + 2):
        assert (await tentar(servidor, sessao, [x]))[1] == rpc_protocol.CLOSE_TRY_AGAIN_LATER

    del banco.erros[x]
    await registrar(servidor, sessao, [x])  # o orçamento está intacto

    assert x in web_app.users_ws


@pytest.mark.asyncio
async def test_p8_without_a_trusted_proxy_a_private_peer_has_no_per_ip_limit(servidor, sessao, banco):
    x = _snowflake()
    banco.docs[x] = {"token": TOKEN}

    for _ in range(rpc_protocol.PROOF_DENIED_LIMIT + 2):
        assert (await tentar(servidor, sessao, [x], OUTRO_TOKEN))[1] == rpc_protocol.CLOSE_REFUSED  # nunca 4429

    await registrar(servidor, sessao, [x])
    assert x in web_app.users_ws


# --- P9: ordem por conexão -------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p9_a_second_registration_on_the_same_connection_waits_for_the_first(servidor, sessao, banco):
    primeiro, segundo = _snowflake(), _snowflake()
    banco.docs[primeiro] = {"token": TOKEN}
    banco.docs[segundo] = {"token": TOKEN}
    banco.gates[primeiro] = asyncio.Event()
    ws = await _conectar(servidor, sessao)

    await ws.send_json(com_token([primeiro]))
    await esperar(lambda: banco.calls == [primeiro])
    await ws.send_json(com_token([segundo]))
    await ceder()
    assert banco.calls == [primeiro]  # o segundo não começou com o primeiro pendente

    banco.gates[primeiro].set()
    await esperar(lambda: segundo in web_app.users_ws)

    assert banco.calls == [primeiro, segundo] and primeiro not in web_app.users_ws  # o segundo substituiu o primeiro


# --- P10: ligação em run_app -----------------------------------------------------------------------------------------


def test_p10_run_app_wires_the_verifier_only_with_auth_and_a_database(monkeypatch, caplog):
    com_banco = SimpleNamespace(database=object())
    sem_banco = SimpleNamespace(database=None)

    ligado = web_app._build_registration_proof(com_banco, {"ENABLE_RPC_AUTH": True})
    assert isinstance(ligado["token_verifier"], TokenProof) and ligado["denied_budget"].limit == (
        rpc_protocol.PROOF_DENIED_LIMIT
    )
    assert web_app._build_registration_proof(com_banco, {"ENABLE_RPC_AUTH": False}) == {}
    assert caplog.records == []  # auth desligada: nem verificador nem aviso

    with caplog.at_level(logging.WARNING):
        assert web_app._build_registration_proof(sem_banco, {"ENABLE_RPC_AUTH": True}) == {}
    assert "NÃO prova a posse do ID" in caplog.text and "RUN_RPC_SERVER=true" in caplog.text


@pytest.mark.asyncio
async def test_p10_a_relay_without_a_database_registers_as_before_and_warns_at_boot(
    iniciar_servidor, sessao, segredo, caplog
):
    with caplog.at_level(logging.WARNING):
        servidor = iniciar_servidor(
            {"RPC_SERVER_SECRET": segredo, **AUTH_LIGADA}
        )  # BotPool sem banco: ``database`` é None
    x = _snowflake()

    await registrar(servidor, sessao, [x], token=_token_do_app())

    assert x in web_app.users_ws and "NÃO prova a posse do ID" in caplog.text


# --- P11: log --------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p11_the_unavailable_warning_has_only_the_type_and_comes_once_per_interval(
    servidor, sessao, banco, monkeypatch, caplog
):
    relogio = [1000.0]
    monkeypatch.setattr(web_app, "_proof_unavailable_log", LogThrottle(60.0, lambda: relogio[0]))
    x = _snowflake()
    banco.docs[x] = {"token": TOKEN}
    banco.erros[x] = TimeoutError("senha-secreta")
    caplog.set_level(logging.DEBUG)

    for _ in range(3):
        await tentar(servidor, sessao, [x])
    relogio[0] += 61
    await tentar(servidor, sessao, [x])

    avisos = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(avisos) == 2 and "TimeoutError" in avisos[0] and "2 suprimidos" in avisos[1]
    assert not any(str(x) in t or TOKEN in t or "senha-secreta" in t for t in [caplog.text])


@pytest.mark.asyncio
async def test_p11_the_denied_path_logs_neither_id_nor_token(servidor, sessao, banco, caplog):
    caplog.set_level(logging.DEBUG)
    x = _snowflake()
    banco.docs[x] = {"token": TOKEN}

    await tentar(servidor, sessao, [x], OUTRO_TOKEN)

    assert str(x) not in caplog.text and OUTRO_TOKEN not in caplog.text and TOKEN not in caplog.text


# --- P12: aviso de banimento -----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p12_the_ban_notice_only_reaches_the_verified_connection(servidor, sessao, banco):
    x = _snowflake()
    banco.docs[x] = {"token": TOKEN}
    ws_dono = await registrar(servidor, sessao, [x])
    invasor = await _conectar(servidor, sessao)
    await invasor.send_json(com_token([x], OUTRO_TOKEN))
    textos_do_invasor, _ = await ate_fechar(invasor)

    assert web_app.close_rpc_for(x, RPC_BLOCKED_REASON) is True

    avisos, codigo = await ate_fechar(ws_dono)
    assert [json.loads(t) for t in avisos] == [{"op": "disconnect", "reason": RPC_BLOCKED_REASON}]
    assert codigo == rpc_protocol.CLOSE_REFUSED
    assert [json.loads(t) for t in textos_do_invasor] == [PROVA_NEGADA]  # o invasor só soube que o token é inválido
