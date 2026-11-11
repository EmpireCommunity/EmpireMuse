"""Subchaves de assinatura por finalidade (contrato T-456 §6): cada serviço de token tem a sua, derivada da chave mestra."""

import pytest

from empiremuse.api.signing import PURPOSE_ERASURE, PURPOSE_JUKEBOX, PURPOSE_RADIO_RESET, derive_key
from empiremuse.panel.services.tokens import SIGNING_KEY_MIN_LENGTH, SignedTokens
from tests.api.helpers import COOKIE_SECRET, SERVICE_KEY, SIGNING_SECRET
from tests.api.support import ApiTestCase

pytestmark = pytest.mark.critical

PURPOSES = (PURPOSE_JUKEBOX, PURPOSE_RADIO_RESET, PURPOSE_ERASURE)


def test_cada_finalidade_tem_a_sua_subchave_estavel_e_longa_o_bastante():
    keys = [derive_key(SIGNING_SECRET, purpose) for purpose in PURPOSES]

    assert len(set(keys)) == len(PURPOSES) and SIGNING_SECRET not in keys
    assert all(len(key) >= SIGNING_KEY_MIN_LENGTH for key in keys)
    assert keys == [derive_key(SIGNING_SECRET, purpose) for purpose in PURPOSES]
    assert derive_key("outra-chave-mestra-0123456789-abcdefghij", PURPOSE_JUKEBOX) != keys[0]


class ServiceKeysTest(ApiTestCase):
    def test_token_de_uma_finalidade_nao_vale_em_outra_nem_com_a_chave_do_painel_generico(self):
        radio = self.ctx.radio_setup_service.tokens
        jukebox = self.ctx.jukebox_service.tokens
        token = radio.issue("1001", 100, "radio_reset", "1:100")

        assert radio.check(token, "1001", 100, "radio_reset", "1:100") is None
        assert jukebox.check(token, "1001", 100, "radio_reset", "1:100") == "invalido"  # outra subchave
        for foreign in (
            COOKIE_SECRET,
            SERVICE_KEY,
            SIGNING_SECRET,
        ):  # nem a do genérico, nem a de serviço, nem a mestra
            assert SignedTokens(foreign).check(token, "1001", 100, "radio_reset", "1:100") == "invalido"
