"""Porta, quórum e recuo do modo rádio da Jukebox (T-434 §2.1, §2.3, §2.4; testes 1, 2, 5 e 11.3/11.6 do §8). Regras puras, em tabela."""

import pytest

from empiremuse.rules.radio_mode import (
    LIVE_RETRY_S,
    RETRY_RESET_S,
    SWITCH_COOLDOWN_S,
    VOTE_COOLDOWN_S,
    Actor,
    Decision,
    PlayerState,
    Target,
    cooldown_wait,
    gate,
    next_retry,
    quorum,
    switch_wait,
    vote_passes,
)

DJ = Actor(is_dj=True, restricted=False)
MEMBER = Actor(is_dj=False, restricted=False)
RESTRICTED_DJ = Actor(is_dj=True, restricted=True)
RESTRICTED_MEMBER = Actor(is_dj=False, restricted=True)

IDLE = PlayerState(playing=False, queued=False, radio=False)
PLAYING = PlayerState(playing=True, queued=False, radio=False)
QUEUED = PlayerState(playing=True, queued=True, radio=False)
ONLY_QUEUE = PlayerState(playing=False, queued=True, radio=False)
RADIO = PlayerState(playing=True, queued=False, radio=True)
STATION = PlayerState(playing=False, queued=False, radio=False, station=True)
BUSY = (PLAYING, QUEUED, ONLY_QUEUE, RADIO, STATION)


@pytest.mark.critical
@pytest.mark.parametrize(
    ("actor", "state", "target", "expected"),
    [
        # entrar ou trocar de rádio: ocioso e vazio entra direto, para qualquer um (inclusive no modo restrito)
        (MEMBER, IDLE, Target.LIVE, Decision.DIRECT),
        (RESTRICTED_MEMBER, IDLE, Target.LIVE, Decision.DIRECT),
        (DJ, IDLE, Target.LIVE, Decision.DIRECT),
        # algo tocando, na fila, em R ou em S: DJ direto, membro vota, restrito não-DJ é recusado
        *[(DJ, state, Target.LIVE, Decision.DIRECT) for state in BUSY],
        *[(MEMBER, state, Target.LIVE, Decision.VOTE) for state in BUSY],
        *[(RESTRICTED_MEMBER, state, Target.LIVE, Decision.DENY) for state in BUSY],
        *[(RESTRICTED_DJ, state, Target.LIVE, Decision.DIRECT) for state in BUSY],
        # sair de R por pedido de música: DJ direto, membro vota, restrito não-DJ recusa
        (DJ, RADIO, Target.LEAVE, Decision.DIRECT),
        (MEMBER, RADIO, Target.LEAVE, Decision.VOTE),
        (RESTRICTED_MEMBER, RADIO, Target.LEAVE, Decision.DENY),
    ],
)
def test_the_gate_table(actor, state, target, expected):
    result = gate(actor, state, target)
    assert result.decision is expected
    assert bool(result.reason) is (expected is Decision.DENY)


@pytest.mark.critical
@pytest.mark.parametrize(
    ("humans", "cap", "needed"),
    [(0, 3, 1), (1, 3, 1), (2, 3, 1), (3, 3, 2), (4, 3, 2), (5, 3, 3), (6, 3, 3), (7, 3, 3), (7, 2, 2), (4, 0, 1)],
)
def test_the_quorum_is_half_of_the_eligible_humans_rounded_up_between_1_and_the_cap(humans, cap, needed):
    assert quorum(humans, cap) == needed
    assert vote_passes(needed, humans, cap)
    assert not vote_passes(needed - 1, humans, cap)


@pytest.mark.critical
@pytest.mark.parametrize(
    ("attempt", "on_air_since", "now", "delay", "after"),
    [
        (0, None, 0.0, LIVE_RETRY_S[0], 1),
        (1, None, 0.0, LIVE_RETRY_S[1], 2),
        (2, None, 0.0, LIVE_RETRY_S[2], 3),
        (3, None, 0.0, None, 3),  # a quarta queda seguida esgota
        (3, 0.0, RETRY_RESET_S - 1, None, 3),  # no ar por menos de RETRY_RESET_S: a contagem continua
        (3, 0.0, RETRY_RESET_S, LIVE_RETRY_S[0], 1),  # no ar por RETRY_RESET_S: recomeça em 2 s
    ],
)
def test_the_live_drop_retries_2_4_8_and_resets_after_staying_on_air(attempt, on_air_since, now, delay, after):
    retry = next_retry(attempt, on_air_since, now)
    assert retry.delay == delay
    assert retry.attempt == after


@pytest.mark.critical
@pytest.mark.parametrize(("wait", "limit"), [(switch_wait, SWITCH_COOLDOWN_S), (cooldown_wait, VOTE_COOLDOWN_S)])
def test_switch_and_vote_cooldowns_block_only_inside_their_window(wait, limit):
    assert wait(None, 100.0) == 0
    assert wait(100.0, 100.0) == limit
    assert wait(100.0, 100.0 + limit - 1) == 1
    assert wait(100.0, 100.0 + limit) == 0
