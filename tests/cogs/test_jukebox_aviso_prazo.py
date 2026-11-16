"""O aviso de permissões do ``/jukebox setup`` tem prazo próprio de 120 s: some sozinho e a limpeza do comando não o apaga antes."""

from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from empiremuse.music_cog import jukebox_messages
from empiremuse.music_cog.jukebox_messages import cleanup_jukebox_command_messages, retain_jukebox_permissions_warning
from empiremuse.utils.others import CustomContext

START = 1000.0
TTL = 120


class FakeLoop:
    """Relógio e agenda do laço sob controle do teste: ``advance`` roda só o que venceu."""

    def __init__(self) -> None:
        self.now = START
        self.scheduled: list[tuple[float, Callable[..., object], tuple]] = []

    def time(self) -> float:
        return self.now

    def call_later(self, delay, callback, *args):
        self.scheduled.append((self.now + delay, callback, args))

    def advance(self, seconds: float) -> None:
        self.now += seconds
        due = [entry for entry in self.scheduled if entry[0] <= self.now]
        self.scheduled = [entry for entry in self.scheduled if entry not in due]
        for _when, callback, args in due:
            callback(*args)


@pytest.fixture
def loop(monkeypatch):
    fake = FakeLoop()
    # só o módulo do código enxerga o laço falso; o asyncio global (e o do pytest) fica intacto
    monkeypatch.setattr(jukebox_messages, "asyncio", SimpleNamespace(get_running_loop=lambda: fake))
    return fake


def command_context(*sent):
    ctx = object.__new__(CustomContext)  # um CustomContext de verdade, sem montar o Discord
    ctx.message = SimpleNamespace(id=1)
    ctx.store_message = None
    ctx.sent_messages = list(sent)
    return ctx


def message(message_id):
    return SimpleNamespace(id=message_id, delete=AsyncMock())


@pytest.mark.asyncio
async def test_the_warning_is_deleted_after_120_seconds_and_registers_that_deadline(loop):
    warning, ctx = message(5), command_context()

    await retain_jukebox_permissions_warning(ctx, warning)

    warning.delete.assert_awaited_once_with(delay=TTL)
    assert ctx.jukebox_warning_deadlines == {5: START + TTL}
    assert [when for when, *_ in loop.scheduled] == [START + TTL]


@pytest.mark.asyncio
async def test_the_command_cleanup_spares_the_warning_until_its_deadline_then_deletes_it(loop):
    warning, other = message(5), message(6)
    ctx = command_context(warning, other)
    await retain_jukebox_permissions_warning(ctx, warning)
    cog = SimpleNamespace(delete_message=AsyncMock())

    loop.advance(TTL - 1)
    await cleanup_jukebox_command_messages(cog, ctx)
    deleted_before = [call.args[0] for call in cog.delete_message.await_args_list]

    loop.advance(1)  # o prazo venceu: a entrada do aviso sai do registro
    assert ctx.jukebox_warning_deadlines == {}
    cog.delete_message.reset_mock()
    await cleanup_jukebox_command_messages(cog, ctx)
    deleted_after = [call.args[0] for call in cog.delete_message.await_args_list]

    assert warning not in deleted_before and other in deleted_before
    assert warning in deleted_after


@pytest.mark.asyncio
async def test_a_slash_interaction_has_no_registry_but_the_warning_still_goes_in_120_seconds(loop):
    warning = message(5)
    interaction = SimpleNamespace()  # não é CustomContext: nada de registro de prazo

    await retain_jukebox_permissions_warning(interaction, warning)

    warning.delete.assert_awaited_once_with(delay=TTL)
    assert not hasattr(interaction, "jukebox_warning_deadlines") and loop.scheduled == []


@pytest.mark.asyncio
async def test_no_warning_message_is_a_noop(loop):
    await retain_jukebox_permissions_warning(command_context(), None)

    assert loop.scheduled == []
