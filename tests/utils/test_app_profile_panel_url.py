"""O perfil e a presença usam somente a URL que run_app publicou depois do bind."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.rules.app_profile import DEFAULT_PANEL_PRESENCE, LEGACY_PANEL_PRESENCES
from empiremuse.utils.app_profile import public_panel_url, sync_app_profile

pytestmark = pytest.mark.critical


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
@pytest.mark.parametrize("text", [*LEGACY_PANEL_PRESENCES, DEFAULT_PANEL_PRESENCE])
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


@pytest.mark.regression
@pytest.mark.parametrize(
    "text",
    [
        "⚙️ Painel: https://muse.empirecommunity.org/panel",
        "Visite muse.empirecommunity.org/panel/docs",
        "⚙️ Painel: https://muse.empirecommunity.org/painel",
        "Visite muse.empirecommunity.org/painel/docs",
    ],
)
@pytest.mark.parametrize("url", ["", "https://other.example/panel"])
def test_presenca_personalizada_preserva_url_literal(text, url):
    from datetime import datetime, timezone
    from empiremuse.cogs.misc import Misc

    bot = SimpleNamespace(
        pool=SimpleNamespace(public_panel_url=url),
        users=[],
        music=SimpleNamespace(players={}),
        guilds=[],
        uptime=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert Misc.placeholders(SimpleNamespace(bot=bot), text) == text


@pytest.mark.regression
@pytest.mark.parametrize(
    ("prefix", "prefix_commands", "shown"),
    [
        ("!!", True, "🎵 /play ou !!play | ajuda: !!help"),  # o prefixo de quem hospeda, não o "!" fixo
        ("?", True, "🎵 /play ou ?play | ajuda: ?help"),
        ("!!", False, ""),  # só comandos de barra: a presença que ensina o prefixo não aparece
        ("", True, ""),
    ],
)
@pytest.mark.parametrize("text", ["default", "legacy"])
def test_presenca_padrao_segue_o_prefixo_configurado(text, prefix, prefix_commands, shown):
    from empiremuse.cogs.misc import Misc
    from empiremuse.rules.app_profile import DEFAULT_COMMANDS_PRESENCE, LEGACY_COMMANDS_PRESENCES

    from datetime import datetime, timezone

    bot = SimpleNamespace(
        default_prefix=prefix,
        config={"INTERACTION_COMMAND_ONLY": not prefix_commands},
        pool=SimpleNamespace(public_panel_url=""),
        users=[],
        music=SimpleNamespace(players={}),
        guilds=[],
        uptime=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    presence = DEFAULT_COMMANDS_PRESENCE if text == "default" else LEGACY_COMMANDS_PRESENCES[0]

    assert Misc.placeholders(SimpleNamespace(bot=bot), presence) == shown
