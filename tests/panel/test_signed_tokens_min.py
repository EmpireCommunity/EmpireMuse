"""``SignedTokens`` recusa chave com menos de 32 caracteres (contrato T-456 §6 [M4])."""

import pytest

from empiremuse.panel.services import tokens as tokens_module
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


def test_registro_de_usos_cheio_recusa_novo_token_e_nunca_expulsa_um_uso_ja_gasto(monkeypatch):
    monkeypatch.setattr(tokens_module, "MAX_USED_TOKENS", 3)
    now = [100.0]
    tokens = SignedTokens("k" * SIGNING_KEY_MIN_LENGTH, clock=lambda: now[0])
    spent = [tokens.issue("1", 2, "escopo", f"alvo{n}") for n in range(3)]
    for token in spent:
        assert tokens.check(token, "1", 2, "escopo", token.split(".")[3]) is None
        tokens.consume(token)
    new = tokens.issue("1", 2, "escopo", "alvo9")

    assert tokens.check(new, "1", 2, "escopo", "alvo9") == "invalido"  # cheio de usos NÃO vencidos: sem espaço, recusa
    assert (
        tokens.check(spent[0], "1", 2, "escopo", "alvo0") == "invalido"
    )  # e o uso mais antigo NÃO foi expulso (o token gasto segue gasto)
    with pytest.raises(OverflowError):
        tokens.consume(new)

    now[0] += 61.0  # os usos vencem (e o token novo, emitido antes, também): só então há espaço de novo
    fresh = tokens.issue("1", 2, "escopo", "alvo9")
    assert tokens.check(fresh, "1", 2, "escopo", "alvo9") is None
