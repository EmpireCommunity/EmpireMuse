"""A skin efetiva no painel da rádio 24 h (skins 2.3, S7): a da rádio, senão a de todas as rádios do servidor, senão o Retrato
preto (``rules/skins.radio_look``); as ``portrait_*`` saem no Retrato, as ``black``/``white`` no visor e a ``text`` em texto, sem desenho. Só fakes; os
desenhos são trocados por gravadores (o PNG de cada um tem os seus próprios testes)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

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
        (None, "portrait_white", ("visor", "white")),
        (None, None, None),  # nunca escolhida: o padrão é a Rádio Texto, que sai sem desenho
        (None, RuntimeError("banco fora"), None),  # banco fora: o mesmo padrão
        ("portrait_white", RuntimeError("banco fora"), ("visor", "white")),
    ],
)
async def test_the_radio_panel_draws_the_effective_skin(own, guild, expected, drawn):
    from empiremuse.i18n import get_i18n

    radio, cog, *_ = world(active=1)
    radio.look = own
    cog.hub.custom_favorites = FakeFavs()
    cog.hub.manager = SimpleNamespace(runner=lambda _id: None)
    ports = SimpleNamespace(
        pool=SimpleNamespace(radio=cog.hub),
        station=STATIONS[0],
        current=None,
        no_tracks=False,
        wall=lambda: 1.0,
    )
    tr = await get_i18n().for_guild(None)
    payload = await DiscordRadioPorts._custom_visor_payload(ports, bot_with(guild), SimpleNamespace(me=None), radio, tr)
    if expected is None:
        assert drawn == [] and payload["embeds"]  # Rádio Texto: embed e logo, nenhum desenho
    else:
        assert drawn == [expected] and payload["file"].filename.endswith(".png")


@pytest.mark.asyncio
async def test_the_text_skin_sends_the_panel_without_drawing(drawn):
    from empiremuse.i18n import get_i18n

    radio, cog, *_ = world(active=1)
    radio.look, radio.channel_id = "text", 1
    cog.hub.custom_favorites = FakeFavs()
    cog.hub.manager = SimpleNamespace(runner=lambda _id: None)
    ports = SimpleNamespace(
        pool=SimpleNamespace(radio=cog.hub),
        station=STATIONS[0],
        current=None,
        no_tracks=False,
        wall=lambda: 1.0,
    )
    tr = await get_i18n().for_guild(None)
    guild = SimpleNamespace(me=None, get_channel=lambda _id: SimpleNamespace(name="Lounge"))
    payload = await DiscordRadioPorts._custom_visor_payload(ports, bot_with(None), guild, radio, tr)
    assert drawn == [] and payload["embeds"] and "file" in payload  # o logo substitui o visor.png de antes
    assert "Lounge" in payload["embeds"][0].author.name  # o nome da call chega ao autor da Rádio Texto


def test_every_radio_look_has_a_drawing():
    """Uma skin nova da rádio no registro sem desenho próprio sairia calada no visor da skin gravada."""
    from empiremuse.rules.skins import LEGACY_RADIO_SKINS, RADIO_LOOKS
    from empiremuse.utils.radio.discord_ports import PORTRAIT_LOOKS
    from empiremuse.utils.radio.panel import TEXT_LOOKS

    assert set(RADIO_LOOKS) == PORTRAIT_LOOKS | set(LEGACY_RADIO_SKINS) | TEXT_LOOKS
