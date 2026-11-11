"""``SignedTokens`` recusa chave com menos de 32 caracteres (contrato T-456 §6 [M4])."""

import pytest

from empiremuse.panel.services.tokens import SIGNING_KEY_MIN_LENGTH, SignedTokens

pytestmark = pytest.mark.critical


@pytest.mark.parametrize("chave", ["", "curta", "x" * 31])
def test_chave_curta_ou_vazia_e_recusada(chave):
    with pytest.raises(ValueError) as erro:
        SignedTokens(chave)
    assert chave not in str(erro.value) or chave == ""


def test_chave_de_32_caracteres_emite_e_confere():
    tokens = SignedTokens("k" * SIGNING_KEY_MIN_LENGTH, clock=lambda: 100.0)

    token = tokens.issue("1", 2, "escopo", "alvo")

    assert tokens.check(token, "1", 2, "escopo", "alvo") is None
