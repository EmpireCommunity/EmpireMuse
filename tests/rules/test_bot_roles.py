"""Regras puras dos papéis dos bots (principal × reservas): ordem dos convites, quem avisa, textos fixos e a descrição da reserva."""

import pytest

from empiremuse.rules.app_profile import DEFAULT_DESCRIPTION, DEFAULT_RESERVE_DESCRIPTION, render_description
from empiremuse.rules.bot_roles import (
    Peer,
    identifier_sort_key,
    invite_order,
    main_missing_hint,
    main_missing_notice,
    notifier,
)

pytestmark = pytest.mark.critical

URL = "https://discord.com/oauth2/authorize?client_id=1"
MAIN, R2, R3, R10 = (
    Peer("TOKEN", 1, True),
    Peer("TOKEN_RESERVE_2", 2, False),
    Peer("TOKEN_RESERVE_3", 3, False),
    Peer("TOKEN_RESERVE_10", 10, False),
)


def test_identifiers_sort_naturally_not_lexically():
    assert sorted(["TOKEN_RESERVE_10", "TOKEN_RESERVE_3", "TOKEN_RESERVE_2"], key=identifier_sort_key) == [
        "TOKEN_RESERVE_2",
        "TOKEN_RESERVE_3",
        "TOKEN_RESERVE_10",
    ]


@pytest.mark.parametrize(
    ("in_guild", "expected"),
    [
        ((), [MAIN, R2, R3, R10]),  # o principal primeiro, depois as reservas por identifier
        ((1,), [R2, R3, R10]),
        ((2, 10), [MAIN, R3]),  # só quem falta no servidor
        ((1, 2, 3, 10), []),
    ],
)
def test_invites_offer_only_the_missing_bots_main_first(in_guild, expected):
    assert invite_order([R10, R3, MAIN, R2], in_guild) == expected


def test_main_comes_first_by_role_not_by_name():
    late_main = Peer("ZZ_MAIN", 9, True)
    assert invite_order([R2, late_main, R3], ()) == [late_main, R2, R3]


@pytest.mark.parametrize(
    ("identifiers", "who"),
    [
        (["TOKEN_RESERVE_10", "TOKEN_RESERVE_2", "TOKEN_RESERVE_3"], "TOKEN_RESERVE_2"),
        (["TOKEN_RESERVE_3"], "TOKEN_RESERVE_3"),
        ([], None),
    ],
)
def test_only_the_lowest_identifier_notifies(identifiers, who):
    assert notifier(identifiers) == who


def test_third_party_names_and_prefixes_cannot_inject_mentions_or_markdown():  # S-V10
    hostile = "@everyone <@&5> [x](http://e.vil)"
    for text in (main_missing_hint(hostile, URL), main_missing_notice(hostile, URL, "!`x")):
        assert "@everyone" not in text and "<@&5>" not in text and "](http" not in text
        assert f"<{URL}>" in text  # o convite é o do código, entre <> (sem prévia)
    notice = main_missing_notice("Muse", URL, "!`x")
    assert "`!xplay`" in notice and "`!`" not in notice  # a crase do prefixo não fecha o trecho de código
    assert main_missing_notice(None, URL, "").count("bot principal") == 2 and "`play`" not in notice


def test_reserve_description_names_the_main_and_loses_its_lines_without_it():
    kwargs = {"prefix": "!", "bots": 3, "panel_url": "P", "support_url": "S"}
    text, _ = render_description(DEFAULT_RESERVE_DESCRIPTION, main="Muse", **kwargs)
    assert text.splitlines()[0].startswith("🎵 Reserva do Muse") and "!play" in text and "/play" not in text
    bare, _ = render_description(DEFAULT_RESERVE_DESCRIPTION, **kwargs)  # sem o principal, as linhas dele saem
    assert "Muse" not in bare and bare.splitlines() == ["⚙️ Painel: P", "💬 Suporte: S"]
    assert "{main}" not in DEFAULT_DESCRIPTION and "reserva entra na sua call" in DEFAULT_DESCRIPTION
