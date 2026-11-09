"""O log em arquivo do bot (``.logs/disnake-AAAA-MM-DD.log``): diário, em append, sem token de webhook e privado ao dono."""

import logging
import os
import stat
import sys

import pytest

from empiremuse.utils import file_log
from tests.logs.apoio import arquivo_do_log

ID = "1234567890123456"
TOKEN = "AbC-dEf_123.xYz-SECRETO-token_do_webhook"
POSIX = pytest.mark.skipif(sys.platform == "win32", reason="modos de arquivo POSIX")


@pytest.fixture
def handler(tmp_path):
    criado = file_log.build_file_log_handler(tmp_path / ".logs")
    yield criado
    criado.close()


def registro(mensagem, *args, **kwargs):
    return logging.LogRecord("t", logging.DEBUG, __file__, 1, mensagem, args or None, kwargs.get("exc_info"))


class TestRedacaoDeTokenDeWebhook:
    @pytest.mark.parametrize("host", ["discord.com", "canary.discordapp.com"])
    @pytest.mark.parametrize("versao", ["", "/v10"])
    def test_o_token_vira_asteriscos_em_cada_host(self, handler, tmp_path, host, versao):
        url = f"https://{host}/api{versao}/webhooks/{ID}/{TOKEN}"

        handler.handle(registro("POST %s retornou 204", url))
        handler.flush()

        texto = arquivo_do_log(tmp_path / ".logs").read_text(encoding="utf-8")
        assert TOKEN not in texto
        assert f"/webhooks/{ID}/*** retornou 204" in texto

    def test_o_token_no_corpo_ou_no_traceback_tambem_some(self, handler, tmp_path):
        try:
            raise RuntimeError(f"falha em https://discord.com/api/webhooks/{ID}/{TOKEN}")
        except RuntimeError:
            handler.handle(registro("erro", exc_info=sys.exc_info()))
        handler.flush()

        texto = arquivo_do_log(tmp_path / ".logs").read_text(encoding="utf-8")
        assert TOKEN not in texto and "RuntimeError" in texto

    def test_mensagem_sem_webhook_nao_e_alterada(self):
        gravado = registro("Pedido %s feito por %d", "play", 7)

        assert file_log.RedactWebhookTokens().filter(gravado) is True
        assert gravado.getMessage() == "Pedido play feito por 7"

    def test_registro_com_args_quebrados_nao_e_descartado(self):
        gravado = registro("valor %d", "não é número")

        assert file_log.RedactWebhookTokens().filter(gravado) is True
        assert "falha ao redigir" in gravado.getMessage()


@POSIX
class TestPermissoes:
    def test_o_arquivo_do_dia_e_privado_e_a_pasta_tambem(self, handler, tmp_path):
        handler.handle(registro("antes"))

        assert stat.S_IMODE(os.stat(arquivo_do_log(tmp_path / ".logs")).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(tmp_path / ".logs").st_mode) == 0o700
