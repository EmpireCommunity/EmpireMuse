"""O ícone de uma playlist é UM emoji Unicode: um grafema, com VS16 e ZWJ, sem texto nem emoji personalizado."""

import pytest

from empiremuse.rules.emoji import check_emoji, split_leading_icon
from empiremuse.rules.limits import PLAYLIST_ICON_MAX

pytestmark = pytest.mark.critical

ACCEPTED = [
    "🎧",
    "🔴",
    "⛏️",  # base + VS16
    "☕",
    "🌧️",
    "👮‍♂️",  # ZWJ + VS16
    "❤️‍🔥",
    "🏳️‍🌈",
    "🏴‍☠️",
    "👍🏽",  # tom de pele
    "👩‍👩‍👧‍👦",
    "🇧🇷",  # bandeira
    "🏴󠁧󠁢󠁥󠁮󠁧󠁿",  # bandeira de subdivisão (tags)
    "1️⃣",  # keycap
    "🫩",  # Unicode 16
    "👨\u200d🦰",  # componente de cabelo depois do ZWJ
    "👩🏽\u200d🦱",
    "☝🏽",  # texto por padrão + tom de pele
    " 🎧 ",  # aparado
]
REJECTED = [
    "",
    " ",
    "rock",
    "a",
    "1",  # dígito sem keycap
    "🎧🎶",  # dois emoji
    "🎧 🎶",
    "🎧a",
    "a🎧",
    "<:rock:123456789012345678>",  # emoji personalizado
    "<a:rock:123456789012345678>",
    ":headphones:",
    "⎈",  # pictográfico que não é emoji
    "☐",
    "🂡",
    "©",  # texto por padrão, sem VS16
    "™",
    "❤",
    "☀",
    "🇿🇿",  # bandeira que não existe
    "🎧\u200d🎧\u200d🎧",  # ZWJ entre bases que nunca se juntam
    "🦰",  # componente de cabelo sozinho
    "🏻",  # tom de pele sozinho
    "🎧‍",  # ZWJ pendurado
    "‍🎧",
    "️",
    "icons/rock.png",
    "🇧",  # uma letra regional só
    "🎧‮",  # controle de direção
    "\U0001fb00",  # bloco de símbolos, não emoji pictográfico
    "\U0001f6ff\U00000378",
    "👨‍" * 9 + "👨",  # passa do limite de pontos de código
    None,
    7,
    b"\xf0\x9f\x8e\xa7",
]


@pytest.mark.parametrize("value", ACCEPTED)
def test_a_single_emoji_grapheme_is_accepted(value):
    assert check_emoji(value) == value.strip()


@pytest.mark.parametrize("value", REJECTED)
def test_anything_that_is_not_exactly_one_emoji_is_refused(value):
    assert check_emoji(value) is None


def test_the_length_cap_is_in_code_points():
    assert len("👩‍👩‍👧‍👦") <= PLAYLIST_ICON_MAX
    assert check_emoji("🎧" * (PLAYLIST_ICON_MAX + 1)) is None


@pytest.mark.parametrize(
    ("text", "icon", "name"),
    [
        ("🔴 Red Line", "🔴", "Red Line"),
        ("👮‍♂️ Midnight", "👮‍♂️", "Midnight"),
        ("💰 Funk G[old] 🎶", "💰", "Funk G[old] 🎶"),  # o emoji do fim fica no nome
        ("Funk 🎶", None, "Funk 🎶"),
        ("🔴Red", None, "🔴Red"),  # sem espaço depois: não é ícone
        ("🎧🎶 Mix", None, "🎧🎶 Mix"),  # dois emoji: nenhum vira ícone
        ("  🎧  ", "🎧", ""),
    ],
)
def test_a_leading_emoji_followed_by_a_space_becomes_the_icon(text, icon, name):
    assert split_leading_icon(text) == (icon, name)
