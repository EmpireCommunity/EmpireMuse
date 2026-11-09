"""F3-2 — testes RED de config, pool e itens correlatos (KI-06, KI-07, KI-08, KI-10, KI-11, KI-12, KI-13, KI-31, caches).

Sem rede, sem Discord, sem Java e sem o .env real (o conftest já neutraliza o ``dotenv_values`` real). Tudo roda em ``tmp_path``
com ``EMPIREMUSE_HOME`` isolado e cwd diferente dele. Testes marcados como GUARDA descrevem comportamento que já funciona hoje e
não pode regredir; os demais falham hoje pelo motivo descrito no nome/mensagem.

Decisões (MAESTRO): KI-06 booleanos aceitam qualquer caixa, inválido continua dando erro citando a chave; KI-07 retries 1..4
são respeitados, 0 = sem limite, negativo = erro claro; KI-08 falha ao ler o .env vira logger.warning (tipo do erro e caminho,
sem conteúdo) e o bot segue; KI-10 falha de gravação levanta e não toca o cache; KI-11 guild novo é acrescentado ao dict;
KI-12 reset_useragent devolve o user-agent; KI-13 com KILL_ON_429=false propaga o erro original; KI-31 sem utcnow() e com
datetimes cientes de fuso; caches relativos (.ytdl_cache, .lastfm_cache, GIT_DIR) ficam sob paths.RUNTIME_DIR.
"""

import asyncio
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from empiremuse import config_loader
from empiremuse.utils import client
from empiremuse.utils.client import BotPool
from empiremuse.utils.db import DBModel, LocalDatabase
from tests.support.laco_do_pool import encerrar_laco

pytestmark = pytest.mark.regression

TOKEN_FALSO = "MTIzNDU2Nzg5MDEyMzQ1Njc4OTAx.GabcDE.abcdefghijklmnopqrstuvwxyz1"  # só o formato; não é um token real


@pytest.fixture(autouse=True)
def sem_rede(monkeypatch):
    """Garante "sem rede": qualquer tentativa de abrir conexão (download de jar/JDK, API, DNS) quebra o teste na hora."""

    def proibido(*args, **kwargs):
        raise AssertionError("tentativa de acesso à rede num teste que não pode usar rede")

    monkeypatch.setattr(socket.socket, "connect", proibido)
    monkeypatch.setattr(socket, "create_connection", proibido)
    monkeypatch.setattr(socket, "getaddrinfo", proibido)


@pytest.fixture
def runtime(monkeypatch, tmp_path) -> Path:
    """RUNTIME_DIR isolado (EMPIREMUSE_HOME) e cwd em outra pasta, para pegar qualquer caminho relativo."""
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    monkeypatch.setenv("EMPIREMUSE_HOME", str(home))
    monkeypatch.chdir(cwd)
    return home


def carregar(monkeypatch, **ambiente) -> dict:
    for chave, valor in ambiente.items():
        monkeypatch.setenv(chave, valor)
    return config_loader.load_config()


# ============================================================================ KI-06 — booleanos em qualquer caixa


class TestKI06Booleanos:
    def test_false_em_maiusculas_desliga_uma_flag_que_vem_ligada_por_padrao(self, runtime, monkeypatch):
        assert config_loader.DEFAULT_CONFIG["AUTO_SYNC_COMMANDS"] is True
        assert carregar(monkeypatch, AUTO_SYNC_COMMANDS="FALSE")["AUTO_SYNC_COMMANDS"] is False


# ============================================================================ KI-07 — LAVALINK_RECONNECT_RETRIES


class TestKI07TentativasDeReconexao:
    @pytest.mark.parametrize("texto", ["-1", "-30"])
    def test_negativo_e_erro_claro_citando_a_chave(self, runtime, monkeypatch, texto):
        with pytest.raises(Exception, match="LAVALINK_RECONNECT_RETRIES"):
            carregar(monkeypatch, LAVALINK_RECONNECT_RETRIES=texto)


# ============================================================================ KI-08 — falha ao ler o .env não é silenciosa


class TestKI08FalhaAoLerODotenv:
    def test_o_bot_segue_com_o_ambiente_apos_a_falha(self, runtime, monkeypatch):
        def quebrar(*args, **kwargs):
            raise OSError("disco com defeito")

        monkeypatch.setattr(config_loader, "dotenv_values", quebrar)
        monkeypatch.setenv("DEFAULT_PREFIX", "??")

        assert config_loader.load_config()["DEFAULT_PREFIX"] == "??"


# ============================================================================ KI-10 — LocalDatabase.update_data


class TestKI10UpdateDataComFalhaDeGravacao:
    @pytest.fixture
    def banco(self, tmp_path):
        return LocalDatabase(dir_=str(tmp_path / "banco"))

    @pytest.mark.asyncio
    async def test_falha_do_insert_one_levanta_e_nao_atualiza_o_cache(self, banco, monkeypatch):
        from tinymongo import TinyMongoCollection

        monkeypatch.setattr(TinyMongoCollection, "update_one", lambda self, *a, **k: SimpleNamespace(raw_result=False))

        def quebrar(self, *args, **kwargs):
            raise OSError("sem permissão (simulado)")

        monkeypatch.setattr(TinyMongoCollection, "insert_one", quebrar)

        with pytest.raises(OSError, match="sem permissão"):
            await banco.update_data("2", {"ver": 1.0}, db_name=DBModel.guilds, collection="c")

        assert "c:guilds:2" not in banco.cache

    @pytest.mark.asyncio
    async def test_a_falha_nao_substitui_o_dado_que_ja_estava_no_cache(self, banco, monkeypatch):
        from tinymongo import TinyMongoCollection

        await banco.update_data("3", {"ver": 1.0, "valor": "antigo"}, db_name=DBModel.guilds, collection="c")

        def quebrar(self, *args, **kwargs):
            raise OSError("x")

        monkeypatch.setattr(TinyMongoCollection, "update_one", quebrar)

        with pytest.raises(OSError):
            await banco.update_data("3", {"ver": 1.0, "valor": "novo"}, db_name=DBModel.guilds, collection="c")

        assert banco.cache["c:guilds:3"]["valor"] == "antigo"


# ============================================================================ KI-13 — rate limit com KILL_ON_429=false


def _erro_http(status: int, texto: str = "") -> disnake.HTTPException:
    return disnake.HTTPException(SimpleNamespace(status=status, reason="Too Many Requests"), texto)


class TestKI13RateLimitSemMatarOProcesso:
    @pytest.fixture
    def pool(self, runtime, monkeypatch):
        pool = BotPool()
        pool.config["KILL_ON_429"] = False
        pool.shutdown = AsyncMock()
        monkeypatch.setattr(client.asyncio, "sleep", AsyncMock())  # sem esperar os 10 s de verdade
        return pool

    @staticmethod
    def _bot(erro: BaseException):
        return SimpleNamespace(http=SimpleNamespace(token="t"), start=AsyncMock(side_effect=erro), identifier="bot1")

    @pytest.mark.asyncio
    async def test_status_429_propaga_o_erro_original_e_nao_typeerror(self, pool):
        erro = _erro_http(429, "rate limited")

        with pytest.raises(disnake.HTTPException) as capturado:
            await pool.start_bot(self._bot(erro))

        assert capturado.value is erro, "KI-13: o erro original deveria ser propagado"


# ============================================================================ Inicialização com bots falsos


@pytest.fixture
def pool_com_bots_falsos(runtime, monkeypatch):
    """Roda o BotPool.setup REAL com BotCore dublado e o loop principal neutralizado."""
    monkeypatch.setenv("RUN_LOCAL_LAVALINK", "false")  # sem Java: o setup não pode subir o Lavalink local
    monkeypatch.setenv("RUN_RPC_SERVER", "false")  # o padrão (True) sobe um servidor tornado na porta do painel
    monkeypatch.setenv("TOKEN", TOKEN_FALSO)  # o setup só reconhece valores com FORMATO de token do Discord
    pool = BotPool()

    def bot_falso(**kwargs):
        bot = MagicMock(identifier=kwargs["identifier"])
        bot.loop.create_task.side_effect = lambda corotina: corotina.close()  # sem "coroutine never awaited"
        return bot

    monkeypatch.setattr(client, "BotCore", bot_falso)
    for nome in ("run_bots", "setup_pool_extras", "cache_updater", "connect_rpc_ws"):
        setattr(pool, nome, AsyncMock())
    pool.node_check = MagicMock()
    pool.start_lavalink = AsyncMock()  # nada de subir Java nem baixar jar/JDK
    yield pool
    encerrar_laco(pool.loop)  # cancela o que o setup deixou (tarefas dubladas e a da versão nova) e fecha o laço
    asyncio.set_event_loop(None)
