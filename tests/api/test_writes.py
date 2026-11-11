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


# ---------------------------------------------------------------------------- cobertura dos formulários do servidor

LINKS_AND_LISTEN_ALONG = {"links", "ouvir_junto"}  # têm corpo próprio (ação e escopo), não só ``values``


def _branches(schema: dict, field: str) -> set[str]:
    """Os valores de ``field`` (``const``) das variantes de um ``oneOf`` do arquivo, resolvendo ``$ref`` locais."""
    found = set()
    for branch in schema["oneOf"]:
        node = branch
        while "$ref" in node:
            node = spec_node(node["$ref"])
        const = node["properties"].get(field, {}).get("const")
        if const is not None:
            found.add(const)
    return found


def spec_node(ref: str) -> dict:
    from tests.api.test_openapi_contract import load_spec

    node = load_spec()
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def test_todo_formulario_do_servidor_tem_corpo_de_escrita_e_ramo_no_openapi():
    """Um formulário novo em ``CATEGORY_FORMS`` sem ``GUILD_FORM_SPECS`` ou sem ramo no ``openapi.yaml`` quebra aqui (e não
    em produção, com ``KeyError`` na primeira escrita)."""
    from empiremuse.api.writes import GUILD_FORM_SPECS
    from empiremuse.panel.services.forms import CATEGORY_FORMS

    keys = {form.key for forms in CATEGORY_FORMS.values() for form in forms}
    written = _branches(spec_node("#/components/schemas/SettingsWrite"), "form")
    read = _branches(spec_node("#/components/schemas/SettingsForm"), "key")

    assert keys - set(GUILD_FORM_SPECS) <= LINKS_AND_LISTEN_ALONG
    assert keys <= written and keys <= read
