"""Geração RPC com fonte de escolha injetada: tamanho e alfabeto do protocolo."""

import string

import pytest

from empiremuse.rules.rpc import generate_rpc_token

pytestmark = pytest.mark.critical


def test_generator_accepts_a_choice_source_without_global_patching():
    calls = []

    def choose(alphabet):
        calls.append(alphabet)
        return "Z"

    assert generate_rpc_token(choice=choose) == "Z" * 50
    assert calls == [string.ascii_letters + string.digits] * 50
