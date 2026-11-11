"""``Idempotency-Key`` (contrato T-456 §4 m4f): escopo, TTL de 10 min, teto de 100 chaves por usuário e só sucesso guardado.

Regra pura sobre o armazenamento, com relógio injetado (nada de ``sleep``)."""

import pytest

from empiremuse.api.idempotency import (
    MAX_KEYS_PER_USER,
    MAX_KEYS_TOTAL,
    MAX_STORED_BYTES,
    TTL_SECONDS,
    IdempotencyConflict,
    IdempotencyStore,
    Reply,
    request_fingerprint,
)

pytestmark = pytest.mark.critical

OK = Reply(200, {"ok": True})


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def run(store, user, key, fingerprint="f", reply=OK):
    claim = store.claim(user, key, fingerprint)
    if claim.ticket is not None:
        store.settle(claim.ticket, reply)
    return claim


def test_a_mesma_chave_e_o_mesmo_pedido_repetem_a_resposta_guardada():
    store = IdempotencyStore(Clock())
    first = run(store, "1", "k1")

    again = store.claim("1", "k1", "f")

    assert first.replay is None and again.replay == OK and again.ticket is None


def test_outro_pedido_com_a_mesma_chave_ou_ainda_em_andamento_e_conflito():
    store = IdempotencyStore(Clock())
    run(store, "1", "k1")
    store.claim("1", "k2", "f")  # em andamento: ainda não fechou

    with pytest.raises(IdempotencyConflict):
        store.claim("1", "k1", "outro")
    with pytest.raises(IdempotencyConflict):
        store.claim("1", "k2", "f")


def test_a_chave_e_por_usuario():
    store = IdempotencyStore(Clock())
    run(store, "1", "k1", reply=Reply(200, {"de": "1"}))

    other = store.claim("2", "k1", "f")

    assert other.replay is None and other.ticket is not None


def test_so_sucesso_fica_guardado_e_o_erro_libera_a_chave():
    for reply in (None, Reply(409, {"error": {}}), Reply(503, None)):
        store = IdempotencyStore(Clock())
        run(store, "1", "k1", reply=reply)

        retry = store.claim("1", "k1", "f")

        assert retry.replay is None and retry.ticket is not None  # a nova tentativa executa de verdade


def test_resposta_grande_demais_nao_e_guardada():
    store = IdempotencyStore(Clock())
    big = Reply(200, {"x": "a" * MAX_STORED_BYTES})

    run(store, "1", "k1", reply=big)

    assert store.claim("1", "k1", "f").replay is None


def test_vence_em_10_minutos_pelo_relogio():
    clock = Clock()
    store = IdempotencyStore(clock)
    run(store, "1", "k1")
    clock.now += TTL_SECONDS - 1
    assert store.claim("1", "k1", "f").replay == OK

    clock.now += 2

    assert store.claim("1", "k1", "f").replay is None


def test_teto_de_chaves_por_usuario_derruba_a_mais_antiga_e_nao_a_dos_outros():
    store = IdempotencyStore(Clock())
    run(store, "2", "other-user")
    for index in range(MAX_KEYS_PER_USER + 1):
        run(store, "1", f"key-{index:04d}")

    assert store.claim("1", "key-0000", "f").replay is None  # a primeira saiu
    assert store.claim("1", f"key-{MAX_KEYS_PER_USER:04d}", "f").replay == OK
    assert store.claim("2", "other-user", "f").replay == OK


def test_teto_total_derruba_a_mais_antiga():
    store = IdempotencyStore(Clock())
    for index in range(MAX_KEYS_TOTAL + 1):
        run(store, str(index), "key-0001")

    assert store.claim("0", "key-0001", "f").replay is None
    assert store.claim(str(MAX_KEYS_TOTAL), "key-0001", "f").replay == OK


def test_fechar_uma_chave_que_ja_foi_tomada_nao_guarda_nada():
    clock = Clock()
    store = IdempotencyStore(clock)
    ticket = store.claim("1", "k1", "f").ticket
    clock.now += TTL_SECONDS + 1
    store.claim("9", "k9", "f")  # a varredura derruba a entrada vencida

    store.settle(ticket, OK)

    assert store.claim("1", "k1", "f").replay is None


def test_a_impressao_cobre_metodo_caminho_e_corpo_sem_depender_da_ordem_das_chaves():
    base = request_fingerprint("PUT", "/api/v1/guilds/1/settings/player", {"a": 1, "b": 2})

    assert base == request_fingerprint("PUT", "/api/v1/guilds/1/settings/player", {"b": 2, "a": 1})
    assert base != request_fingerprint("PUT", "/api/v1/guilds/2/settings/player", {"a": 1, "b": 2})
    assert base != request_fingerprint("PATCH", "/api/v1/guilds/1/settings/player", {"a": 1, "b": 2})
    assert base != request_fingerprint("PUT", "/api/v1/guilds/1/settings/player", {"a": 1, "b": 3})


def test_purgar_o_usuario_descarta_as_respostas_e_os_pedidos_em_andamento_so_dele():
    store = IdempotencyStore(Clock())
    run(store, "1", "guardada")
    in_flight = store.claim("1", "andamento", "f")
    run(store, "2", "de-outro")

    store.purge_user("1")

    assert store.claim("1", "guardada", "f").replay is None
    assert store.claim("2", "de-outro", "f").replay == OK  # o outro usuário não muda
    store.purge_user("sem-nada")  # usuário sem entradas: nada acontece
    assert in_flight.ticket is not None
    assert (
        store.claim("1", "andamento", "f").ticket is not None
    )  # antes do purge a mesma chave daria conflito (em andamento)


def test_pedido_que_fecha_depois_do_apagamento_nao_guarda_a_resposta():
    store = IdempotencyStore(Clock())
    ticket = store.claim("1", "k1", "f").ticket

    store.purge_user("1")
    store.settle(ticket, OK)

    assert store.claim("1", "k1", "f").replay is None  # nada guardado: a chave executa de novo


def test_pedido_antigo_nao_fecha_a_entrada_nova_da_mesma_chave_depois_do_apagamento():
    store = IdempotencyStore(Clock())
    old = store.claim("1", "k1", "f").ticket
    store.purge_user("1")
    new = store.claim("1", "k1", "f").ticket

    store.settle(old, Reply(200, {"antigo": True}))
    store.settle(new, OK)

    assert store.claim("1", "k1", "f").replay == OK
