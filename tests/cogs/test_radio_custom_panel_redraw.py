"""Rádio personalizada: o painel que saiu sem o visor (vaga de render ocupada) ganha a imagem sem esperar a próxima faixa."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.utils.radio import custom_ports
from empiremuse.utils.radio.custom_ports import CustomRadioPorts
from empiremuse.utils.render.slot import RenderBusy

pytestmark = pytest.mark.regression


def ports_with(payloads):
    """Portas sem o mundo real: cada edição pede um payload da lista (``RenderBusy`` = vaga ocupada)."""
    ports = object.__new__(CustomRadioPorts)
    message = SimpleNamespace(edit=AsyncMock())
    channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
    ports.radio = SimpleNamespace(id="r")
    ports.pool = SimpleNamespace(
        radio=SimpleNamespace(store=SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(panel_message_id=7))))
    )
    ports._bot = lambda: object()
    ports._guild = lambda: object()
    ports._channel = lambda: channel
    ports._panel_task = None
    ports._panel_dirty = False
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    async def payload(bot, guild, current):
        item = payloads.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    ports.panel_sleep = sleep
    ports._panel_payload = payload
    return ports, message, waits


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payloads", "edits", "waits"),
    [
        ([RenderBusy(), {"file": "visor"}], 1, 1),  # a vaga liberou: o visor chega na nova tentativa
        ([RenderBusy()] * (custom_ports.PANEL_BUSY_RETRIES + 1), 0, custom_ports.PANEL_BUSY_RETRIES),  # e desiste
    ],
)
async def test_a_busy_render_slot_retries_the_custom_panel_later(payloads, edits, waits):
    ports, message, slept = ports_with(list(payloads))
    ports.refresh_panel()
    await ports._panel_task
    assert message.edit.await_count == edits and slept == [custom_ports.PANEL_BUSY_RETRY_S] * waits


@pytest.mark.asyncio
async def test_a_redraw_asked_during_an_edit_is_not_dropped():
    gate = asyncio.Event()
    ports, message, _ = ports_with([{"file": "a"}, {"file": "b"}])

    async def slow_edit(**kwargs):
        await gate.wait()

    message.edit.side_effect = slow_edit
    ports.refresh_panel()
    await asyncio.sleep(0)
    ports.refresh_panel()  # chega com a primeira edição em curso
    gate.set()
    await ports._panel_task
    assert message.edit.await_count == 2


@pytest.mark.asyncio
@pytest.mark.regression
async def test_the_public_panel_refresh_redraws_the_custom_panel():
    """Quem está fora das portas (o menu do painel, o ``/skins``) acha o redesenho pelo nome público."""
    ports, message, _ = ports_with([{"file": "visor"}])
    getattr(ports, "refresh_panel")()
    await ports._panel_task
    assert message.edit.await_count == 1
