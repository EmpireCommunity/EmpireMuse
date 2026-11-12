"""O perfil e a presença usam somente a URL que run_app publicou depois do bind."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.utils.app_profile import public_panel_url, sync_app_profile

pytestmark = pytest.mark.critical


@pytest.mark.parametrize("url", ["", "https://muse.empirecommunity.org/panel", "https://self.example/painel"])
def test_url_usa_somente_estado_runtime(url):
    assert public_panel_url(SimpleNamespace(public_panel_url=url)) == url


def test_sem_servidor_nao_anuncia_url_nem_le_configuracao():
    assert public_panel_url(SimpleNamespace()) == ""


@pytest.mark.asyncio
async def test_perfil_publica_o_link_novo_sem_sufixo_painel():
    info = SimpleNamespace(description="old", tags=[], edit=AsyncMock())
    config = {
        "APP_DESCRIPTION": "Painel: {panel_url}",
        "APP_TAGS": "",
        "DEFAULT_PREFIX": "!",
        "SUPPORT_SERVER": "",
    }
    bot = SimpleNamespace(
        appinfo=info,
        config=config,
        is_reserve=False,
        pool=SimpleNamespace(main_bot=None, public_panel_url="https://muse.empirecommunity.org/panel"),
        identifier="test",
    )
    await sync_app_profile(bot, 1)
    assert info.edit.await_args.kwargs["description"] == "Painel: https://muse.empirecommunity.org/panel"


@pytest.mark.parametrize("url", ["", "https://muse.empirecommunity.org/panel", "https://self.example/painel"])
@pytest.mark.parametrize("text", ["⚙️ Painel: muse.empirecommunity.org/panel", "⚙️ Painel: {panel_url}"])
def test_presenca_compartilha_a_url_runtime_e_omite_painel_indisponivel(url, text):
    from empiremuse.cogs.misc import Misc

    from datetime import datetime, timezone

    bot = SimpleNamespace(
        pool=SimpleNamespace(public_panel_url=url),
        users=[],
        music=SimpleNamespace(players={}),
        guilds=[],
        uptime=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert Misc.placeholders(SimpleNamespace(bot=bot), text) == (f"⚙️ Painel: {url}" if url else "")
