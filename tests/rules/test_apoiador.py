"""Como pedir as funções Limitadas a Apoiadores (Crivo BK-9 CM1): o ticket no servidor de suporte configurado, ou a frase genérica."""

from types import SimpleNamespace

import pytest

from empiremuse.rules.apoiador import ASK_GENERIC, ASK_SUPPORT, ask_line, refused_text, support_link, with_ask
from empiremuse.utils.radio import RadioHub

pytestmark = pytest.mark.critical


def tr(key, **values):
    return f"{key}{values}" if values else key


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://empirecommunity.org/discord", "https://empirecommunity.org/discord"),
        ("  https://discord.gg/abc  ", "https://discord.gg/abc"),
        ("http://discord.gg/abc", ""),  # só https
        ("https://x.test/a)[b](c", ""),  # markdown
        ("https://x.test/@everyone", ""),  # menção em massa
        ("https://user:pass@x.test/", ""),  # credencial
        ("", ""),
        (None, ""),
        (42, ""),
    ],
)
def test_only_a_clean_https_link_is_used(value, expected):
    assert support_link(value) == expected


def test_with_a_link_the_ask_is_the_ticket_without_preview_and_without_it_the_generic_sentence():
    assert ask_line(tr, "https://discord.gg/abc") == f"{ASK_SUPPORT}{ {'link': '<https://discord.gg/abc>'} }"
    assert ask_line(tr, "") == ASK_GENERIC
    assert with_ask(tr, "aviso", "") == f"aviso {ASK_GENERIC}"


def test_only_the_no_access_refusal_gets_the_ask():
    assert refused_text(tr, "no_premium", "") == f"radio.refused.no_premium {ASK_GENERIC}"
    assert refused_text(tr, "no_manage_guild", "https://discord.gg/abc") == "radio.refused.no_manage_guild"


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"SUPPORT_SERVER": "https://discord.gg/proprio"}, "https://discord.gg/proprio"),  # quem hospeda põe o seu
        ({"SUPPORT_SERVER": ""}, ""),
        (None, ""),
    ],
)
def test_the_hub_reads_the_configured_support_server_on_each_use(config, expected):
    pool = SimpleNamespace(database=None) if config is None else SimpleNamespace(database=None, config=config)
    assert RadioHub(pool).support_url() == expected
