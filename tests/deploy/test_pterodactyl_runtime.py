"""D-23 — o bot sob o Pterodactyl: cwd no EMPIREMUSE_HOME e porta vinda de SERVER_PORT.

O Wings cria o container sem ``Cmd`` e sem ``WorkingDir`` (o cwd fica no ``/data`` da imagem, que o usuário do nó não
grava) e exporta ``SERVER_PORT``/``SERVER_IP``. O valor das variáveis do egg vai literal, sem interpolação.
"""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from empiremuse import config_loader


@pytest.fixture
def sem_porta(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("SERVER_PORT", raising=False)


@pytest.fixture
def cwd_restaurado(monkeypatch):
    """Garante que o chdir do ponto de entrada não vaza para os outros testes."""
    monkeypatch.chdir(os.getcwd())


# --------------------------------------------------------------------------------------------------------------
# 1. Diretório de trabalho
# --------------------------------------------------------------------------------------------------------------


class TestCwdNoRuntimeDir:
    @pytest.fixture
    def main_module(self, monkeypatch):
        import empiremuse.__main__ as main_module

        self.cwd_na_construcao = []

        class PoolFalso:
            def __init__(inner):
                self.cwd_na_construcao.append(os.getcwd())
                inner.exit_code = 0

            def setup(inner):
                pass

        monkeypatch.setattr(main_module, "BotPool", PoolFalso)
        return main_module

    @pytest.mark.smoke
    def test_empiremuse_home_vazio_e_ignorado(self, monkeypatch, tmp_path, main_module, cwd_restaurado):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("EMPIREMUSE_HOME", "")

        with pytest.raises(SystemExit):
            main_module.main()

        assert os.path.samefile(os.getcwd(), tmp_path)

    def test_arquivo_no_lugar_da_pasta_tambem_e_erro_claro(self, monkeypatch, tmp_path, main_module, cwd_restaurado):
        arquivo = tmp_path / "arquivo.txt"
        arquivo.write_text("x")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("EMPIREMUSE_HOME", str(arquivo))

        with pytest.raises(SystemExit) as exc_info:
            main_module.main()

        assert "EMPIREMUSE_HOME" in str(exc_info.value.code)
        assert self.cwd_na_construcao == []


# --------------------------------------------------------------------------------------------------------------
# 2. Porta
# --------------------------------------------------------------------------------------------------------------


class TestResolvePort:
    def test_nenhuma_usa_6000(self, sem_porta):
        assert config_loader.resolve_port(None) == 6000

    @pytest.mark.parametrize("valor", ["65536", "-1"])
    def test_porta_fora_da_faixa_e_erro(self, monkeypatch, sem_porta, valor):
        monkeypatch.setenv("PORT", valor)
        with pytest.raises(ValueError, match="PORT"):
            config_loader.resolve_port(None)


class TestLoadConfigRpcServer:
    @pytest.fixture(autouse=True)
    def runtime_vazio(self, monkeypatch, tmp_path, sem_porta):
        monkeypatch.setenv("EMPIREMUSE_HOME", str(tmp_path))

    @pytest.mark.smoke
    def test_rpc_server_port_vence_server_port(self, monkeypatch):
        monkeypatch.setenv("PORT", "8080")
        monkeypatch.setenv("SERVER_PORT", "25565")
        assert config_loader.load_config()["RPC_SERVER"] == "ws://localhost:8080/ws"


class TestServidorHttp:
    @pytest.fixture
    def listen_chamado(self, monkeypatch):
        from empiremuse import web_app

        chamadas = []

        class ServidorFalso:
            def __init__(self, *args, **kwargs):
                pass

            def listen(self, porta, address=None, **kwargs):
                chamadas.append(porta)

        monkeypatch.setattr(web_app.tornado.httpserver, "HTTPServer", ServidorFalso)
        monkeypatch.setattr(web_app, "_build_panel", lambda pool, config: (None, False, ()))
        monkeypatch.setattr(web_app, "make_router", lambda app, panel: app)
        return web_app, chamadas

    def test_porta_invalida_e_erro_claro_e_nao_escuta(self, monkeypatch, sem_porta, listen_chamado):
        web_app, chamadas = listen_chamado
        monkeypatch.setenv("SERVER_PORT", "{{SERVER_PORT}}")
        with pytest.raises(ValueError, match="SERVER_PORT"):
            web_app.run_app(MagicMock(), config={"PORT": None})
        assert chamadas == []


class TestConexaoRpcDesligada:
    """Com o RPC desligado e o endereço padrão, o bot não abre o cliente WebSocket, qualquer que seja a origem da porta."""

    @staticmethod
    def _conectar(monkeypatch, **ambiente):
        from empiremuse.utils.client import BotPool

        for nome in ("PORT", "SERVER_PORT"):
            monkeypatch.delenv(nome, raising=False)
        for nome, valor in ambiente.items():
            monkeypatch.setenv(nome, valor)

        async def ws_loop():
            ws_loop.chamado = True

        ws_loop.chamado = False
        monkeypatch.setenv("RUN_RPC_SERVER", "false")
        config = config_loader.load_config()
        pool = SimpleNamespace(config=config, ws_client=SimpleNamespace(ws_loop=ws_loop))
        asyncio.run(BotPool.connect_rpc_ws(pool))
        return ws_loop.chamado

    @pytest.fixture(autouse=True)
    def runtime_vazio(self, monkeypatch, tmp_path):
        monkeypatch.setenv("EMPIREMUSE_HOME", str(tmp_path))

    def test_com_port_nao_abre_o_cliente(self, monkeypatch):
        assert self._conectar(monkeypatch, PORT="8080") is False
