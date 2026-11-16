"""Testes da Task C-22: roteamento de caminhos por empiremuse.paths e subprocess Popen cwd."""

from __future__ import annotations

import json
import base64
import zlib

import pytest

from empiremuse import paths
from empiremuse.utils import branding
from empiremuse.utils.db import LocalDatabase
from empiremuse.utils.music import lavalink_server_list, local_lavalink as ll
from tests.support.formatos_reais import BOT_ID, MongoEmMemoria, cog_de_sessoes
from tests.persistence.test_formato_caches import gravar_o_cache_de_playlist, pool_vazio as pool_de_caches
from tests.support.formato_fila_salva import salvar as salvar_fila, tres_faixas
from tests.support.formato_sessao import jogador_completo
from tests.persistence.test_formato_skin import editor_sem_tela as _editor_sem_tela
from tests.persistence.test_formato_skin import BotaoDeModo, InteracaoDoEditor, dados_globais, skin_realista


editor_sem_tela = _editor_sem_tela  # fixture do editor de skin, de persistence/test_formato_skin.py


class TestRoteamentoDeCaminhosDeDados:
    """Verifica roteamento de data/, assets/ e local_database."""

    def test_data_e_assets_roteados_por_project_dir(self):
        assert lavalink_server_list.BUNDLED_SERVER_LIST_PATH == paths.PROJECT_DIR / "data" / "lavalink_servers.ini"
        assert branding._ASSETS_DIR == paths.PROJECT_DIR / "assets"
