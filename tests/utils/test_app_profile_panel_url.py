"""B6: o perfil do bot escolhe o link do modo externo validado, sem cair no genérico quando a configuração falha."""

import pytest

from empiremuse.utils.app_profile import public_panel_url
from tests.api.helpers import NOW, external_config

pytestmark = pytest.mark.critical


def test_url_externa_valida_substitui_painel_generico_mesmo_se_ele_estava_ligado():
    config = {
        "PORT": "6000",
        "PANEL_ENABLED": True,
        "PANEL_BASE_URL": "https://old.example",
        **external_config(EXTERNAL_PANEL_URL="https://muse.empirecommunity.org/panel/"),
    }
    assert public_panel_url(config, now=NOW) == "https://muse.empirecommunity.org/panel"


def test_configuracao_externa_incompleta_nao_publica_link_nem_cai_no_generico():
    assert (
        public_panel_url(
            {
                "PANEL_ENABLED": True,
                "PANEL_BASE_URL": "https://old.example",
                "EXTERNAL_PANEL_URL": "https://muse.empirecommunity.org/panel",
            },
            now=NOW,
        )
        == ""
    )


def test_modo_externo_vazio_preserva_o_link_do_generico_para_hospedagem_propria():
    assert (
        public_panel_url({"PANEL_ENABLED": True, "PANEL_BASE_URL": "https://self.example"}, now=NOW)
        == "https://self.example/painel"
    )
    assert public_panel_url({"PANEL_ENABLED": False, "PANEL_BASE_URL": "https://self.example"}, now=NOW) == ""


@pytest.mark.asyncio
async def test_perfil_publica_o_link_novo_sem_sufixo_painel(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from empiremuse.utils.app_profile import sync_app_profile

    monkeypatch.setattr("empiremuse.utils.app_profile.time.time", lambda: NOW)
    info = SimpleNamespace(description="old", tags=[], edit=AsyncMock())
    config = {
        "PORT": "6000",
        "APP_DESCRIPTION": "Painel: {panel_url}",
        "APP_TAGS": "",
        "DEFAULT_PREFIX": "!",
        "SUPPORT_SERVER": "",
        **external_config(EXTERNAL_PANEL_URL="https://muse.empirecommunity.org/panel"),
    }
    bot = SimpleNamespace(
        appinfo=info, config=config, is_reserve=False, pool=SimpleNamespace(main_bot=None), identifier="test"
    )
    await sync_app_profile(bot, 1)
    assert info.edit.await_args.kwargs["description"] == "Painel: https://muse.empirecommunity.org/panel"
