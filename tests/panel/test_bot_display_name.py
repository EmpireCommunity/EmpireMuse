"""D12 — o título e o cabeçalho do painel usam o nome do bot no Discord, com "Music Bot" de reserva."""

from types import SimpleNamespace

import pytest

from empiremuse.panel.handlers.base import FALLBACK_BOT_NAME, bot_display_name


def bot(name):
    return SimpleNamespace(user=SimpleNamespace(name=name))


@pytest.mark.parametrize(
    ("pool", "esperado"),
    [
        (None, FALLBACK_BOT_NAME),
        (SimpleNamespace(main_bot=None, bots=[]), FALLBACK_BOT_NAME),
        (SimpleNamespace(main_bot=None, bots=[SimpleNamespace(user=None)]), FALLBACK_BOT_NAME),
        (SimpleNamespace(main_bot=bot("   "), bots=[]), FALLBACK_BOT_NAME),
        (SimpleNamespace(main_bot=bot(" Tocador "), bots=[bot("Reserva")]), "Tocador"),
        (SimpleNamespace(main_bot=None, bots=[SimpleNamespace(user=None), bot("Reserva")]), "Reserva"),
    ],
)
def test_nome_do_bot_com_reserva(pool, esperado):
    assert bot_display_name(pool) == esperado
