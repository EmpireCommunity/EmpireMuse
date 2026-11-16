"""Estrela de admin e "info" do painel ao vivo (T-473 J3): diretório fora é "indisponível, tente de novo", nunca "estação não encontrada"."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.cogs import radio as radio_cog
from empiremuse.i18n import get_i18n
from empiremuse.utils.radio.directory import DirectoryError
from tests.cogs.test_radio_dono import ADMIN, GUILD, build

STATION_UUID = "96062a7b-0601-11e8-ae97-52543be04c81"


@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["favorite_server", "info"])
async def test_a_directory_outage_reads_as_unavailable_not_as_not_found(monkeypatch, action):
    w = build()
    monkeypatch.setattr(radio_cog, "can_manage_guild", AsyncMock(return_value=True))
    monkeypatch.setattr(w.cog, "_too_soon", lambda *args: False)
    w.hub.directory.by_uuid = AsyncMock(return_value=None)  # a leitura tolerante engole a falha
    w.hub.directory.by_uuid_strict = AsyncMock(side_effect=DirectoryError("network"))
    radio = SimpleNamespace(id="r1", guild_id=GUILD, source={"station": STATION_UUID})
    tr = get_i18n().for_locale("pt-BR")

    if action == "favorite_server":
        said = await w.cog._favorite_server(SimpleNamespace(author=SimpleNamespace(id=ADMIN)), tr, radio)
    else:
        said = await w.cog._info(tr, radio)

    assert said == tr("radio.owner.unavailable")
