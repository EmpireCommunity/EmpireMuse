"""Contrato T-428: tokens sintéticos, sem rede nem configuração real."""

import copy
import dataclasses
import pickle

import pytest

from empiremuse.rules.bot_tokens import Role, describe, load_bot_tokens, role_of

pytestmark = [pytest.mark.critical, pytest.mark.regression]


def token(letter="a"):
    return letter * 24 + "." + letter * 6 + "." + letter * 30


@pytest.mark.parametrize("value", [None, "", " \t\r\n", 42])
def test_missing_main_is_fatal_even_with_reserves(value):
    source = {"TOKEN_RESERVE_2": token("b")}
    if value is not None:
        source["TOKEN"] = value
    loaded = load_bot_tokens(source)
    assert loaded.fatal
    assert loaded.issues[0].code == "MISSING"


@pytest.mark.parametrize(
    "value,code",
    [
        (" " + token(), "FORMAT"),
        (token() + " ", "FORMAT"),
        (token() + "\t", "FORMAT"),
        (token() + "\r\n", "FORMAT"),
        (token() + " " + token("b"), "FORMAT"),
        ("Bot " + token(), "FORMAT"),
        ("'" + token(), "FORMAT"),
        ("x" * 257, "TOO_LONG"),
    ],
)
def test_bad_main_is_fatal(value, code):
    result = load_bot_tokens({"TOKEN": value})
    assert result.fatal and result.issues[0].code == code
    assert code in describe(result.issues[0])


@pytest.mark.parametrize("quote", ["", "'", '"'])
def test_quotes_and_roles_and_natural_order(quote):
    result = load_bot_tokens(
        {
            "TOKEN_RADIO_2": token("d"),
            "TOKEN_RESERVE_10": token("c"),
            "TOKEN_RESERVE_2": token("b"),
            "TOKEN": quote + token() + quote,
            "IGNORED": token("e"),
            "TOKEN_1": token("e"),
            "TOKEN_" + "BOT_X": token("e"),
            "MY" + "BOT": token("e"),
            "TOKEN_RESERVE_EMPTY": " \t",
            "TOKEN_RADIO_EMPTY": "",
            "TOKEN_RADIO_NUM": 42,
        }
    )
    assert not result.fatal and not result.issues
    assert [bot.identifier for bot in result.bots] == ["TOKEN", "TOKEN_RESERVE_2", "TOKEN_RESERVE_10", "TOKEN_RADIO_2"]
    assert [bot.role for bot in result.bots] == [Role.MAIN, Role.RESERVE, Role.RESERVE, Role.RADIO]
    assert result.bots[0].reveal() == token()


@pytest.mark.parametrize("reverse", [False, True])
def test_deduplication_and_invalid_reserve_are_local(reverse):
    items = [
        ("TOKEN_RADIO_2", token("b")),
        ("TOKEN_RESERVE_2", token("b")),
        ("TOKEN_RESERVE_3", token()),
        ("TOKEN_RESERVE_BAD", "bad"),
        ("TOKEN", token()),
    ]
    result = load_bot_tokens(dict(reversed(items) if reverse else items))
    assert [bot.identifier for bot in result.bots] == ["TOKEN", "TOKEN_RESERVE_2"]
    assert [(i.code, i.other) for i in result.issues] == [
        ("DUPLICATE", "TOKEN"),
        ("FORMAT", None),
        ("DUPLICATE", "TOKEN_RESERVE_2"),
    ]
    assert not result.fatal


@pytest.mark.parametrize("prefix", ["TOKEN_RADIO_", "token_reserve_"])
def test_rejected_names_and_representations_never_echo_secrets(prefix):
    result = load_bot_tokens({"TOKEN": token(), prefix + token("z"): token("b")})
    issue = result.issues[0]
    assert issue.code == "NAME" and issue.variable == prefix.upper()
    for representation in (str(result), repr(result), str(issue), repr(issue), describe(issue), str(result.bots[0])):
        for secret in (token(), token("z"), token("b")):
            assert (
                secret not in representation and secret[:8] not in representation and secret[-4:] not in representation
            )


@pytest.mark.parametrize(
    "serialize", [pickle.dumps, copy.copy, copy.deepcopy, vars, dataclasses.asdict, lambda bot: bot.__getstate__()]
)
def test_bot_token_cannot_be_serialized(serialize):
    bot = load_bot_tokens({"TOKEN": token()}).bots[0]
    with pytest.raises(TypeError):
        serialize(bot)


@pytest.mark.parametrize(
    "identifier,role",
    [
        (None, None),
        (42, None),
        ("token_radio_a", None),
        ("TOKEN", Role.MAIN),
        ("TOKEN_RESERVE_A", Role.RESERVE),
        ("TOKEN_RADIO_A", Role.RADIO),
        ("TOKEN_RADIO_", None),
    ],
)
def test_role_of(identifier, role):
    assert role_of(identifier) is role


def test_radio_order():
    from empiremuse.rules.bot_roles import radio_order

    assert radio_order(
        ["TOKEN_RESERVE_10", "TOKEN_RADIO_10", "TOKEN", "TOKEN_RADIO_2", "TOKEN_RESERVE_2", "other"]
    ) == ["TOKEN_RADIO_2", "TOKEN_RADIO_10", "TOKEN_RESERVE_2", "TOKEN_RESERVE_10"]
