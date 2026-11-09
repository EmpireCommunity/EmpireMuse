"""F3-4 — servidor web/RPC (empiremuse/web_app.py): compatibilidade do protocolo, registro de bots, origem, limites e robustez.

Sem Discord, sem .env real, sem setup() nem Java e sem rede externa: o servidor Tornado real sobe numa porta efêmera em
127.0.0.1 (``TCPServer.listen`` é substituído só para escolher a porta e registrar os argumentos), a ``BotPool`` real é
construída com o ambiente isolado em tmp_path e as opções chegam pelas variáveis de ambiente, como para o operador.
Segredos e IDs são montados em tempo de execução.

Convenção: ``test_guarda_*`` caracterizam o comportamento atual (passam hoje e precisam continuar passando); os demais
falham hoje pelo motivo descrito na docstring. O app desktop (irmão EmpireMuse-RPC-app) é só lido, nunca alterado.
"""

import ast
import asyncio
import copy
import datetime
import json
import logging
import re
import secrets
import time
import traceback
from collections import deque
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from tornado.tcpserver import TCPServer
from tornado.testing import bind_unused_port

from empiremuse import rpc_protocol, web_app
from empiremuse.utils.client import BotPool
from empiremuse.utils.music.models import LavalinkPlayer, LavalinkTrack
from empiremuse.web_app import WSClient
from tests.support.raiz import RAIZ

pytestmark = pytest.mark.critical

ROOT = RAIZ

VERSAO_APP = "3.0.0"
ESPERA = 2.0  # teto de espera por um evento do servidor
ASSENTAR = 0.15  # tempo para o servidor processar uma mensagem que não tem resposta
TAMANHO_MAXIMO_MENSAGEM = 64 * 1024
AMBIENTE = ("RPC_SERVER_SECRET", "RPC_ALLOWED_ORIGINS", "WEB_BIND_ADDRESS", "ENABLE_RPC_AUTH", "PORT", "SERVER_PORT")

# O que LavalinkPlayer.process_rpc produz (caracterizado nos testes abaixo). A lista repassada ao usuário é a união
# disso, sem ``token`` (nunca repassado) e sem ``auth_enabled`` (hoje descartado pelo servidor; o app não o consome).
CAMPOS_UPDATE = {
    "auth_enabled", "bot_id", "bot_name", "guild", "lastfm_user", "listen_along_invite", "op", "start_time", "thumb",
    "token", "track", "user",
}  # fmt: skip
CAMPOS_IDLE = (CAMPOS_UPDATE - {"start_time"}) | {
    "idle_endtime", "idle_starttime", "invite_permissions", "public", "support_server",
}  # fmt: skip
CAMPOS_CLOSE = {"auth_enabled", "bot_id", "bot_name", "lastfm_user", "op", "thumb", "token", "user"}
CAMPOS_TRACK = {
    "247", "author", "autoplay", "duration", "loop", "paused", "position", "queue", "requester_id", "source", "stream",
    "thumb", "title", "url",
}  # fmt: skip
NUNCA_REPASSADOS = {"token", "auth_enabled"}
CAMPOS_REPASSADOS = {
    "update": CAMPOS_UPDATE - NUNCA_REPASSADOS,
    "idle": CAMPOS_IDLE - NUNCA_REPASSADOS,
    "close": CAMPOS_CLOSE - NUNCA_REPASSADOS,
}
# O que o app desktop lê das mensagens do servidor (conferido na fonte dele em test_guarda_app_desktop_consome_...).
CAMPOS_USUARIO_PARA_BOT = {"op", "user_ids", "version"}


def _snowflake() -> int:
    return secrets.randbelow(9 * 10**17) + 10**17  # 18 dígitos


def _token_do_app() -> str:
    return secrets.token_hex(25)  # 50 caracteres, como o gerado pelo comando /rich_presence


def _mensagem_usuario(ids: list, token: str = "", **extra) -> dict:
    """O que o app desktop envia ao conectar (rpc_client.py: handle_socket)."""
    return {"op": "rpc_update", "user_ids": ids, "token": token, "version": VERSAO_APP, **extra}


# ---------------------------------------------------------------------------------------------------------------------
# Infraestrutura: servidor real em porta efêmera, sessão HTTP e utilitários de leitura
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class _Servidor:
    pool: BotPool
    porta: int
    escuta: dict

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.porta}"

    @property
    def url_ws(self) -> str:
        return f"ws://127.0.0.1:{self.porta}/ws"


@pytest_asyncio.fixture
async def iniciar_servidor(tmp_path, monkeypatch):
    """Devolve ``iniciar(env)``, que sobe o servidor real com as opções vindas do ambiente (como o operador as define)."""
    for nome in ("home", "cwd"):
        (tmp_path / nome).mkdir()
    monkeypatch.setenv("EMPIREMUSE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path / "cwd")
    for nome in AMBIENTE:
        monkeypatch.delenv(nome, raising=False)
    monkeypatch.setenv("ENABLE_RPC_AUTH", "false")  # o padrão do código é true; os casos ligam a auth quando precisam
    web_app.users_ws.clear()
    web_app.bots_ws.clear()
    abertos = []

    def iniciar(env: dict | None = None) -> _Servidor:
        for nome, valor in (env or {}).items():
            monkeypatch.setenv(nome, valor)
        pool = BotPool()
        escuta: dict = {}

        def escutar(self, port, address=None, **kwargs):
            sock, porta = bind_unused_port()
            self.add_sockets([sock])
            escuta.update(port=port, address=address, kwargs=kwargs, porta=porta)
            abertos.append(self)

        monkeypatch.setattr(TCPServer, "listen", escutar)
        web_app.run_app(pool)
        return _Servidor(pool=pool, porta=escuta["porta"], escuta=escuta)

    yield iniciar

    for http in abertos:
        http.stop()
        await http.close_all_connections()
    web_app.users_ws.clear()
    web_app.bots_ws.clear()


@pytest_asyncio.fixture
async def sessao():
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0)) as s:
        yield s


@pytest.fixture
def segredo() -> str:
    return secrets.token_hex(16)


async def _conectar(servidor: _Servidor, sessao, *, chave: str | None = None, origem: str | None = None):
    cabecalhos = {}
    if chave is not None:
        cabecalhos["X-RPC-Key"] = chave
    if origem is not None:
        cabecalhos["Origin"] = origem
    return await sessao.ws_connect(servidor.url_ws, headers=cabecalhos)


async def _registrar_bot(servidor: _Servidor, sessao, chave: str | None, ids: list | None = None):
    """Conecta e registra um bot como o WSClient faz (``user_ids`` + ``bot: True``); devolve (ws, ids)."""
    ids = ids or [_snowflake()]
    ws = await _conectar(servidor, sessao, chave=chave)
    await ws.send_json({"user_ids": ids, "bot": True})
    await asyncio.sleep(ASSENTAR)
    return ws, ids


async def _registrar_usuario(servidor: _Servidor, sessao, ids: list, token: str = "", **extra):
    ws = await _conectar(servidor, sessao)
    await ws.send_json(_mensagem_usuario(ids, token, **extra))
    await asyncio.sleep(ASSENTAR)
    return ws


async def _receber(ws, espera: float = ESPERA) -> dict:
    msg = await ws.receive(timeout=espera)
    assert msg.type == aiohttp.WSMsgType.TEXT, f"esperava um texto, veio {msg.type.name} (código {ws.close_code})"
    return json.loads(msg.data)


async def _nada_recebido(ws, espera: float = 0.4) -> bool:
    """True se nenhum TEXTO chegou em ``espera`` s (um fechamento da conexão não é conteúdo)."""
    try:
        msg = await ws.receive(timeout=espera)
    except asyncio.TimeoutError:
        return True
    return msg.type != aiohttp.WSMsgType.TEXT


async def _fechamento(ws, espera: float = ESPERA) -> int | None:
    """Código com que o servidor fechou a conexão (-1 se sem código), ou None se ela continuou aberta."""
    try:
        while True:
            msg = await ws.receive(timeout=espera)
            if msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            ):
                return ws.close_code if ws.close_code is not None else -1
    except asyncio.TimeoutError:
        return None


# ---------------------------------------------------------------------------------------------------------------------
# O que LavalinkPlayer.process_rpc produz (o REAL, com dublês de bot/guild/faixa)
# ---------------------------------------------------------------------------------------------------------------------


class _UsuarioDoBot:
    id = 111111111111111111
    display_avatar = SimpleNamespace(replace=lambda **kwargs: SimpleNamespace(url="https://exemplo.invalid/avatar.png"))

    def __str__(self) -> str:
        return "Bot#0001"


async def _stats(op: str, usuario: int, token_no_banco: str, auth: bool = False) -> dict:
    """Roda o process_rpc real e devolve o dict que ele entrega ao ``ws_client.send`` (op: update, idle ou close)."""
    enviados: list[dict] = []
    jogador = object.__new__(LavalinkPlayer)
    jogador.bot = SimpleNamespace(
        user=_UsuarioDoBot(),
        config={"ENABLE_RPC_AUTH": auth, "INVITE_PERMISSIONS": 3145728, "SUPPORT_SERVER": "https://exemplo.invalid/s"},
        appinfo=SimpleNamespace(bot_public=True),
        loop=asyncio.get_running_loop(),
        get_global_data=AsyncMock(return_value={"token": token_no_banco, "lastfm": {"username": "usuario-fm"}}),
        ws_client=SimpleNamespace(send=AsyncMock(side_effect=lambda d: enviados.append(copy.deepcopy(d)))),
    )
    agora = datetime.datetime.now(datetime.timezone.utc)
    jogador.guild = SimpleNamespace(name="Servidor Falso")
    jogador.is_closing = False
    jogador.listen_along_invite = ""
    jogador.queue = deque()
    jogador.keep_connected = False
    jogador.loop = False
    jogador.start_timestamp = 1700000000.0
    jogador.start_time = agora
    jogador.idle_endtime = agora
    jogador.idle_start_timestamp = 1700000000
    jogador.channel_id = 1
    jogador.paused = False
    jogador.auto_pause = False
    jogador.last_position = 0
    jogador.last_update = time.time() * 1000
    info = {
        "title": "Titulo - Teste", "author": "Autor", "length": 200000, "identifier": "abcdefghijk", "isStream": False,
        "uri": "https://exemplo.invalid/x", "sourceName": "youtube", "position": 0, "artworkUrl": "",
    }  # fmt: skip
    jogador.current = LavalinkTrack(id_="id", info=info, requester=1) if op == "update" else None

    if op == "close":
        await jogador.process_rpc(close=True, users=[usuario], wait=True)
    else:
        await jogador.process_rpc(SimpleNamespace(voice_states={}), users=[usuario], wait=True)
    assert enviados, "o process_rpc não entregou nada ao ws_client"
    return enviados[0]


@pytest.mark.asyncio
async def test_guarda_process_rpc_update_envia_exatamente_estes_campos():
    stats = await _stats("update", _snowflake(), _token_do_app())

    assert stats["op"] == "update"
    assert set(stats) == CAMPOS_UPDATE
    assert set(stats["track"]) == CAMPOS_TRACK


@pytest.mark.asyncio
async def test_guarda_process_rpc_close_envia_exatamente_estes_campos():
    stats = await _stats("close", _snowflake(), _token_do_app())

    assert stats["op"] == "close"
    assert set(stats) == CAMPOS_CLOSE


# ---------------------------------------------------------------------------------------------------------------------
# O que o app desktop envia e consome (só leitura da fonte dele)
# ---------------------------------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------------------------------
# O que o WSClient (bot) manda no registro e consome (servidor falso em aiohttp, sem o servidor Tornado)
# ---------------------------------------------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def servidor_aiohttp_falso():
    """``montar(roteiro)`` sobe um /ws falso; ``roteiro(ws, request)`` conduz a conversa. Devolve a URL."""
    runners = []

    async def montar(roteiro) -> str:
        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await roteiro(ws, request)
            return ws

        app = web.Application()
        app.router.add_get("/ws", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        runners.append(runner)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        return f"ws://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/ws"

    yield montar
    for runner in runners:
        await runner.cleanup()


def _bot_falso(bot_id: int, jogadores: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(id=bot_id),
        wait_until_ready=AsyncMock(),
        music=SimpleNamespace(players=jogadores or {}),
        loop=asyncio.get_running_loop(),
    )


def _wsclient(url: str, bots: list, tmp_path, monkeypatch) -> WSClient:
    monkeypatch.setenv("EMPIREMUSE_HOME", str(tmp_path))
    pool = BotPool()
    pool.get_all_bots = lambda: bots
    return WSClient(url, pool)


def _jogador_na_chamada(usuario: int) -> tuple[SimpleNamespace, SimpleNamespace]:
    canal = SimpleNamespace(voice_states={usuario: object()})
    jogador = SimpleNamespace(
        guild=SimpleNamespace(me=SimpleNamespace(voice=SimpleNamespace(channel=canal))),
        process_rpc=AsyncMock(),
        last_channel=object(),
    )
    return jogador, canal


async def _rodar_ws_loop(servidor_aiohttp_falso, tmp_path, monkeypatch, quadros) -> list[int]:
    """Roda o ws_loop real contra um /ws falso que envia ``quadros(usuarios)`` e fecha.

    Devolve os usuários cujo canal de voz foi atualizado (process_rpc chamado com o canal deles).
    """
    usuarios = [_snowflake(), _snowflake()]
    jogadores, pares = {}, {}
    for i, usuario in enumerate(usuarios):
        jogador, canal = _jogador_na_chamada(usuario)
        jogadores[i] = jogador
        pares[usuario] = (jogador, canal)

    async def roteiro(ws, request):
        await ws.receive_json()  # registro do bot
        for quadro in quadros(usuarios):
            await ws.send_str(quadro)
        await asyncio.sleep(0.2)
        await ws.close()

    url = await servidor_aiohttp_falso(roteiro)
    client = _wsclient(url, [_bot_falso(_snowflake(), jogadores)], tmp_path, monkeypatch)
    try:
        await asyncio.wait_for(client.ws_loop(), 10)
    finally:
        client.clear_tasks()
        if client.session:
            await client.session.close()
    return [
        u
        for u, (jogador, canal) in pares.items()
        if any(c.args == (canal,) for c in jogador.process_rpc.call_args_list)
    ]


def _rpc_update(usuario: int) -> str:
    return json.dumps({"op": "rpc_update", "user_ids": [usuario]})


@pytest.mark.asyncio
async def test_guarda_wsclient_consome_rpc_update_e_atualiza_o_canal_dos_usuarios(
    servidor_aiohttp_falso, tmp_path, monkeypatch
):
    atualizados = await _rodar_ws_loop(
        servidor_aiohttp_falso, tmp_path, monkeypatch, lambda us: [_rpc_update(us[0]), _rpc_update(us[1])]
    )

    assert len(atualizados) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "quadro_invalido",
    [
        "isto nao e json",
        '{"op": "rpc_update", "user_ids": "abc"}',
    ],
    ids=["texto", "user-ids-texto"],
)
async def test_ws_loop_do_cliente_ignora_quadro_invalido_e_continua(
    servidor_aiohttp_falso, tmp_path, monkeypatch, quadro_invalido
):
    """Hoje o ``json.loads``/``.get`` do quadro inválido levanta e derruba o ws_loop: o 2º usuário nunca é atualizado."""
    atualizados = await _rodar_ws_loop(
        servidor_aiohttp_falso,
        tmp_path,
        monkeypatch,
        lambda us: [_rpc_update(us[0]), quadro_invalido, _rpc_update(us[1])],
    )

    assert len(atualizados) == 2


# ---------------------------------------------------------------------------------------------------------------------
# Compatibilidade do relé: o que o servidor repassa (servidor real)
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usuario_para_bot_repassa_so_op_user_ids_e_version(iniciar_servidor, sessao, segredo):
    """Campos extras do usuário não chegam ao bot (hoje o dict inteiro, menos token/auth_enabled/bot, é repassado)."""
    servidor = iniciar_servidor({"RPC_SERVER_SECRET": segredo})
    bot_ws, _ = await _registrar_bot(servidor, sessao, segredo)

    await _registrar_usuario(
        servidor, sessao, [_snowflake()], _token_do_app(), campo_extra="x", outro={"a": 1}, user=_snowflake()
    )

    assert set(await _receber(bot_ws)) == CAMPOS_USUARIO_PARA_BOT


async def _usuario_e_bot(servidor, sessao, segredo, token_do_usuario=""):
    """Bot registrado + usuário registrado (o repasse do registro já foi consumido). Devolve (bot_ws, bot_ids, usuario_ws, usuario)."""
    bot_ws, bot_ids = await _registrar_bot(servidor, sessao, segredo)
    usuario = _snowflake()
    usuario_ws = await _registrar_usuario(servidor, sessao, [usuario], token_do_usuario)
    await _receber(bot_ws)
    return bot_ws, bot_ids, usuario_ws, usuario


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["update", "close"])
async def test_bot_para_usuario_repassa_exatamente_os_campos_do_process_rpc_sem_o_que_for_extra(
    iniciar_servidor, sessao, segredo, op
):
    """A lista repassada é o que o process_rpc produz (sem token); campo desconhecido do bot não chega ao usuário."""
    servidor = iniciar_servidor({"RPC_SERVER_SECRET": segredo})
    bot_ws, _, usuario_ws, usuario = await _usuario_e_bot(servidor, sessao, segredo)
    stats = await _stats(op, usuario, _token_do_app())
    stats.update(campo_extra="x", outro={"a": 1})

    await bot_ws.send_json(stats)
    entregue = await _receber(usuario_ws)

    assert set(entregue) == CAMPOS_REPASSADOS[op]


@pytest.mark.asyncio
async def test_guarda_versao_antiga_do_app_recebe_disconnect_e_nao_e_repassada(iniciar_servidor, sessao, segredo):
    servidor = iniciar_servidor({"RPC_SERVER_SECRET": segredo})
    bot_ws, _ = await _registrar_bot(servidor, sessao, segredo)
    ws = await _conectar(servidor, sessao)

    await ws.send_json(_mensagem_usuario([_snowflake()]) | {"version": "1.0.0"})

    assert (await _receber(ws))["op"] == "disconnect"
    assert await _nada_recebido(bot_ws)


@pytest.mark.asyncio
async def test_guarda_pagina_de_status_responde_e_so_exibe_o_endereco_do_rele(iniciar_servidor, sessao, segredo):
    """A página não abre o /ws: o JS só mostra ``<origem>/ws``. Se um dia abrir, a mesma origem tem de ser aceita."""
    servidor = iniciar_servidor({"RPC_SERVER_SECRET": segredo})

    async with sessao.get(servidor.base + "/") as resposta:
        corpo = await resposta.text()

    assert resposta.status == 200
    assert '"/ws"' in corpo and "new WebSocket" not in corpo


# ---------------------------------------------------------------------------------------------------------------------
# Registro de bot autenticado (X-RPC-Key)
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_guarda_bot_com_a_chave_configurada_e_aceito_e_recebe_os_registros_de_usuario(
    iniciar_servidor, sessao, segredo
):
    servidor = iniciar_servidor({"RPC_SERVER_SECRET": segredo})
    bot_ws, _ = await _registrar_bot(servidor, sessao, segredo)

    await _registrar_usuario(servidor, sessao, [_snowflake()])

    assert (await _receber(bot_ws))["op"] == "rpc_update"


@pytest.mark.asyncio
@pytest.mark.parametrize("chave", [None, ""], ids=["ausente", "vazia"])
async def test_registro_de_bot_fecha_com_4401_tambem_quando_o_segredo_nao_foi_configurado(
    iniciar_servidor, sessao, chave
):
    """Sem RPC_SERVER_SECRET vale o segredo efêmero do processo: chave ausente ou vazia nunca vale."""
    servidor = iniciar_servidor()
    ws = await _conectar(servidor, sessao, chave=chave)

    await ws.send_json({"user_ids": [_snowflake()], "bot": True})

    assert await _fechamento(ws) == 4401


async def _wsclient_interno(servidor: _Servidor) -> WSClient:
    """O WSClient como o BotPool o usa: aponta para o próprio servidor, com a MESMA pool/config, sem segredo à mão."""
    servidor.pool.get_all_bots = lambda: [_bot_falso(_snowflake())]
    client = WSClient(servidor.url_ws, servidor.pool)
    await client.connect()
    await asyncio.sleep(0.4)  # deixa o registro chegar ao servidor
    client.clear_tasks()
    return client


@pytest.mark.asyncio
async def test_guarda_bot_interno_conecta_ao_proprio_servidor_sem_configurar_segredo(iniciar_servidor, sessao):
    """O caso comum (RPC_SERVER=ws://localhost:$PORT/ws, sem RPC_SERVER_SECRET) segue funcionando de fábrica."""
    servidor = iniciar_servidor()
    client = await _wsclient_interno(servidor)
    try:
        await _registrar_usuario(servidor, sessao, [_snowflake()])
        recebido = json.loads((await client.connection.receive(timeout=ESPERA)).data)
    finally:
        await client.connection.close()
        await client.session.close()

    assert recebido["op"] == "rpc_update"


# ---------------------------------------------------------------------------------------------------------------------
# Autenticação por usuário (ENABLE_RPC_AUTH): decidida só pela configuração do servidor
# ---------------------------------------------------------------------------------------------------------------------


async def _entrega(servidor, sessao, segredo, *, token_do_usuario, token_do_bot, auth_no_payload, com_campo=True):
    """Bot envia um ``update`` (do process_rpc real) ao usuário registrado; devolve (primeira mensagem, ws do bot, ws, id)."""
    bot_ws, _, usuario_ws, usuario = await _usuario_e_bot(servidor, sessao, segredo, token_do_usuario)
    stats = await _stats("update", usuario, token_do_bot, auth=auth_no_payload)
    if not com_campo:
        del stats["auth_enabled"]
    await bot_ws.send_json(stats)
    return await _receber(usuario_ws), bot_ws, usuario_ws, usuario


AUTH_LIGADA = {"ENABLE_RPC_AUTH": "true"}


@pytest.mark.asyncio
async def test_guarda_auth_ligada_token_igual_entrega_o_update(iniciar_servidor, sessao, segredo):
    servidor = iniciar_servidor({"RPC_SERVER_SECRET": segredo, **AUTH_LIGADA})
    token = _token_do_app()

    entregue, *_ = await _entrega(
        servidor, sessao, segredo, token_do_usuario=token, token_do_bot=token, auth_no_payload=True
    )

    assert entregue["op"] == "update"


@pytest.mark.asyncio
async def test_guarda_auth_ligada_usuario_bloqueado_nao_recebe_ate_o_token_voltar_a_bater(
    iniciar_servidor, sessao, segredo
):
    servidor = iniciar_servidor({"RPC_SERVER_SECRET": segredo, **AUTH_LIGADA})
    token_certo, token_errado = _token_do_app(), _token_do_app()
    _, bot_ws, usuario_ws, usuario = await _entrega(
        servidor, sessao, segredo, token_do_usuario=token_certo, token_do_bot=token_errado, auth_no_payload=True
    )

    await bot_ws.send_json(await _stats("update", usuario, token_errado, auth=True))
    assert await _nada_recebido(usuario_ws), "usuário bloqueado recebeu dados"

    await bot_ws.send_json(await _stats("update", usuario, token_certo, auth=True))
    assert (await _receber(usuario_ws))["op"] == "update"


# Como o bot informa o token na mensagem: texto vazio, campo ausente ou ``null``.
TOKEN_DO_BOT_VAZIO = ["", "ausente", None]
TOKEN_DO_BOT_VAZIO_IDS = ["vazio", "ausente", "null"]


async def _entrega_com_token_do_bot(servidor, sessao, segredo, *, token_do_usuario, token_do_bot, auth: bool):
    """Como ``_entrega``, mas deixa o token do bot vazio, ausente ou ``null`` na mensagem enviada ao servidor."""
    bot_ws, _, usuario_ws, usuario = await _usuario_e_bot(servidor, sessao, segredo, token_do_usuario)
    # O process_rpc real não monta o update com token vazio e auth ligada: parte de um token válido e o esvazia.
    stats = await _stats("update", usuario, _token_do_app(), auth=auth)
    if token_do_bot == "ausente":
        stats.pop("token", None)
    else:
        stats["token"] = token_do_bot
    await bot_ws.send_json(stats)
    return await _receber(usuario_ws)


def _bloqueou_com_o_aviso_de_token(entregue: dict) -> bool:
    return entregue["op"] == "exception" and "token" in entregue["message"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("token_do_bot", list(TOKEN_DO_BOT_VAZIO)[:2], ids=list(TOKEN_DO_BOT_VAZIO_IDS)[:2])
async def test_guarda_auth_ligada_usuario_com_token_e_bot_sem_token_nao_entrega(
    iniciar_servidor, sessao, segredo, token_do_bot
):
    servidor = iniciar_servidor({"RPC_SERVER_SECRET": segredo, **AUTH_LIGADA})

    entregue = await _entrega_com_token_do_bot(
        servidor, sessao, segredo, token_do_usuario=_token_do_app(), token_do_bot=token_do_bot, auth=True
    )

    assert _bloqueou_com_o_aviso_de_token(entregue)


@pytest.mark.asyncio
@pytest.mark.parametrize("token_do_bot", ["", "ausente"], ids=["vazio", "ausente"])
async def test_guarda_auth_desligada_usuario_sem_token_continua_recebendo(
    iniciar_servidor, sessao, segredo, token_do_bot
):
    """Compatibilidade: o app sem token (auth desligada) segue recebendo os dados do bot."""
    servidor = iniciar_servidor({"RPC_SERVER_SECRET": segredo})

    entregue = await _entrega_com_token_do_bot(
        servidor, sessao, segredo, token_do_usuario="", token_do_bot=token_do_bot, auth=False
    )

    assert entregue["op"] == "update"


@pytest.mark.asyncio
async def test_servidor_com_auth_ligada_ignora_auth_enabled_falso_enviado_pelo_bot(iniciar_servidor, sessao, segredo):
    """Hoje o servidor confia no ``auth_enabled`` do payload: com ``false`` a checagem do token é pulada."""
    servidor = iniciar_servidor({"RPC_SERVER_SECRET": segredo, **AUTH_LIGADA})

    entregue, *_ = await _entrega(
        servidor, sessao, segredo, token_do_usuario=_token_do_app(), token_do_bot=_token_do_app(), auth_no_payload=False
    )

    assert entregue["op"] == "exception"


@pytest.mark.asyncio
async def test_servidor_com_auth_desligada_ignora_auth_enabled_verdadeiro_enviado_pelo_bot(
    iniciar_servidor, sessao, segredo
):
    """Hoje ``auth_enabled: true`` no payload liga a checagem de token mesmo com o servidor em ENABLE_RPC_AUTH=false."""
    servidor = iniciar_servidor({"RPC_SERVER_SECRET": segredo})

    entregue, *_ = await _entrega(
        servidor, sessao, segredo, token_do_usuario=_token_do_app(), token_do_bot=_token_do_app(), auth_no_payload=True
    )

    assert entregue["op"] == "update"


# ---------------------------------------------------------------------------------------------------------------------
# Origem do navegador (check_origin)
# ---------------------------------------------------------------------------------------------------------------------

ORIGENS_PERMITIDAS = "https://painel.exemplo.invalid, https://outro.exemplo.invalid"
AMBIENTE_COM_ORIGENS = {"RPC_ALLOWED_ORIGINS": ORIGENS_PERMITIDAS}


@pytest.mark.asyncio
async def test_guarda_origin_ausente_cliente_nativo_e_aceito(iniciar_servidor, sessao):
    servidor = iniciar_servidor()

    ws = await _conectar(servidor, sessao)

    assert not ws.closed


ORIGENS_ESTRANHAS = [
    "https://site-estranho.exemplo.invalid",
    "https://painel.exemplo.invalid.estranho.invalid",
    "http://painel.exemplo.invalid",
    "null",
    "http://localhost:{porta}",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("origem", list(ORIGENS_ESTRANHAS)[:2])
@pytest.mark.parametrize("com_lista", [True, False], ids=["com-lista", "lista-vazia"])
async def test_origin_de_outra_origem_e_rejeitada_com_403(iniciar_servidor, sessao, origem, com_lista):
    """Hoje ``check_origin`` devolve sempre True."""
    servidor = iniciar_servidor(AMBIENTE_COM_ORIGENS if com_lista else {})

    with pytest.raises(aiohttp.WSServerHandshakeError) as erro:
        await _conectar(servidor, sessao, origem=origem.format(porta=servidor.porta))

    assert erro.value.status == 403


# ---------------------------------------------------------------------------------------------------------------------
# Limites: tamanho da mensagem, IDs, número de conexões e taxa
# ---------------------------------------------------------------------------------------------------------------------


def _json_com_tamanho(base: dict, tamanho: int) -> str:
    """JSON de ``base`` com um campo ``pad`` ajustado para o texto ter exatamente ``tamanho`` caracteres ASCII."""
    vazio = len(json.dumps({**base, "pad": ""}))
    texto = json.dumps({**base, "pad": "x" * (tamanho - vazio)})
    assert len(texto) == tamanho
    return texto


@pytest.mark.asyncio
async def test_guarda_mensagem_de_exatamente_64_kib_e_aceita(iniciar_servidor, sessao, segredo):
    servidor = iniciar_servidor({"RPC_SERVER_SECRET": segredo})
    bot_ws, _ = await _registrar_bot(servidor, sessao, segredo)
    ws = await _conectar(servidor, sessao)

    await ws.send_str(_json_com_tamanho(_mensagem_usuario([_snowflake()]), TAMANHO_MAXIMO_MENSAGEM))

    assert (await _receber(bot_ws))["op"] == "rpc_update"
    assert not ws.closed


def _fechou_de_proposito(codigo: int | None) -> bool:
    """Fechamento decidido pelo servidor (código de aplicação/política), não queda de conexão nem silêncio."""
    return codigo not in (None, -1, 1006)


IDS_VALIDOS = {
    "18-digitos": lambda: [_snowflake()],
    "15-digitos": lambda: [10**14 + secrets.randbelow(9 * 10**14)],
    "20-digitos": lambda: [10**19 + secrets.randbelow(9 * 10**19)],
    "tres-ids": lambda: [_snowflake() for _ in range(3)],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("caso", list(sorted(IDS_VALIDOS))[:2])
async def test_guarda_usuario_com_user_ids_validos_e_aceito_e_repassado(iniciar_servidor, sessao, segredo, caso):
    servidor = iniciar_servidor({"RPC_SERVER_SECRET": segredo})
    bot_ws, _ = await _registrar_bot(servidor, sessao, segredo)
    ids = IDS_VALIDOS[caso]()

    await _registrar_usuario(servidor, sessao, ids)

    assert (await _receber(bot_ws))["user_ids"] == ids


@pytest.mark.asyncio
async def test_guarda_usuario_com_quatro_ids_e_recusado_e_nao_e_repassado(iniciar_servidor, sessao, segredo):
    servidor = iniciar_servidor({"RPC_SERVER_SECRET": segredo})
    bot_ws, _ = await _registrar_bot(servidor, sessao, segredo)
    ws = await _conectar(servidor, sessao)

    await ws.send_json(_mensagem_usuario([_snowflake() for _ in range(4)]))

    codigo = await _fechamento(ws)
    assert _fechou_de_proposito(codigo), f"o servidor não recusou a conexão (código {codigo})"
    assert await _nada_recebido(bot_ws)


# ---------------------------------------------------------------------------------------------------------------------
# JSON inválido ou que não é objeto (servidor): 4400 sem derrubar o handler e sem expor o conteúdo no log
# ---------------------------------------------------------------------------------------------------------------------

QUADROS_INVALIDOS = {
    "texto": "isto nao e json",
    "truncado": '{"op": "rpc_update", "user_ids": [',
    "lista": "[]",
    "texto-json": '"texto"',
    "numero": "42",
    "nulo": "null",
    "booleano": "true",
}


@pytest.mark.asyncio
async def test_guarda_servidor_segue_atendendo_depois_de_uma_mensagem_invalida(iniciar_servidor, sessao, segredo):
    servidor = iniciar_servidor({"RPC_SERVER_SECRET": segredo})
    bot_ws, _ = await _registrar_bot(servidor, sessao, segredo)
    ruim = await _conectar(servidor, sessao)
    await ruim.send_str(QUADROS_INVALIDOS["texto"])
    await _fechamento(ruim)

    await _registrar_usuario(servidor, sessao, [_snowflake()])

    assert (await _receber(bot_ws))["op"] == "rpc_update"


# ---------------------------------------------------------------------------------------------------------------------
# KI-25: fechar uma conexão que nunca foi registrada como bot
# ---------------------------------------------------------------------------------------------------------------------


def _value_error_do_servidor(caplog) -> list[str]:
    """Registros (de qualquer nível) com um ValueError cujo traceback passa por web_app.py.

    O Tornado captura o ValueError de ``on_close`` e o registra em DEBUG ("Malformed WebSocket request received").
    """
    achados = []
    for registro in caplog.records:
        if registro.exc_info and registro.exc_info[0] is ValueError:
            texto = "".join(traceback.format_exception(*registro.exc_info))
            if "web_app.py" in texto:
                achados.append(texto.strip().splitlines()[-1])
    return achados


async def _fechar_e_observar(servidor, sessao, caplog, como: str) -> list[str]:
    """Abre uma conexão, faz ``como`` e a fecha; devolve os ValueError do servidor vistos no log."""
    with caplog.at_level(logging.DEBUG):
        ws = await _conectar(servidor, sessao)
        if como == "bot-rejeitado":
            await ws.send_json({"user_ids": [_snowflake()], "bot": True})
        elif como == "usuario-registrado":
            await ws.send_json(_mensagem_usuario([_snowflake()]))
        elif como == "mensagem-sem-ids":
            await ws.send_json({"op": "rpc_update"})
        await asyncio.sleep(ASSENTAR)
        await ws.close()
        await asyncio.sleep(0.4)
    return _value_error_do_servidor(caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize("como", ["sem-enviar-nada", "mensagem-sem-ids"])
async def test_fechar_conexao_nunca_registrada_nao_gera_value_error(iniciar_servidor, sessao, segredo, caplog, como):
    """Hoje ``bots_ws.remove(self)`` roda para qualquer conexão que não era usuário: ``ValueError: x not in list``."""
    servidor = iniciar_servidor({"RPC_SERVER_SECRET": segredo})

    assert await _fechar_e_observar(servidor, sessao, caplog, como) == []


# ---------------------------------------------------------------------------------------------------------------------
# Endereço de escuta (WEB_BIND_ADDRESS) e aviso quando a auth está desligada e o bind é público
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("endereco", ["127.0.0.1", "::1"])
async def test_web_bind_address_e_repassado_ao_listen(iniciar_servidor, endereco):
    assert iniciar_servidor({"WEB_BIND_ADDRESS": endereco}).escuta["address"] == endereco


def _avisou_sobre_a_auth(caplog, capsys) -> bool:
    textos = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    saida = capsys.readouterr()
    textos += [saida.out, saida.err]
    return any(re.search(r"ENABLE_RPC_AUTH|autentica", t, re.IGNORECASE) for t in textos)


@pytest.mark.asyncio
@pytest.mark.parametrize("env", [{}, {"WEB_BIND_ADDRESS": "::"}], ids=["padrao", "ipv6"])
async def test_aviso_na_inicializacao_quando_a_auth_esta_desligada_e_o_bind_e_publico(
    iniciar_servidor, caplog, capsys, env
):
    """Hoje nada é avisado."""
    with caplog.at_level(logging.WARNING):
        iniciar_servidor(env)

    assert _avisou_sobre_a_auth(caplog, capsys)


# ---------------------------------------------------------------------------------------------------------------------
# Quem pode enviar mensagens bot -> usuário: só uma conexão autenticada E registrada como bot
# ---------------------------------------------------------------------------------------------------------------------


async def _tentar_enviar(ws, payload: dict) -> None:
    """Envia, aceitando que o servidor já possa ter fechado a conexão (o que é o esperado para quem não é bot)."""
    try:
        await ws.send_json(payload)
    except ConnectionError, aiohttp.ClientError, RuntimeError:
        pass


def _mensagens_para_usuario(usuario: int, bot_id: int) -> dict:
    """Formas de mensagem bot -> usuário (sem ``user_ids``), incluindo as que o servidor também gera."""
    return {
        "minimo": {"op": "update", "bot_id": bot_id, "user": usuario},
        "so-user": {"op": "update", "user": usuario},
        "exception": {"op": "exception", "message": "mensagem forjada", "bot_id": bot_id, "user": usuario},
        "close": {"op": "close", "bot_id": bot_id, "user": usuario},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("forma", sorted(_mensagens_para_usuario(0, 0)))
async def test_mensagem_bot_para_usuario_de_conexao_anonima_e_recusada_em_qualquer_forma(
    iniciar_servidor, sessao, segredo, forma
):
    servidor = iniciar_servidor({"RPC_SERVER_SECRET": segredo})
    usuario = _snowflake()
    usuario_ws = await _registrar_usuario(servidor, sessao, [usuario])
    anonima = await _conectar(servidor, sessao)

    await _tentar_enviar(anonima, _mensagens_para_usuario(usuario, _snowflake())[forma])

    assert await _nada_recebido(usuario_ws), "o usuário recebeu uma mensagem de uma conexão anônima"
    assert await _fechamento(anonima) == 4401


# ---------------------------------------------------------------------------------------------------------------------
# Complementos da validação: o bot autenticado só entrega ops conhecidas; sessões; versão e token; WSClient
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["disconnect", 5])
async def test_bot_autenticado_so_entrega_as_ops_update_idle_e_close(iniciar_servidor, sessao, segredo, op):
    """Um ``disconnect`` ou ``exception`` vindo do bot faria o app do usuário se desconectar ou exibir erro alheio."""
    servidor = iniciar_servidor({"RPC_SERVER_SECRET": segredo})
    bot_ws, _, usuario_ws, usuario = await _usuario_e_bot(servidor, sessao, segredo)

    await bot_ws.send_json({"op": op, "bot_id": _snowflake(), "user": usuario, "message": "x", "reason": "x"})

    assert await _nada_recebido(usuario_ws), "o usuário recebeu uma op que o bot não pode enviar"
    assert not bot_ws.closed


@pytest.mark.asyncio
async def test_guarda_usuario_bloqueado_por_token_nao_recebe_o_close_do_bot(iniciar_servidor, sessao, segredo):
    servidor = iniciar_servidor({"RPC_SERVER_SECRET": segredo, **AUTH_LIGADA})
    _, bot_ws, usuario_ws, _ = await _entrega(
        servidor, sessao, segredo, token_do_usuario=_token_do_app(), token_do_bot=_token_do_app(), auth_no_payload=True
    )

    await bot_ws.close()

    assert await _nada_recebido(usuario_ws), "usuário bloqueado recebeu o close do bot"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("versao", "aceita"),
    [("2.6.1", True), ("abc", False)],
)
async def test_versao_minima_do_app_e_2_6_1(iniciar_servidor, sessao, segredo, versao, aceita):
    servidor = iniciar_servidor({"RPC_SERVER_SECRET": segredo})
    bot_ws, _ = await _registrar_bot(servidor, sessao, segredo)
    ws = await _conectar(servidor, sessao)

    await ws.send_json(_mensagem_usuario([_snowflake()]) | {"version": versao})

    if aceita:
        assert (await _receber(bot_ws))["version"] == versao
    else:
        assert (await _receber(ws))["op"] == "disconnect"
        assert await _nada_recebido(bot_ws)


@pytest.mark.asyncio
@pytest.mark.parametrize("tamanho", [1, 100])
async def test_token_de_usuario_so_pode_ter_0_ou_50_caracteres(iniciar_servidor, sessao, segredo, tamanho):
    servidor = iniciar_servidor({"RPC_SERVER_SECRET": segredo})
    bot_ws, _ = await _registrar_bot(servidor, sessao, segredo)
    ws = await _conectar(servidor, sessao)

    await ws.send_json(_mensagem_usuario([_snowflake()], "t" * tamanho))

    assert (await _receber(ws))["op"] == "disconnect"
    assert await _nada_recebido(bot_ws)


@pytest.mark.asyncio
@pytest.mark.parametrize("quem", ["usuario", "bot"])
async def test_lista_vazia_de_user_ids_e_recusada(iniciar_servidor, sessao, segredo, quem):
    servidor = iniciar_servidor({"RPC_SERVER_SECRET": segredo})
    bot_ws, _ = await _registrar_bot(servidor, sessao, segredo)
    ws = await _conectar(servidor, sessao, chave=segredo if quem == "bot" else None)

    await ws.send_json({**_mensagem_usuario([]), **({"bot": True} if quem == "bot" else {})})

    codigo = await _fechamento(ws)
    assert _fechou_de_proposito(codigo), f"o servidor não recusou a lista vazia (código {codigo})"
    assert await _nada_recebido(bot_ws)


@pytest.mark.asyncio
async def test_origin_vazio_nao_e_aceito_por_causa_de_entradas_vazias_na_lista(iniciar_servidor, sessao):
    servidor = iniciar_servidor({"RPC_ALLOWED_ORIGINS": ",, ,https://painel.exemplo.invalid,"})

    with pytest.raises(aiohttp.WSServerHandshakeError) as erro:
        await _conectar(servidor, sessao, origem="")

    assert erro.value.status == 403


# --- WSClient: nada é enviado antes do registro; a sincronização depois dele reenvia o estado --------------------------


# --- protocolo puro (empiremuse/rpc_protocol.py) -----------------------------------------------------------------------


def test_same_secret_so_confere_texto_nao_vazio_e_igual():
    assert rpc_protocol.same_secret("abc", "abc") is True
    for fornecido, esperado in [
        ("abd", "abc"),
        ("abc ", "abc"),
        ("", "abc"),
        ("", ""),
        ("abc", ""),
        (None, "abc"),
        (123, "123"),
    ]:
        assert rpc_protocol.same_secret(fornecido, esperado) is False, (fornecido, esperado)


def test_is_valid_id_limites_exatos():
    assert rpc_protocol.is_valid_id(10**14) is True
    assert rpc_protocol.is_valid_id(10**20 - 1) is True
    for invalido in (10**14 - 1, 10**20, 0, -(10**15), True, 1e17, float(10**17), "100000000000000000", None):
        assert rpc_protocol.is_valid_id(invalido) is False, invalido


def test_normalize_origin_e_parse_allowed_origins():
    assert rpc_protocol.normalize_origin("  HTTPS://Ex.Invalid/ ") == "https://ex.invalid"
    assert rpc_protocol.parse_allowed_origins(" A.invalid/ , ,B.INVALID,,") == frozenset({"a.invalid", "b.invalid"})
    assert rpc_protocol.parse_allowed_origins("") == frozenset()
    assert rpc_protocol.parse_allowed_origins(None) == frozenset()


def test_same_token_trata_ausente_como_vazio_e_compara_exatamente():
    assert rpc_protocol.same_token("", "") is True
    assert rpc_protocol.same_token(None, "") is True
    assert rpc_protocol.same_token("abc", "abc") is True
    assert rpc_protocol.same_token("abc", "abd") is False
    assert rpc_protocol.same_token("abc", None) is False


def test_rate_limiter_janela_deslizante(monkeypatch):
    agora = {"t": 1000.0}
    monkeypatch.setattr(rpc_protocol.time, "monotonic", lambda: agora["t"])
    limitador = rpc_protocol.RateLimiter(limit=3, window=10.0)

    assert [limitador.allow() for _ in range(4)] == [True, True, True, False]
    agora["t"] += 9.99
    assert limitador.allow() is False
    agora["t"] += 0.01  # exatamente 10 s depois das primeiras mensagens
    assert limitador.allow() is True


@pytest.mark.asyncio
async def test_conexao_alem_do_limite_e_fechada_com_1013(iniciar_servidor, sessao, monkeypatch):
    """O limite de conexões (MAX_CONNECTIONS = 1000 em produção) é reduzido por monkeypatch."""
    limite = 3
    monkeypatch.setattr(web_app, "MAX_CONNECTIONS", limite)
    servidor = iniciar_servidor()
    abertas = [await _conectar(servidor, sessao) for _ in range(limite)]
    assert await _fechamento(abertas[-1], ASSENTAR) is None, "a última conexão dentro do limite foi recusada"
    excedente = await _conectar(servidor, sessao)
    assert await _fechamento(excedente) == 1013, "a conexão acima do limite não foi recusada com 1013"
    await abertas[0].close()
    await asyncio.sleep(ASSENTAR)
    de_novo = await _conectar(servidor, sessao)
    assert await _fechamento(de_novo, ASSENTAR) is None, "o limite não foi liberado quando uma conexão fechou"


@pytest.mark.asyncio
async def test_close_rpc_for_fecha_so_a_conexao_do_usuario(iniciar_servidor, sessao):
    """T9 (apagar dados): a conexão aberta do usuário cai com ``CLOSE_REFUSED`` e sai de ``users_ws``; a de outro usuário fica."""
    servidor = iniciar_servidor()
    alvo, outro = _snowflake(), _snowflake()
    ws_alvo = await _registrar_usuario(servidor, sessao, [alvo], _token_do_app())
    ws_outro = await _registrar_usuario(servidor, sessao, [outro], _token_do_app())

    assert web_app.close_rpc_for(alvo) is True

    assert await _fechamento(ws_alvo) == rpc_protocol.CLOSE_REFUSED
    assert await _fechamento(ws_outro, ASSENTAR) is None
    assert alvo not in web_app.users_ws and outro in web_app.users_ws
    assert web_app.close_rpc_for(alvo) is False  # sem conexão: nada a fechar, nem erro


def test_close_rpc_for_com_a_conexao_ja_fechada_nao_levanta_e_ainda_a_remove(monkeypatch):
    """T9: ``write_message`` numa conexão que já caiu levanta ``WebSocketClosedError``; o fechamento segue e a entrada sai."""
    import tornado.websocket

    chamadas = []

    class ConexaoCaida:
        def write_message(self, *_):
            raise tornado.websocket.WebSocketClosedError

        def _forget_users(self):
            chamadas.append("esquece")

        def _reject(self, code, reason):
            chamadas.append(("fecha", code))

    monkeypatch.setitem(web_app.users_ws, 4242, ConexaoCaida())

    assert web_app.close_rpc_for(4242) is True
    assert chamadas == ["esquece", ("fecha", rpc_protocol.CLOSE_REFUSED)]
