"""T-533b: regra pura do portão dos comandos de dono (modos ENV, DENY e FALLBACK)."""

import pytest

from empiremuse.rules.owner_gate import (
    OwnerGateMode,
    admin_owner_allowed,
    parse_owner_ids,
    resolve_owner_gate,
    split_owner_ids,
)

pytestmark = [pytest.mark.critical, pytest.mark.regression]

DONO_ENV, DONO_APP, ESTRANHO = 100000000000000001, 100000000000000002, 100000000000000003


@pytest.mark.parametrize(
    ("raw", "ids", "esperado"),
    [
        ("100000000000000001", [DONO_ENV], OwnerGateMode.ENV),
        ("100000000000000001||abc", [DONO_ENV], OwnerGateMode.ENV),  # um ID válido basta
        ("1,2", [], OwnerGateMode.DENY),  # separador errado: preenchida sem ID válido
        ("abc", [], OwnerGateMode.DENY),
        ("-1", [], OwnerGateMode.DENY),  # só snowflake ASCII de 15 a 20 dígitos
        ("0", [], OwnerGateMode.DENY),
        ("+5", [], OwnerGateMode.DENY),
        ("1_000", [], OwnerGateMode.DENY),
        ("12345", [], OwnerGateMode.DENY),  # curto demais
        ("1 2 3 4 5 6 7 8 9 0 1 2 3 4 5", [], OwnerGateMode.DENY),
        ("١٠٠٠٠٠٠٠٠٠٠٠٠٠٠٠٠١", [], OwnerGateMode.DENY),  # dígitos não ASCII
        ("000000000000001", [], OwnerGateMode.DENY),  # zero à esquerda
        ("123456789012345678901", [], OwnerGateMode.DENY),  # 21 dígitos
        ("-1||0||+5", [], OwnerGateMode.DENY),
        (" || x", [], OwnerGateMode.DENY),
        ("", [], OwnerGateMode.FALLBACK),
        ("   ", [], OwnerGateMode.FALLBACK),
        (" || ", [], OwnerGateMode.FALLBACK),  # nenhum item não branco
        (None, [], OwnerGateMode.FALLBACK),
    ],
)
def test_resolve_owner_gate(raw, ids, esperado):
    assert resolve_owner_gate(raw, ids) is esperado


@pytest.mark.parametrize(
    ("modo", "usuario", "app_owner", "esperado"),
    [
        (OwnerGateMode.ENV, DONO_ENV, DONO_APP, True),
        (OwnerGateMode.ENV, DONO_APP, DONO_APP, False),  # o dono da aplicação fora de OWNER_IDS não passa
        (OwnerGateMode.ENV, ESTRANHO, DONO_APP, False),
        (OwnerGateMode.DENY, DONO_ENV, DONO_APP, False),
        (OwnerGateMode.DENY, DONO_APP, DONO_APP, False),
        (OwnerGateMode.FALLBACK, DONO_APP, DONO_APP, True),
        (OwnerGateMode.FALLBACK, ESTRANHO, DONO_APP, False),
        (OwnerGateMode.FALLBACK, DONO_APP, None, False),  # appinfo ainda não lido: nega
        (OwnerGateMode.FALLBACK, DONO_ENV, None, False),
    ],
)
def test_admin_owner_allowed(modo, usuario, app_owner, esperado):
    assert admin_owner_allowed(modo, usuario, frozenset({DONO_ENV}), app_owner) is esperado


@pytest.mark.parametrize(
    ("raw", "ids", "invalid"),
    [
        ("100000000000000001||100000000000000002", [DONO_ENV, DONO_APP], 0),
        (" 100000000000000003 || || x ||100000000000000004", [ESTRANHO, 100000000000000004], 1),
        ("-1||0||+5||1_000", [], 4),
        ("1,2", [], 1),
        ("", [], 0),
        (None, [], 0),
        ("abc||def", [], 2),
    ],
)
def test_split_owner_ids_counts_the_discarded_items_without_keeping_them(raw, ids, invalid):
    assert split_owner_ids(raw) == (ids, invalid)
    assert parse_owner_ids(raw) == ids
