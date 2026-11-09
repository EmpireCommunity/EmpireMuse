"""E5, banimento de usuário: regras puras (contrato §7, R8.1/R8.2/R8.6/R8.8). Sem I/O e sem relógio real."""

import pytest

from empiremuse.rules import erasure as erasure_rules
from empiremuse.rules.user_ban import (
    BanCommand,
    WarningLimiter,
    as_user_id,
    ban_target_error,
    erasure_exempt,
    is_blocked,
    new_user_ban_record,
    parse_ban_command,
    user_ban_from_doc,
    user_ban_to_doc,
)
from empiremuse.utils import erasure_views

pytestmark = pytest.mark.critical

BANIDO, DONO, COMUM = 100000000000000001, 100000000000000009, 100000000000000002
BANIDOS, DONOS = {BANIDO}, {DONO}


@pytest.mark.parametrize(
    ("user_id", "exempt", "esperado"),
    [
        pytest.param(BANIDO, False, True, id="int banido"),
        pytest.param(str(BANIDO), False, True, id="str banido (PanelUser.id)"),
        pytest.param(COMUM, False, False, id="comum passa"),
        pytest.param(DONO, False, False, id="dono nunca"),
        pytest.param(BANIDO, True, False, id="isenção libera"),
        pytest.param(None, False, True, id="None nega"),
        pytest.param(True, False, True, id="bool nega"),
        pytest.param(0, False, True, id="zero nega"),
        pytest.param(-5, False, True, id="negativo nega"),
        pytest.param("abc", False, True, id="texto nega"),
        pytest.param("", False, True, id="vazio nega"),
        pytest.param("１００", False, True, id="dígito não ASCII nega"),
        pytest.param(2**64, False, True, id="além de 64 bits nega"),
        pytest.param(1.5, False, True, id="float nega"),
        pytest.param(None, True, True, id="isenção não salva ID inválido"),
    ],
)
def test_is_blocked(user_id, exempt, esperado):
    assert is_blocked(user_id, BANIDOS, DONOS, exempt=exempt) is esperado


def test_as_user_id_converts_text_and_refuses_the_rest():
    assert as_user_id("123") == 123 and as_user_id(123) == 123
    assert as_user_id("1" * 21) is None and as_user_id(b"1") is None


@pytest.mark.parametrize(
    ("tipo", "cmd_tipo", "nome", "custom_id", "esperado"),
    [
        pytest.param(2, 1, "apagar_meus_dados", None, True, id="slash pt-BR"),
        pytest.param(2, 1, "delete_my_data", None, True, id="slash en-US"),
        pytest.param(2, 1, "apagar_meus_dados_x", None, False, id="nome parecido"),
        pytest.param(2, 1, "Apagar_meus_dados", None, False, id="maiúscula"),
        pytest.param(2, 2, "apagar_meus_dados", None, False, id="comando de usuário (menu)"),
        pytest.param(2, 1, "play", None, False, id="outro comando"),
        pytest.param(3, None, None, "erasure_confirm:abc", True, id="confirmar"),
        pytest.param(3, None, None, "erasure_cancel:abc", True, id="cancelar"),
        pytest.param(3, None, None, "erasure_grant:abc", True, id="concessão"),
        pytest.param(3, None, None, "erasure_grant_cancel:abc", True, id="cancelar concessão"),
        pytest.param(3, None, None, "erasure_confirm_x:abc", False, id="prefixo parecido"),
        pytest.param(3, None, None, "erasure_confirmx", False, id="sem dois-pontos"),
        pytest.param(3, None, None, "erasure_confirm", False, id="sem dois-pontos nem código"),
        pytest.param(3, None, None, "erasure_owner_send:abc", False, id="pedido do dono não entra"),
        pytest.param(3, None, None, "erasure_owner_cancel:abc", False, id="cancelar do dono não entra"),
        pytest.param(3, None, None, None, False, id="sem custom_id"),
        pytest.param(5, None, None, "erasure_confirm:abc", False, id="modal não é isento"),
        pytest.param(4, 1, "apagar_meus_dados", None, False, id="autocompletar não é isento"),
    ],
)
def test_erasure_exemption_is_exact(tipo, cmd_tipo, nome, custom_id, esperado):
    assert erasure_exempt(tipo, cmd_tipo, nome, custom_id) is esperado


def test_the_confirmation_ids_have_one_source():
    assert erasure_views.CONFIRM_ID == erasure_rules.CONFIRM_ID
    assert erasure_views.CANCEL_ID == erasure_rules.CANCEL_ID
    assert erasure_views.GRANT_PREFIX == erasure_rules.GRANT_PREFIX


def test_the_ban_collection_is_not_an_erasure_store():
    assert all(store.collection != "banned_users" for store in erasure_rules.STORES)  # R8.7: o registro sobrevive


def test_record_keeps_only_id_date_reason_and_owner_and_cleans_the_reason():
    registro = new_user_ban_record(BANIDO, "  spam\n\x1b[31mvermelho‮  " + "x" * 300, DONO, 1_700_000_000_000)
    assert len(registro.reason) <= 200 and "\n" not in registro.reason and "\x1b" not in registro.reason
    assert set(user_ban_to_doc(registro)) == {"banned_at", "reason", "by"}  # sem nome do usuário
    assert user_ban_from_doc({"_id": str(BANIDO), **user_ban_to_doc(registro)}) == registro


@pytest.mark.parametrize("_id", [None, "abc", "", 0, -1, True, 2**64, "12 3"])
def test_malformed_document_is_none(_id):
    assert user_ban_from_doc({"_id": _id, "banned_at": 1}) is None


def test_document_with_bad_fields_falls_back_without_raising():
    registro = user_ban_from_doc({"_id": BANIDO, "banned_at": "ontem", "by": True, "reason": None})
    assert (registro.banned_at, registro.by, registro.reason) == (0, 0, "")


@pytest.mark.parametrize(
    ("alvo", "esperado"),
    [
        (COMUM, None),
        (DONO, "owner"),
        (555, "bot"),
        (None, "invalid"),
        (0, "invalid"),
        ("x", "invalid"),
        (True, "invalid"),
    ],
)
def test_ban_target_error(alvo, esperado):
    assert ban_target_error(alvo, DONOS, {555}) == esperado


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("!!banuser 123 abuso repetido", BanCommand("ban", "123", "abuso repetido")),
        ("!!banuser 123", BanCommand("ban", "123", "")),
        (
            "!!banuser\t123\tmotivo",
            BanCommand("ban", "123\tmotivo", ""),
        ),  # só espaço separa o motivo; o ID inválido é recusado depois
        ("!!unbanuser 123 qualquer coisa", BanCommand("unban", "123", "")),
        ("!!banuser", BanCommand("ban", "", "")),
        ("!!banuserx 123", None),
        ("!!BanUser 123", None),
        ("!!banuser 123", None),
        ("?banuser 123", None),
        ("banuser 123", None),
        ("!!apagardados 123", None),
    ],
)
def test_parse_ban_command(texto, esperado):
    assert parse_ban_command(texto, "!!") == esperado


def test_parse_ban_command_needs_a_prefix():
    assert parse_ban_command("banuser 1", "") is None


class Relogio:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_warning_limiter_is_one_per_user_per_hour():
    relogio = Relogio()
    limiter = WarningLimiter(clock=relogio)
    assert limiter.allow(1) and not limiter.allow(1) and limiter.allow(2)
    relogio.t = 3599.0
    assert not limiter.allow(1)
    relogio.t = 3600.0
    assert limiter.allow(1)


def test_warning_limiter_memory_is_bounded():
    relogio = Relogio()
    limiter = WarningLimiter(max_users=3, clock=relogio)
    for uid in range(1, 10):
        relogio.t += 1
        limiter.allow(uid)
    assert len(limiter) <= 3
    relogio.t += 4000  # tudo venceu: a poda limpa e o usuário volta a poder ser avisado
    assert limiter.allow(1) and len(limiter) == 1
