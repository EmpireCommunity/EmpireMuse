"""Os avisos síncronos (permissão de enviar, de conectar, fila salva) saem no idioma ESCOLHIDO pelo servidor quando ele já está em cache."""

from types import SimpleNamespace

import pytest

from empiremuse.i18n.service import get_i18n
from empiremuse.utils.music.guild_text import guild_text_sync

KEY = "player.paused"
GID = 77


@pytest.fixture
def i18n(monkeypatch):
    service = get_i18n()
    monkeypatch.setattr(service, "pool", None)
    return service


def pool_with(settings):
    return SimpleNamespace(servers=SimpleNamespace(peek_section=lambda gid, section: settings if gid == GID else None))


@pytest.mark.regression
@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        pytest.param({"language": "pt-BR"}, "pt-BR", id="idioma-salvo-vence-o-do-discord"),
        pytest.param({"language": None}, "en-US", id="automatico-segue-o-discord"),
        pytest.param(None, "en-US", id="fora-do-cache-segue-o-discord"),
    ],
)
def test_sync_text_prefers_the_saved_language_when_it_is_cached(i18n, settings, expected):
    i18n.pool = pool_with(settings)
    guild = SimpleNamespace(id=GID, preferred_locale="en-US")

    assert guild_text_sync(guild, KEY) == i18n.for_locale(expected)(KEY)


def test_sync_text_without_a_guild_or_pool_uses_the_fallback(i18n):
    assert guild_text_sync(None, KEY) == i18n.for_locale("en-US")(KEY)
