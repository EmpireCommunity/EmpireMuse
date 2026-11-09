"""Item 7 (P1, Turbine) — B2 / V-L2: toda fonte de segredo "aprendida" chega ao redator por ``register_secret``.

O ``collect_secret_values`` só vê o valor INTEIRO de variáveis com nome de segredo (``LAVALINK_NODE_*`` é um JSON): a senha de dentro dele,
as do ``lavalink.ini``, da lista baixada, os tokens dos bots, os ``refresh_tokens`` do Mongo e a senha do Lavalink local
precisam ser registradas. O teste roda o ``BotPool.setup`` REAL (bots, rede e Java dublados) e olha o efeito no ``redact``: o valor sai
mascarado, inclusive na forma URL-encoded. Os valores não têm formato de token, para só o registro poder mascará-los.
"""

import asyncio
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote

import pytest

from empiremuse.logs.redact import REDACTED, redact
from empiremuse.utils import client
from empiremuse.utils.client import BotPool
from tests.support.laco_do_pool import encerrar_laco

pytestmark = pytest.mark.critical

TOKEN_FALSO = "MTIzNDU2Nzg5MDEyMzQ1Njc4OTAx.GabcDE.abcdefghijklmnopqrstuvwxyz1"  # só o formato; entra no setup, não nos testes de segredo


@pytest.fixture(autouse=True)
def sem_rede(monkeypatch):
    def proibido(*args, **kwargs):
        raise AssertionError("tentativa de acesso à rede num teste que não pode usar rede")

    monkeypatch.setattr(socket.socket, "connect", proibido)
    monkeypatch.setattr(socket, "create_connection", proibido)
    monkeypatch.setattr(socket, "getaddrinfo", proibido)


@pytest.fixture
def runtime(monkeypatch, tmp_path) -> Path:
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    monkeypatch.setenv("EMPIREMUSE_HOME", str(home))
    monkeypatch.chdir(cwd)
    return home


@pytest.fixture
def fabrica(runtime, monkeypatch):
    """Devolve ``criar(**ambiente)``: um ``BotPool`` novo com o ambiente dado, ``BotCore`` dublado, sem RPC, sem Java e sem checar nó."""
    criados = []

    def criar(**ambiente):
        padrao = {
            "RUN_LOCAL_LAVALINK": "false",
            "RUN_RPC_SERVER": "false",
            "AUTO_DOWNLOAD_LAVALINK_SERVERLIST": "false",
            "TOKEN": TOKEN_FALSO,
        }
        for chave, valor in {**padrao, **ambiente}.items():
            monkeypatch.setenv(chave, valor)
        pool = BotPool()

        def bot_falso(**kwargs):
            bot = MagicMock(identifier=kwargs["identifier"])
            bot.loop.create_task.side_effect = lambda corotina: corotina.close()
            return bot

        monkeypatch.setattr(client, "BotCore", bot_falso)
        for nome in ("run_bots", "setup_pool_extras", "cache_updater", "connect_rpc_ws"):
            setattr(pool, nome, AsyncMock())
        pool.node_check = MagicMock()
        pool.start_lavalink = AsyncMock()
        criados.append(pool)
        return pool

    yield criar
    for pool in criados:
        encerrar_laco(pool.loop)
    asyncio.set_event_loop(None)


def no_json(senha: str, identificador: str = "NO1") -> str:
    return json.dumps(
        {"identifier": identificador, "host": "127.0.0.1", "port": 2333, "password": senha, "secure": False}
    )


def assert_mascarado(*valores: str) -> None:
    for valor in valores:
        assert redact(f"[{valor}]") == f"[{REDACTED}]", f"o redator ainda deixa passar {valor!r}"


def assert_ainda_nao_mascarado(*valores: str) -> None:
    for valor in valores:
        assert redact(f"[{valor}]") == f"[{valor}]"


class TestSenhasDeNo:
    def test_the_password_inside_a_lavalink_node_json_from_the_environment_is_registered(self, fabrica):
        senha = "senha-no-json-do-ambiente-A1"
        pool = fabrica(LAVALINK_NODE_1=no_json(senha))
        assert_ainda_nao_mascarado(senha)

        pool.setup()

        assert_mascarado(senha)

    def test_the_password_of_a_lavalink_ini_section_is_registered(self, fabrica, runtime):
        senha = "senha-do-lavalink-ini-C3"
        (runtime / "lavalink.ini").write_text(
            f"[SERVIDOR_INI]\nhost = 127.0.0.1\nport = 2333\npassword = {senha}\nsecure = false\n", encoding="utf-8"
        )
        pool = fabrica()
        assert_ainda_nao_mascarado(senha)

        pool.setup()

        assert_mascarado(senha)

    def test_the_password_of_the_downloaded_server_list_is_registered(self, fabrica, monkeypatch):
        senha = "senha-da-lista-baixada-D4"
        pool = fabrica(AUTO_DOWNLOAD_LAVALINK_SERVERLIST="true")
        monkeypatch.setattr(
            client,
            "get_lavalink_servers",
            lambda: {"LISTA1": {"identifier": "LISTA1", "host": "10.0.0.1", "port": "443", "password": senha}},
        )
        assert_ainda_nao_mascarado(senha)

        pool.setup()

        assert_mascarado(senha)


class TestRefreshTokensDoMongo:
    @pytest.mark.asyncio
    async def test_the_refresh_tokens_read_from_mongo_are_registered(self, monkeypatch):
        refresh = "refresh-token-do-mongo-G7"

        class Resposta:
            def __init__(self, status):
                self.status = status

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def json(self):
                return {"plugins": [{"name": "youtube-plugin"}]}

            def raise_for_status(self):
                return None

        class Sessao:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def get(self, *args, **kwargs):
                return Resposta(200)

            async def post(self, *args, **kwargs):
                return Resposta(204)

        monkeypatch.setattr(client.asyncio, "sleep", AsyncMock())
        monkeypatch.setattr(client.aiohttp, "ClientSession", Sessao)
        monkeypatch.setattr(client, "ClientSession", Sessao)
        pool = BotPool.__new__(BotPool)
        pool.get_all_bots = lambda: []
        colecao = SimpleNamespace(find_one=AsyncMock(return_value={"refresh_tokens": {"usuario": refresh}}))
        pool.mongo_database = SimpleNamespace(_connect={"global": {"global": colecao}})
        assert_ainda_nao_mascarado(refresh)

        await pool.check_node(
            {"host": "127.0.0.1", "port": 8090, "password": "senha-local-h8", "identifier": "LOCAL", "retries": 1}
        )

        assert_mascarado(refresh)


class TestLavalinkLocal:
    @pytest.mark.asyncio
    async def test_the_local_lavalink_password_is_registered_once_the_local_node_is_set_up(self, monkeypatch):
        """A senha do nó ``LOCAL`` é fixa no código (``youshallnotpass``); o contrato manda registrá-la junto do Lavalink local.

        Chama as duas pontas que conhecem a senha: a montagem do nó ``LOCAL`` (``node_check``) e a subida do Lavalink local
        (``start_lavalink``, com o ``run_lavalink`` dublado); registrar em qualquer uma delas, ou ao importar o módulo, basta.
        """
        pool = BotPool.__new__(BotPool)
        pool.config = {
            "PREFER_YOUTUBE_NATIVE_PLAYBACK": False,
            "ONLY_USE_NATIVE_SEARCH_PROVIDERS": False,
            "SEARCH_PROVIDERS": "",
            "LAVALINK_FILE_URL": None,
            "LAVALINK_FILE_SHA256": None,
            "YOUTUBE_OAUTH_REFRESH_TOKEN": "",
            "LAVALINK_INITIAL_RAM": 30,
            "LAVALINK_RAM_LIMIT": 100,
            "LAVALINK_CPU_CORES": 1,
            "LAVALINK_ADDITIONAL_SLEEP": 0,
            "LAVALINK_JAVA_PATH": "",
            "LAVALINK_HOME": "",
            "LAVALINK_OFFLINE": True,
        }
        pool.loop = asyncio.get_running_loop()
        pool.check_node = AsyncMock()
        pool.killing_state = False
        pool.lavalink_instance = None
        pool.stop_lavalink = MagicMock()
        monkeypatch.setattr(client, "run_lavalink", MagicMock(return_value=MagicMock()))

        pool.node_check({}, start_local=True)
        await pool.start_lavalink()
        await asyncio.sleep(0)

        assert_mascarado("youshallnotpass")
