"""T-533a: regras puras da prova do token no registro do RPC (chave por IP e orçamento de provas negadas), com relógio injetado."""

import pytest

from empiremuse.rules.rpc_proof import DeniedBudget, LogThrottle, proof_ip_key

pytestmark = [pytest.mark.critical, pytest.mark.regression]


@pytest.mark.parametrize(
    ("remote_ip", "trusted", "esperado"),
    [
        ("8.8.8.8", False, "8.8.8.8"),
        ("10.0.0.5", True, "10.0.0.5"),
        ("10.0.0.5", False, None),  # par privado sem proxy confiável: só vale o teto global
        ("127.0.0.1", False, None),
        ("2001:4860:4860::8888", False, "2001:4860:4860::/64"),
        ("2001:4860:4860:0:1:2:3:4", False, "2001:4860:4860::/64"),  # o mesmo /64 conta junto
        ("fd00::1", True, "fd00::/64"),
        ("fd00::1", False, None),
        ("unknown", True, None),  # UNKNOWN_PEER
        ("lixo", True, None),
        ("::ffff:8.8.8.8", False, "8.8.8.8"),  # IPv4 mapeado vale como o IPv4, não como ::/64
        ("::ffff:10.0.0.5", False, None),
        ("::ffff:10.0.0.5", True, "10.0.0.5"),
        ("fe80::1%eth0", True, None),  # com escopo não é chave
        ("", True, None),
        (None, True, None),
    ],
)
def test_proof_ip_key(remote_ip, trusted, esperado):
    assert proof_ip_key(remote_ip, trusted) == esperado


class _Relogio:
    def __init__(self):
        self.agora = 1000.0

    def __call__(self):
        return self.agora


def _orcamento(relogio, limit=3, window=60.0, maxsize=4):
    return DeniedBudget(limit, window, maxsize, relogio)


def test_budget_exhausts_only_after_the_limit_of_denials():
    relogio = _Relogio()
    orcamento = _orcamento(relogio)

    for _ in range(2):
        orcamento.record("1.1.1.1")
    assert not orcamento.exhausted("1.1.1.1")
    orcamento.record("1.1.1.1")
    assert orcamento.exhausted("1.1.1.1")
    assert not orcamento.exhausted("2.2.2.2")  # outra chave não é afetada


def test_budget_reading_never_counts():
    orcamento = _orcamento(_Relogio())
    for _ in range(10):
        assert not orcamento.exhausted("1.1.1.1")


def test_budget_denials_expire_with_the_window():
    relogio = _Relogio()
    orcamento = _orcamento(relogio)
    for _ in range(3):
        orcamento.record("1.1.1.1")

    relogio.agora += 59.9
    assert orcamento.exhausted("1.1.1.1")
    relogio.agora += 0.2
    assert not orcamento.exhausted("1.1.1.1")


def test_budget_old_denials_do_not_add_up_with_new_ones():
    relogio = _Relogio()
    orcamento = _orcamento(relogio)
    orcamento.record("1.1.1.1")
    orcamento.record("1.1.1.1")
    relogio.agora += 61
    orcamento.record("1.1.1.1")
    assert not orcamento.exhausted("1.1.1.1")


def test_budget_is_bounded_and_drops_the_oldest_key():
    relogio = _Relogio()
    orcamento = _orcamento(relogio, limit=1, maxsize=2)
    for chave in ("a", "b", "c"):
        relogio.agora += 1
        orcamento.record(chave)

    assert len(orcamento) == 2
    assert not orcamento.exhausted("a")  # a mais antiga saiu
    assert orcamento.exhausted("c")


def test_log_throttle_lets_one_through_per_interval_and_counts_the_suppressed():
    relogio = _Relogio()
    gate = LogThrottle(60.0, relogio)

    assert gate.take() == 0  # o primeiro passa, sem suprimidos
    relogio.agora += 10
    assert gate.take() is None
    assert gate.take() is None
    relogio.agora += 50
    assert gate.take() == 2  # passa de novo e informa quantos ficaram de fora
    assert gate.take() is None
    relogio.agora += 60
    assert gate.take() == 1
