"""Regras puras da serialização da API do painel externo (contrato T-456 §4 m5): lista de permissão de campos.

Usa formulários de mentira com a mesma forma dos ``Formulario`` do painel (``key``, ``versao``, ``valores``,
``erros``, ``mensagem``, ``extras``); nada aqui toca o painel.
"""

import copy
import json
from types import SimpleNamespace
from typing import NamedTuple

import pytest

from empiremuse.rules import panel_serialize as ps

pytestmark = pytest.mark.critical

SECRET = "SEGREDO-NAO-PODE-SAIR-0123456789"


def make_form(key="lastfm", versao="abcdef0123456789", valores=None, extras=None, erros=None, mensagem=None):
    return SimpleNamespace(
        key=key, versao=versao, valores=valores or {}, extras=extras or {}, erros=erros or {}, mensagem=mensagem
    )


class Item(NamedTuple):
    nome: str
    url: str
    link: bool
    interno: str = SECRET


def test_only_allowlisted_values_and_extras_leave_and_the_version_is_renamed():
    form = make_form(
        key="lastfm",
        valores={"scrobble": True, "sessionkey": SECRET, "acao_erro": None},
        extras={"vinculada": True, "usuario": "fulano", "token": SECRET, "novo_campo": 1},
    )
    body = ps.serialize_form(form, ps.ACCOUNT)
    assert body == {
        "key": "lastfm",
        "version": "abcdef0123456789",
        "values": {"scrobble": True},
        "extras": {"vinculada": True, "usuario": "fulano"},
    }
    assert SECRET not in json.dumps(body)


def test_named_tuples_become_objects_with_only_the_allowlisted_fields():
    form = make_form(
        key="favoritos",
        extras={"itens": (Item("Rock", "https://x.example/r", True),), "total": 1, "maximo": 10, "exportar_url": "/p"},
    )
    body = ps.serialize_form(form, ps.ACCOUNT)
    assert body["extras"] == {
        "itens": [{"nome": "Rock", "url": "https://x.example/r", "link": True}],
        "total": 1,
        "maximo": 10,
    }
    assert SECRET not in json.dumps(body)


def test_plain_pairs_are_read_by_position_and_nested_shapes_recurse():
    pairs = (("pt-BR", "Português"), ("en-US", "English"))
    body = ps.serialize_form(
        make_form(key="idioma", valores={"language": "pt-BR"}, extras={"idiomas": pairs}), ps.GUILD
    )
    assert body["extras"]["idiomas"] == [{"code": "pt-BR", "name": "Português"}, {"code": "en-US", "name": "English"}]


def test_missing_allowlisted_fields_are_left_out_not_invented():
    body = ps.serialize_form(make_form(key="lastfm", valores={}, extras={"vinculada": False}), ps.ACCOUNT)
    assert body["values"] == {} and body["extras"] == {"vinculada": False}


def test_serialization_never_mutates_the_form():
    form = make_form(key="favoritos", extras={"itens": [Item("a", "u", False)], "total": 1}, valores={"nome": "x"})
    before = copy.deepcopy(form)
    ps.serialize_form(form, ps.ACCOUNT)
    ps.error_fields(form)
    assert form == before


def test_the_message_and_error_texts_do_not_leave_only_error_field_names_do():
    form = make_form(
        key="prefixo",
        valores={"custom_prefix": "!"},
        erros={"custom_prefix": "Texto para o usuário", "acao": "outro texto"},
        mensagem="Corrija os campos",
    )
    body = ps.serialize_form(form, ps.ACCOUNT)
    assert "Texto" not in json.dumps(body, ensure_ascii=False) and "Corrija" not in json.dumps(body)
    assert ps.error_fields(form) == ["acao", "custom_prefix"]


def test_error_field_names_are_validated_before_leaving():
    form = make_form(erros={"campo ok": "x", "ok_1": "y", "<script>": "z", "a" * 80: "w"})
    assert ps.error_fields(form) == ["ok_1"]


def test_unknown_form_key_or_scope_fails_closed():
    with pytest.raises(ps.UnknownForm):
        ps.serialize_form(make_form(key="inexistente"), ps.ACCOUNT)
    with pytest.raises(ps.UnknownForm):
        ps.serialize_form(make_form(key="lastfm"), ps.GUILD)  # a chave do escopo errado não vale
    with pytest.raises(ps.UnknownForm):
        ps.serialize_form(make_form(key="lastfm"), "outro")


def test_the_same_key_means_different_fields_per_scope():
    both = {"prefix": "!", "custom_prefix": "?"}
    guild = ps.serialize_form(make_form(key="prefixo", valores=both), ps.GUILD)
    account = ps.serialize_form(make_form(key="prefixo", valores=both), ps.ACCOUNT)
    assert guild["values"] == {"prefix": "!"} and account["values"] == {"custom_prefix": "?"}


@pytest.mark.parametrize("bad", [{"a": 1}, object(), {1, 2}, b"bytes"])
def test_only_plain_scalars_and_sequences_pass_under_a_leaf_field(bad):
    form = make_form(key="player", valores={"autoplay": True}, extras={"volumes": bad})
    with pytest.raises(ps.SerializationError):
        ps.serialize_form(form, ps.GUILD)


def test_depth_is_capped():
    deep: tuple = ()
    for _ in range(40):
        deep = (deep,)
    with pytest.raises(ps.SerializationError):
        ps.serialize_form(make_form(key="player", extras={"volumes": deep}), ps.GUILD)


def test_rpc_form_never_carries_the_token_stored_or_new():
    form = make_form(
        key="rpc",
        valores={"token": SECRET},
        extras={"disponivel": True, "configurado": True, "token_novo": SECRET, "token": SECRET},
    )
    body = ps.serialize_form(form, ps.ACCOUNT)
    assert body["values"] == {} and body["extras"] == {"disponivel": True, "configurado": True}
    assert SECRET not in json.dumps(body)


def test_new_rpc_token_comes_once_with_no_store_and_only_from_the_rpc_form():
    form = make_form(key="rpc", extras={"disponivel": True, "configurado": True, "token_novo": SECRET})
    once = ps.new_rpc_token(form)
    assert once is not None
    body, headers = once
    assert body == {"token": SECRET}
    assert headers == {"Cache-Control": "no-store"}
    assert ps.new_rpc_token(make_form(key="rpc", extras={"token_novo": None})) is None
    assert ps.new_rpc_token(make_form(key="rpc", extras={})) is None
    assert ps.new_rpc_token(make_form(key="lastfm", extras={"token_novo": SECRET})) is None


@pytest.mark.parametrize("bad", ["", 5, ["x"]])
def test_new_rpc_token_ignores_anything_that_is_not_a_non_empty_string(bad):
    assert ps.new_rpc_token(make_form(key="rpc", extras={"token_novo": bad})) is None


def test_no_allowlisted_name_looks_like_a_secret():
    forbidden = ("token", "secret", "sessionkey", "session_key", "password", "senha", "authorization", "cookie")
    names = [name for _, name in ps.all_field_names()]
    assert names, "o registro não pode estar vazio"
    for name in names:
        assert not any(part in name.lower() for part in forbidden), name


def test_every_registered_shape_is_serializable_with_empty_forms():
    for scope, key in ps.registered_forms():
        body = ps.serialize_form(make_form(key=key), scope)
        assert body["key"] == key and body["values"] == {} and body["extras"] == {}
