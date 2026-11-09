"""2.1.2 (A) — ponta a ponta no redator: o ID público do OAuth do painel aparece em claro; o secret continua ``***``.

Dois caminhos reais: só ``environ`` (``install_early_hooks`` → ``configure_secrets``) e ``config`` + ``environ``
(``collect_secret_values(config, environ)`` → ``configure_logging``, como o ``BotPool.setup``).
"""

import io
import logging
from pathlib import Path

import pytest

from empiremuse.logs.hooks import install_early_hooks
from empiremuse.logs.redact import REDACTED, redact, scrub
from empiremuse.logs.setup import configure_logging
from empiremuse.logs.settings import LogSettings
from empiremuse.rules.owner_shell import collect_secret_values

from tests.logs.apoio import agora_utc

pytestmark = pytest.mark.critical

ID_PUBLICO = "198765432109876543"  # só o formato de um snowflake
SECRET = "sEcReT-do-painel-oauth-9f8e7d6c"


@pytest.fixture(autouse=True)
def hooks_restaurados(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import threading

    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)


class TestSoEnviron:
    def test_the_early_hooks_leave_the_public_id_in_the_clear_and_mask_the_secret(self) -> None:
        ambiente = {"PANEL_OAUTH_CLIENT_ID": ID_PUBLICO, "PANEL_OAUTH_CLIENT_SECRET": SECRET}
        install_early_hooks(ambiente, stream=io.StringIO())

        texto = redact(f"client_id={ID_PUBLICO} client_secret={SECRET}")

        assert ID_PUBLICO in texto
        assert SECRET not in texto and REDACTED in texto


class TestConfigMaisEnviron:
    def test_configure_logging_with_the_pool_sources_keeps_the_public_id_readable(self, tmp_path: Path) -> None:
        config = {"PANEL_OAUTH_CLIENT_ID": ID_PUBLICO}
        ambiente = {"PANEL_OAUTH_CLIENT_SECRET": SECRET}
        console = io.StringIO()

        configure_logging(
            LogSettings(logging.INFO, False, False, frozenset(), 300),
            collect_secret_values(config, ambiente),
            stream=console,
            logs_dir=tmp_path / ".logs",
            now=agora_utc,
        )
        logging.getLogger("empiremuse.painel").warning("oauth id=%s secret=%s", ID_PUBLICO, SECRET)

        saida = console.getvalue()
        assert ID_PUBLICO in saida
        assert SECRET not in saida and REDACTED in saida

    def test_the_same_snowflake_also_registered_as_a_secret_value_is_masked(self) -> None:
        install_early_hooks(
            {"PANEL_OAUTH_CLIENT_ID": ID_PUBLICO, "PANEL_OAUTH_CLIENT_SECRET": ID_PUBLICO}, stream=io.StringIO()
        )

        assert ID_PUBLICO not in scrub(f"id={ID_PUBLICO}", 200)
