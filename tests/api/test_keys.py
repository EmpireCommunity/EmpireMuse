"""Chave de serviço e rotação (contrato §2 m1, §14 autenticação): comparação sem curto-circuito, prazo a cada uso."""

import pytest

from empiremuse.api.keys import ServiceKeys, key_fingerprint
from tests.api.helpers import NOW, PREVIOUS_KEY, SERVICE_KEY

pytestmark = pytest.mark.critical

UNTIL = NOW + 3600


def test_chave_atual_confere_e_devolve_a_impressao_dela():
    keys = ServiceKeys(SERVICE_KEY)

    assert keys.match(SERVICE_KEY, NOW) == key_fingerprint(SERVICE_KEY)
    assert key_fingerprint(SERVICE_KEY) != SERVICE_KEY


@pytest.mark.parametrize("provided", [None, "", "outra", SERVICE_KEY + "x", 123, b"x", "é" * 40])
def test_ausente_vazia_errada_ou_de_tipo_errado_nunca_confere(provided):
    assert ServiceKeys(SERVICE_KEY, PREVIOUS_KEY, UNTIL).match(provided, NOW) is None


def test_chave_vazia_nunca_confere_nem_como_anterior():
    assert ServiceKeys(SERVICE_KEY, "", UNTIL).match("", NOW) is None


def test_chave_anterior_vale_ate_a_data_e_e_conferida_a_cada_uso():
    keys = ServiceKeys(SERVICE_KEY, PREVIOUS_KEY, UNTIL)

    assert keys.match(PREVIOUS_KEY, NOW) == key_fingerprint(PREVIOUS_KEY)
    assert keys.match(PREVIOUS_KEY, UNTIL - 1) is not None
    assert keys.match(PREVIOUS_KEY, UNTIL) is None  # venceu no meio da vida do processo
    assert keys.match(SERVICE_KEY, UNTIL + 10) is not None


def test_chave_anterior_sem_data_e_ignorada():
    assert ServiceKeys(SERVICE_KEY, PREVIOUS_KEY, None).match(PREVIOUS_KEY, NOW) is None


def test_cada_uso_da_chave_anterior_gera_aviso_sem_o_valor(caplog):
    keys = ServiceKeys(SERVICE_KEY, PREVIOUS_KEY, UNTIL)
    with caplog.at_level("WARNING"):
        keys.match(PREVIOUS_KEY, NOW)
        keys.match(PREVIOUS_KEY, NOW)
        keys.match(SERVICE_KEY, NOW)

    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 2
    assert PREVIOUS_KEY not in caplog.text


def test_compara_todas_as_chaves_sem_curto_circuito(monkeypatch):
    chamadas = []
    import empiremuse.api.keys as modulo

    original = modulo.same_secret
    monkeypatch.setattr(modulo, "same_secret", lambda a, b: chamadas.append(b) or original(a, b))

    ServiceKeys(SERVICE_KEY, PREVIOUS_KEY, UNTIL).match(SERVICE_KEY, NOW)

    assert chamadas == [SERVICE_KEY, PREVIOUS_KEY]
