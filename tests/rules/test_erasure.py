"""``empiremuse/rules/erasure.py`` (contrato §8, §10): T3, R6 e as impressões da passada 2. Puro, sem I/O e com o relógio como parâmetro."""

from copy import deepcopy

import pytest

from empiremuse.rules.erasure import (
    ERASE_GLOBAL_PER_HOUR,
    ERASE_OK_PER_HOUR,
    ERASE_TRIES_PER_HOUR,
    STORES,
    USER_FIELD_DEFAULTS,
    ErasureStatus,
    StoreAction,
    StoreResult,
    allow_erasure,
    build_report,
    custom_favorite_key_matches,
    fingerprint,
    is_snowflake,
    login_is_recent,
    read_is_stale,
    only_defaults,
    reset_to_default,
    resurrected_paths,
    summarize,
    tombstone_seconds,
)
from empiremuse.utils.db import global_db_models

pytestmark = pytest.mark.critical

SAL = b"sal-de-teste-0123"
AGORA = 10_000.0


@pytest.mark.parametrize(
    ("chave", "casa"),
    [
        ("1:45", True),
        ("70000000000000001:12345678901234567", False),  # outro membro
        ("1:145", False),
        ("1:450", False),
        ("45:1", False),
        ("45", False),
        (":45", False),
        ("a:b:45", True),  # o membro é o que vem depois do último ":"
        ("lixo", False),
        ("", False),
        ("1:45 ", False),
        ("1:045", False),
    ],
)
def test_t3_custom_favorite_key_matches_so_com_o_sufixo_exato_depois_dos_dois_pontos(chave, casa):
    assert custom_favorite_key_matches(chave, 45) is casa


def test_t3_chave_que_nao_e_texto_nao_casa():
    assert custom_favorite_key_matches(None, 45) is False  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("texto", "valido"),
    [
        ("12345678901234567", True),  # 17 dígitos
        ("18446744073709551615", True),  # 2**64 - 1
        ("18446744073709551616", False),  # 2**64
        ("1234567890123456", False),  # 16
        ("123456789012345678901", False),  # 21
        ("12345678901234567a", False),
        ("١٢٣٤٥٦٧٨٩٠١٢٣٤٥٦٧٨", False),  # dígitos que não são ASCII
        ("-1234567890123456", False),
        ("", False),
    ],
)
def test_is_snowflake(texto, valido):
    assert is_snowflake(texto) is valido


def test_tombstone_seconds_nunca_menos_de_600_e_o_dobro_do_cache_do_banco():
    assert tombstone_seconds(300) == 600
    assert tombstone_seconds(1000) == 2000
    assert tombstone_seconds(10) == 600


# ------------------------------------------------------------------------------------------------ R6


def test_r6_sem_historico_pode():
    assert allow_erasure([], [], [], AGORA).allowed


def test_r6_tres_ok_na_hora_barram_e_dizem_quando_libera():
    ok = [AGORA - 100, AGORA - 50, AGORA - 10]

    decisao = allow_erasure(ok, ok, [], AGORA)

    assert not decisao.allowed and decisao.reason == "user_ok"
    assert decisao.retry_after_s == pytest.approx(3600 - 100)


def test_r6_partial_so_entra_em_tries_e_nao_barra_as_3_por_hora():
    tries = [AGORA - 100 + n for n in range(ERASE_TRIES_PER_HOUR - 1)]  # 9 tentativas, nenhum OK

    assert allow_erasure([], tries, [], AGORA).allowed
    barrada = allow_erasure([], [*tries, AGORA - 1], [], AGORA)
    assert not barrada.allowed and barrada.reason == "user_tries"


def test_r6_teto_global():
    global_runs = [AGORA - 60 + n * 0.5 for n in range(ERASE_GLOBAL_PER_HOUR)]

    decisao = allow_erasure([], [], global_runs, AGORA)

    assert not decisao.allowed and decisao.reason == "global"


def test_r6_o_que_passou_de_uma_hora_nao_conta_e_a_espera_e_a_maior_entre_os_limites():
    velhos = [AGORA - 3600 - n for n in range(ERASE_OK_PER_HOUR)]
    assert allow_erasure(velhos, velhos, [], AGORA).allowed

    ok = [AGORA - 3000, AGORA - 2000, AGORA - 1000]
    global_runs = [AGORA - 3500 + n for n in range(ERASE_GLOBAL_PER_HOUR)]
    decisao = allow_erasure(ok, ok, global_runs, AGORA)
    assert decisao.retry_after_s == pytest.approx(3600 - 3000)  # o do usuário (600 s) é maior que o global (100 s)


# ------------------------------------------------------------------------------------------------ impressões


def test_os_campos_e_padroes_batem_com_o_modelo_de_global_users():
    modelo = global_db_models["users"]
    for caminho, padrao in USER_FIELD_DEFAULTS.items():
        no_modelo = modelo
        for parte in caminho.split("."):
            no_modelo = no_modelo[parte]
        assert no_modelo == padrao, caminho


def _doc(**campos):
    doc = deepcopy(global_db_models["users"])
    doc.update(campos)
    return doc


def test_fingerprint_so_tem_campos_fora_do_padrao_e_nunca_o_valor():
    doc = _doc(token="T" * 50, fav_links={"a": "https://x"})
    doc["lastfm"]["sessionkey"] = "SK-SEGREDO"

    prints = fingerprint(7, doc, SAL)

    assert set(prints) == {"token", "fav_links", "lastfm.sessionkey"}
    assert "SK-SEGREDO" not in "".join(prints.values()) and "T" * 50 not in "".join(prints.values())
    assert fingerprint(7, _doc(), SAL) == {}


def test_fingerprint_depende_do_sal_e_do_usuario():
    doc = _doc(token="T" * 50)

    assert fingerprint(7, doc, SAL) == fingerprint(7, doc, SAL)
    assert fingerprint(7, doc, SAL) != fingerprint(7, doc, b"outro-sal-1234567")
    assert fingerprint(7, doc, SAL) != fingerprint(8, doc, SAL)


def test_resurrected_paths_so_valor_identico_valor_novo_nao_entra():
    antes = _doc(token="T" * 50, fav_links={"a": "https://x"}, custom_prefix="!")
    prints = fingerprint(7, antes, SAL)
    agora = _doc(token="N" * 50, fav_links={"a": "https://x"}, custom_prefix="")  # token novo, prefixo já no padrão

    assert resurrected_paths(7, agora, prints, SAL) == ["fav_links"]
    assert set(resurrected_paths(7, antes, prints, SAL)) == set(prints)
    assert resurrected_paths(7, _doc(), prints, SAL) == []
    assert resurrected_paths(7, antes, {}, SAL) == []


def test_reset_to_default_volta_ao_padrao_sem_alterar_o_original_e_o_scrobble_acompanha_a_chave():
    doc = _doc(token="T" * 50, custom_prefix="!")
    doc["lastfm"].update(username="fulano", sessionkey="SK", scrobble=True)
    original = deepcopy(doc)

    novo = reset_to_default(doc, ["lastfm.sessionkey", "token"])

    assert doc == original
    assert novo["token"] == "" and novo["lastfm"]["sessionkey"] == "" and novo["lastfm"]["scrobble"] is False
    assert novo["lastfm"]["username"] == "fulano" and novo["custom_prefix"] == "!"  # o que não foi pedido fica


def test_reset_to_default_recria_o_ramo_que_faltava():
    assert reset_to_default({}, ["lastfm.username"]) == {"lastfm": {"username": ""}}


def test_only_defaults():
    assert only_defaults(_doc()) is True
    assert only_defaults(_doc(token="x")) is False
    assert only_defaults({"_id": "1"}) is True
    doc = _doc()
    doc["lastfm"]["scrobble"] = True
    assert only_defaults(doc) is False


def test_stores_sao_os_da_secao_2_e_so_o_da_fila_de_scrobbles_e_do_banco_local():
    por_chave = {store.key: store for store in STORES}

    assert list(por_chave) == [
        "users",
        "scrobbles",
        "radio_user",
        "radio_custom_favorites",
        "radios",
        "radio_stations",
    ]
    assert [store.key for store in STORES if store.local] == ["scrobbles"]
    assert por_chave["radio_custom_favorites"].action is StoreAction.DELETE_MATCHING
    assert {por_chave["radios"].action, por_chave["radio_stations"].action} == {StoreAction.ANONYMIZE}


def test_summarize_e_build_report():
    ok = {"users": StoreResult(1), "radio_user": StoreResult(0)}
    parcial = {**ok, "scrobbles": StoreResult(2, ok=False)}

    assert summarize(ok) is ErasureStatus.OK and summarize({}) is ErasureStatus.OK
    relatorio = build_report(parcial)
    assert relatorio.status is ErasureStatus.PARTIAL
    assert dict(relatorio.counts) == {"users": 1, "radio_user": 0, "scrobbles": 2} and relatorio.failed == (
        "scrobbles",
    )


@pytest.mark.parametrize(
    ("erased_at", "read_at", "now", "velha"),
    [
        (None, 900.0, 1000.0, False),  # sem sepultura e leitura nova
        (None, 400.0, 1000.0, True),  # sem sepultura, mas a leitura tem 600 s: nunca confiável
        (None, 401.0, 1000.0, False),  # 599 s
        (950.0, 900.0, 1000.0, True),  # apagado depois da leitura
        (950.0, 950.0, 1000.0, True),  # no mesmo instante: conservador
        (950.0, 960.0, 1000.0, False),  # leitura depois do fim da exclusão: uso novo
        (100.0, 900.0, 1000.0, False),  # sepultura antiga, leitura posterior e nova
        (100.0, 50.0, 1000.0, True),  # sepultura expirada e leitura velha
    ],
)
def test_t5_read_is_stale(erased_at, read_at, now, velha):
    assert read_is_stale(erased_at, read_at, now, 600.0) is velha


@pytest.mark.parametrize(
    ("idade_s", "recente"),
    [
        (0, True),
        (29 * 60, True),
        (1800, True),
        (1801, False),
        (31 * 60, False),
    ],  # §3.5: 30 min no máximo, pela ``created_at``
)
def test_login_is_recent_measures_the_login_age_without_consent(idade_s, recente):
    assert login_is_recent(AGORA - idade_s, AGORA) is recente
