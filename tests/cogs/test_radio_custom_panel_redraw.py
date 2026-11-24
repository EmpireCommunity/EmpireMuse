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
    ports._panel_edited_at = None
    clock = [0.0]
    ports.now = lambda: clock[0]
    waits = []

    async def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds

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


@pytest.mark.asyncio
async def test_redraws_asked_inside_the_window_become_one_edit():
    """Cliques seguidos no painel da personalizada: no máximo uma edição por ``PANEL_EDIT_INTERVAL_S``, como a ao vivo."""
    ports, message, waits = ports_with([{"file": "a"}, {"file": "b"}, {"file": "c"}])
    ports.refresh_panel()
    await ports._panel_task
    ports.now = lambda: 1.0  # um segundo depois da primeira edição
    for _ in range(3):
        ports.refresh_panel()
    await ports._panel_task
    assert message.edit.await_count == 2 and waits == [custom_ports.PANEL_EDIT_INTERVAL_S - 1.0]


@pytest.mark.asyncio
async def test_stopping_the_radio_cancels_the_pending_panel_retry():
    """A nova tentativa do painel (vaga de render ocupada) não edita depois que a rádio parou de verdade."""
    gate = asyncio.Event()
    ports, message, _ = ports_with([RenderBusy(), {"file": "visor"}])

    async def held(seconds):
        await gate.wait()

    ports.panel_sleep = held
    ports.refresh_panel()
    task = ports._panel_task
    await asyncio.sleep(0)  # a primeira tentativa achou a vaga ocupada e espera
    ports.radio_stopped()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert message.edit.await_count == 0
