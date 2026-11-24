"""A leitura da skin: o helper único ``load_custom_skins`` e o ``Music.create_player``.

Só o dicionário do documento chega ao player, depois de validado por ``validate_skin``. O texto do formato antigo
(``base64`` de ``pickle``) nunca é aberto: fica como está no documento, nada é gravado, a skin padrão assume e sai um aviso só
com o ``guild_id``.
"""

import copy
import logging
import pickle

import pytest

from tests.support.formatos_reais import SERVIDOR
from tests.support.avisos_no_log import avisos, log_sem_o_conteudo
from tests.support.serializacao_nucleo import SEGREDO
from tests.support.skins import carregar_helper
from tests.persistence.test_formato_skin import (
    BotDeSkins,
    chamar_create_player,
    dados_globais,
    skin_realista,
    texto_legado,
)
from tests.persistence.test_formato_skin import editor_sem_tela as _editor_sem_tela

editor_sem_tela = _editor_sem_tela  # fixture do editor de skin, de persistence/test_formato_skin.py


def documento(normais=None, estaticas=None, **extra) -> dict:
    """O documento global do servidor com tudo que os pontos de leitura usam."""
    base = {
        **dados_globais(normais, estaticas),
        "prefix": "!",
        "global_skin": False,
        "listen_along_invites": {},
        "voice_channel_status": "",
        "player_skin": "",
        "player_skin_static": "",
    }
    return base | extra


@pytest.fixture(autouse=True)
def _ambiente(tmp_path, monkeypatch):
    monkeypatch.setenv("EMPIREMUSE_HOME", str(tmp_path))


@pytest.fixture
def helper():
    return carregar_helper()


async def rodar(helper, dados, jogadores=()):
    bot = BotDeSkins(dados, jogadores)
    await helper(bot, SERVIDOR, dados)
    return bot


class TestHelper:
    @pytest.mark.asyncio
    async def test_dicionario_valido_chega_inteiro_e_nada_e_gravado(self, helper):
        dados = documento(
            normais={"A": skin_realista(), "Vazia": {}, "Nenhuma": None}, estaticas={"F": skin_realista(content="f")}
        )
        antes = copy.deepcopy(dados)
        bot = BotDeSkins(dados)
        skins = await helper(bot, SERVIDOR, dados)
        assert skins.normal == {"A": skin_realista()} and skins.static == {"F": skin_realista(content="f")}
        assert skins.rejected == 0 and bot.escritas == [] and dados == antes

    @pytest.mark.asyncio
    async def test_texto_do_formato_antigo_fica_no_documento_e_nunca_e_aberto(
        self, helper, monkeypatch, caplog, capsys
    ):
        def nunca(*args, **kwargs):
            raise AssertionError("o texto antigo foi aberto")

        monkeypatch.setattr(pickle, "loads", nunca)
        monkeypatch.setattr(pickle, "load", nunca)
        monkeypatch.setattr(pickle, "Unpickler", nunca)
        antigo = texto_legado(skin_realista(content=SEGREDO))
        dados = documento(normais={"Velha": antigo, "Boa": skin_realista()}, estaticas={"FixaVelha": antigo})
        bot = BotDeSkins(dados)
        with caplog.at_level(logging.DEBUG):
            skins = await helper(bot, SERVIDOR, dados)
        assert skins.normal == {"Boa": skin_realista()} and skins.static == {} and skins.rejected == 2
        assert dados["custom_skins"]["Velha"] == antigo and dados["custom_skins_static"]["FixaVelha"] == antigo
        assert bot.escritas == []
        assert any(str(SERVIDOR) in aviso for aviso in avisos(caplog)) and log_sem_o_conteudo(caplog, capsys)


class TestCreatePlayer:
    @pytest.mark.asyncio
    async def test_o_player_recebe_so_dicionarios_e_o_texto_antigo_e_ignorado(self):
        dados = documento(
            normais={"N": skin_realista(content="n"), "Velha": texto_legado(skin_realista())}, estaticas={"F": ""}
        )
        recebidos = await chamar_create_player(dados, [])
        assert recebidos["custom_skin_data"] == {"N": skin_realista(content="n")}
        assert recebidos["custom_skin_static_data"] == {}  # skin vazia vale como sem skin: o player nem a recebe


@pytest.mark.asyncio
@pytest.mark.regression
@pytest.mark.parametrize(
    ("marks", "skin"), [({"skin_chosen": True, "static_skin_chosen": True}, "default"), ({}, None)]
)
async def test_create_player_keeps_the_classic_default_only_when_it_was_chosen_in_2_3(marks, skin):
    recebidos = await chamar_create_player(documento(), [], controller=marks)

    assert (
        recebidos["skin"] == skin and recebidos["skin_static"] == skin
    )  # ``None`` = o padrão novo, aplicado pelo player
