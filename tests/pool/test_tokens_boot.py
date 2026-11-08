"""T-428: falha de tokens antes de qualquer serviço e registro antes dos avisos."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from empiremuse.config_loader import DEFAULT_CONFIG
from empiremuse.utils import client
from empiremuse.utils.client import BotPool
from tests.rules.test_bot_tokens import token

pytestmark = [pytest.mark.critical, pytest.mark.regression]


@pytest.mark.parametrize("main", [None, "invalid"])
def test_fatal_before_services_and_login(monkeypatch, main):
    monkeypatch.setattr(client.os, "environ", {})
    boot = Mock()
    pool = BotPool.__new__(BotPool)
    pool.config = {**DEFAULT_CONFIG, "TOKEN_RESERVE_1": token("b")}
    if main is not None:
        pool.config["TOKEN"] = main
    pool.boot_report = boot
    monkeypatch.setattr(client, "configure_logging", Mock())
    bot_core = Mock(side_effect=AssertionError("nenhum bot pode ser construído"))
    monkeypatch.setattr(client, "BotCore", bot_core)
    for name in ("start_heartbeat", "load_skins", "start_logs_task", "node_check"):
        setattr(pool, name, Mock(side_effect=AssertionError(name)))
    rpc = Mock(side_effect=AssertionError("RPC"))
    monkeypatch.setattr(client, "start", rpc)
    migrate = Mock(side_effect=AssertionError("migração"))
    monkeypatch.setattr(client.radios_release, "run", migrate)
    pool.servers = SimpleNamespace(migrate_language=Mock(side_effect=AssertionError("idioma")))

    pool.setup()

    assert pool.exit_code == 1
    assert [call.args[:2] for call in boot.step.call_args_list].count(("Tokens", "fail")) == 1
    for action in (bot_core, rpc, migrate, pool.start_heartbeat, pool.load_skins, pool.servers.migrate_language):
        action.assert_not_called()


def test_register_normalized_tokens_before_token_diagnostics(monkeypatch):
    monkeypatch.setattr(client.os, "environ", {})
    events = []
    pool = BotPool.__new__(BotPool)
    pool.config = {**DEFAULT_CONFIG, "TOKEN": '"' + token() + '"', "TOKEN_RADIO_" + token("z"): token("b")}
    pool.boot_report = SimpleNamespace(flush_deferred=lambda: None, step=lambda *args: events.append(args))
    monkeypatch.setattr(client, "configure_logging", lambda *args: None)
    monkeypatch.setattr(client, "register_secret", lambda value: events.append(("registered", value)))

    class StopSetup(Exception):
        pass

    pool.load_skins = Mock(side_effect=StopSetup)
    with pytest.raises(StopSetup):
        pool.setup()

    first_token_log = next(i for i, event in enumerate(events) if event[0] == "Tokens")
    assert events.index(("registered", token())) < first_token_log
    assert token() in pool.secret_values()
    diagnostics = str([event for event in events if event[0] == "Tokens"])
    assert token("z")[:8] not in diagnostics and token("z")[-4:] not in diagnostics
