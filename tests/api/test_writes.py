"""Corpos das escritas (contrato T-456 §4 s4): tipo conferido antes de qualquer serviço; nunca ``TypeError``."""

import pytest

from empiremuse.api.errors import ApiError
from empiremuse.api.writes import (
    GENRES,
    IDS,
    INT,
    JSON_DOC,
    TEXT,
    TEXT_OR_NULL,
    BOOL,
    optional,
    parse_guild_write,
    required,
    values_to_args,
)

pytestmark = pytest.mark.critical

VERSION = "0123456789abcdef"
SPEC = {
    "flag": required(BOOL),
    "volume": required(INT),
    "text": required(TEXT),
    "maybe": optional(TEXT_OR_NULL),
    "ids": optional(IDS),
    "genres": optional(GENRES),
    "doc": optional(JSON_DOC),
}


def fields_of(error: ApiError):
    return error.fields


def test_converte_cada_tipo_para_o_formato_do_formulario():
    args = values_to_args(
        {
            "flag": True,
            "volume": 60,
            "text": "a",
            "maybe": None,
            "ids": ["1", "2"],
            "genres": ["a", "b"],
            "doc": {"x": 1},
        },
        SPEC,
    )

    assert args == {
        "flag": ["on"],
        "volume": ["60"],
        "text": ["a"],
        "maybe": [""],
        "ids": ["1", "2"],
        "genres": ["a, b"],
        "doc": ['{"x":1}'],
    }
    assert "flag" not in values_to_args({"flag": False, "volume": 1, "text": ""}, SPEC)  # checkbox desmarcado = ausente


def test_tipo_errado_campo_que_falta_e_campo_desconhecido_nomeiam_os_campos():
    cases = [
        ({"flag": 1, "volume": 1, "text": "x"}, ["flag"]),
        ({"flag": True, "volume": True, "text": "x"}, ["volume"]),
        ({"flag": True, "volume": 1.5, "text": "x"}, ["volume"]),
        ({"flag": True, "volume": 10**12, "text": "x"}, ["volume"]),
        ({"flag": True, "volume": 1, "text": None}, ["text"]),
        ({"flag": True, "volume": 1, "text": "x" * 3000}, ["text"]),
        ({"flag": True, "volume": 1, "text": "x", "ids": "1"}, ["ids"]),
        ({"flag": True, "volume": 1, "text": "x", "ids": [1]}, ["ids"]),
        ({"flag": True, "volume": 1, "text": "x", "ids": [""]}, ["ids"]),
        ({"flag": True, "volume": 1, "text": "x", "genres": ["a,b"]}, ["genres"]),
        ({"flag": True, "volume": 1, "text": "x", "doc": "texto"}, ["doc"]),
        ({"flag": True}, ["text", "volume"]),
        ({"flag": True, "volume": 1, "text": "x", "extra": 1}, ["extra"]),
        ({"flag": True, "volume": 1, "text": "x", "Extra-Longo!": 1}, ["values"]),  # nome fora do padrão não é ecoado
        ([], ["values"]),
        ("x", ["values"]),
    ]
    for values, fields in cases:
        with pytest.raises(ApiError) as raised:
            values_to_args(values, SPEC)

        assert (raised.value.status_code, fields_of(raised.value)) == (400, fields), values


def test_values_ausente_so_vale_quando_nenhum_campo_e_obrigatorio():
    assert values_to_args(None, {"a": optional(TEXT)}) == {}
    with pytest.raises(ApiError):
        values_to_args(None, SPEC)


def test_o_form_vem_do_corpo_ou_da_unica_opcao_da_categoria_e_a_versao_tem_formato():
    body = {"expected_version": VERSION, "values": {"djroles": ["1"]}}

    assert parse_guild_write(["dj"], body).form_key == "dj"
    for bad in (
        lambda: parse_guild_write(["a", "b"], body),
        lambda: parse_guild_write(["a", "b"], {**body, "form": "c"}),
        lambda: parse_guild_write(["a", "b"], {**body, "form": ["a"]}),
        lambda: parse_guild_write(["dj"], {**body, "expected_version": "ABC"}),
    ):
        with pytest.raises(ApiError) as raised:
            bad()

        assert raised.value.status_code == 400
