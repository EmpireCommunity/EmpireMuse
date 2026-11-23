"""A skin efetiva no painel da rádio 24 h (skins 2.3, S7): a da rádio, senão a de todas as rádios do servidor, senão o Retrato
preto (``rules/skins.radio_look``); as ``portrait_*`` saem no Retrato e as ``black``/``white`` no visor. Só fakes; os
desenhos são trocados por gravadores (o PNG de cada um tem os seus próprios testes)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.utils.radio import discord_ports
from empiremuse.utils.radio.discord_ports import DiscordRadioPorts
from tests.cogs.test_radio_personalizada_filtro import STATIONS, FakeFavs, world

pytestmark = pytest.mark.critical


def bot_with(radio_skin):
    if isinstance(radio_skin, Exception):
        get = AsyncMock(side_effect=radio_skin)
    else:
        get = AsyncMock(return_value={"radio_skin": radio_skin})
    return SimpleNamespace(get_color=lambda me: 0, get_global_data=get)


@pytest.fixture
def drawn(monkeypatch):
    """O que cada desenho recebeu: ``("portrait", skin)`` ou ``("visor", skin)``."""
    from empiremuse.utils.radio import visor
    from empiremuse.utils.render.cards import radio_portrait

    seen = []

    def portrait(card, tr):
        seen.append(("portrait", card.skin))
        return b"png"

    def visor_png(snapshot, tr, *, custom=False):
        seen.append(("visor", snapshot.skin))
        return b"png"

    monkeypatch.setattr(radio_portrait, "render_portrait", portrait)
    monkeypatch.setattr(visor, "render_snapshot", visor_png)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("own", "guild", "expected"),
    [
        ("white", "portrait_white", ("visor", "white")),  # a da rádio vence a de todas as rádios
        (None, "portrait_white", ("portrait", "portrait_white")),
        (None, None, ("portrait", "portrait_black")),  # nunca escolhida: o padrão
        (None, RuntimeError("banco fora"), ("portrait", "portrait_black")),
        ("portrait_white", RuntimeError("banco fora"), ("portrait", "portrait_white")),
        (None, "text", ("visor", "black")),  # a de texto ainda sai no visor da skin gravada
    ],
)
async def test_the_radio_panel_draws_the_effective_skin(own, guild, expected, drawn):
    from empiremuse.i18n import get_i18n

    radio, cog, *_ = world(active=1)
    radio.look = own
    cog.hub.custom_favorites = FakeFavs()
    cog.hub.manager = SimpleNamespace(runner=lambda _id: None)
    ports = SimpleNamespace(
        pool=SimpleNamespace(radio=cog.hub), station=STATIONS[0], current=None, no_tracks=False, wall=lambda: 1.0
    )
    tr = await get_i18n().for_guild(None)
    payload = await DiscordRadioPorts._custom_visor_payload(ports, bot_with(guild), SimpleNamespace(me=None), radio, tr)
    assert drawn == [expected] and payload["file"].filename.endswith(".png")


@pytest.mark.asyncio
async def test_the_live_radio_panel_reads_the_skin_of_all_radios_of_its_server(monkeypatch):
    from empiremuse.utils.radio import panel, snapshot

    live = AsyncMock(return_value="retrato")
    monkeypatch.setattr(snapshot, "live_snapshot", live)
    monkeypatch.setattr(panel, "filter_select_options", lambda *a, **k: [])
    monkeypatch.setattr(discord_ports, "look_payload", AsyncMock(return_value={}))
    hub = SimpleNamespace(
        manager=SimpleNamespace(runner=lambda _id: None),
        favorites=SimpleNamespace(list=AsyncMock(return_value=[])),
        locations=None,
    )
    ports = SimpleNamespace(pool=SimpleNamespace(radio=hub), _logo=AsyncMock(return_value=None))
    radio = SimpleNamespace(id="r", guild_id=1, filter=None)
    bot = bot_with("portrait_white")
    await DiscordRadioPorts._visor_payload(ports, bot, SimpleNamespace(me=None), radio, SimpleNamespace(locale="pt-BR"))
    assert live.await_args.kwargs["guild_radio_skin"] == "portrait_white"
    assert bot.get_global_data.await_args.args == (1,)
