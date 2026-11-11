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
        ("1||abc", [1], OwnerGateMode.ENV),  # um ID válido basta
        ("1,2", [], OwnerGateMode.DENY),  # separador errado: preenchida sem ID válido
        ("abc", [], OwnerGateMode.DENY),
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
        ("1||2", [1, 2], 0),
        (" 3 || || x ||4", [3, 4], 1),
        ("1,2", [], 1),
        ("", [], 0),
        (None, [], 0),
        ("abc||def", [], 2),
    ],
)
def test_split_owner_ids_counts_the_discarded_items_without_keeping_them(raw, ids, invalid):
    assert split_owner_ids(raw) == (ids, invalid)
    assert parse_owner_ids(raw) == ids
