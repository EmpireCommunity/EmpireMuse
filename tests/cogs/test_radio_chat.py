"""A varredura do chat da rádio (item 8, R1, R-4): em lote, nunca a fixada, o painel nem o aviso do bot, uma tentativa sem permissão e
no máximo 2 varreduras ao mesmo tempo no processo."""

import asyncio
import logging

import pytest

from empiremuse.utils.radio.chat import ChatSweeper, PermissionLost
from empiremuse.utils.radio.runner import ProcessLimits

pytestmark = pytest.mark.critical

CHAT = {"voice_delete_after_s": 30, "stage_max_messages": 3, "stage_max_age_s": 600}


class FakeChat:
    def __init__(self, messages=()):
        self.t = 1_000_000.0
        self.messages = list(messages)  # (id, criada_em, protegida)
        self.fetches = 0
        self.deletes: list[list[int]] = []
        self.sleeps: list[float] = []
        self.forbidden = False
        self.in_flight = 0
        self.max_in_flight = 0

    def now(self):
        return self.t

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds
        await asyncio.sleep(0)

    async def fetch_recent(self, limit):
        self.fetches += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        for _ in range(3):
            await asyncio.sleep(0)
        self.in_flight -= 1
        if self.forbidden:
            raise PermissionLost
        return sorted(self.messages, key=lambda m: -m[1])[:limit]

    async def bulk_delete(self, ids):
        if self.forbidden:
            raise PermissionLost
        self.deletes.append(list(ids))
        gone = set(ids)
        self.messages = [m for m in self.messages if m[0] not in gone]


def sweeper(chat_ports, *, stage=False, limits=None):
    return ChatSweeper(chat_ports, CHAT, stage=stage, limits=limits or ProcessLimits())


class TestSweep:
    @pytest.mark.asyncio
    async def test_a_thousand_old_messages_take_at_most_ten_deletes(self):
        ports = FakeChat([(i, 1_000_000.0 - 100 - i, False) for i in range(1000)])
        s = sweeper(ports)

        while await s.sweep_once():
            pass

        assert ports.messages == [] and len(ports.deletes) <= 10 and all(len(batch) <= 100 for batch in ports.deletes)
        assert ports.fetches <= 11  # uma busca por varredura, mais a última que não achou nada

    @pytest.mark.asyncio
    async def test_the_pinned_the_panel_and_the_bot_notices_stay(self):
        ports = FakeChat([(1, 1_000_000.0 - 500, True), (2, 1_000_000.0 - 500, True), (3, 1_000_000.0 - 500, False)])
        s = sweeper(ports)

        deleted = await s.sweep_once()

        assert deleted == 1 and [m[0] for m in ports.messages] == [1, 2]

    @pytest.mark.asyncio
    async def test_nothing_to_delete_means_no_delete_call_at_all(self):
        ports = FakeChat([(1, 1_000_000.0 - 5, False)])

        assert await sweeper(ports).sweep_once() == 0 and ports.deletes == []

    @pytest.mark.asyncio
    async def test_stage_keeps_the_last_n_messages_and_drops_the_oldest(self):
        ports = FakeChat([(i, 1_000_000.0 - 100 + i, False) for i in range(1, 8)])

        await sweeper(ports, stage=True).sweep_once()

        assert [m[0] for m in ports.messages] == [5, 6, 7]

    @pytest.mark.asyncio
    async def test_the_rules_changed_on_the_web_page_apply_to_the_next_sweep(self):
        ports = FakeChat([(1, 1_000_000.0 - 10, False)])
        s = sweeper(ports)
        assert await s.sweep_once() == 0

        s.update({**CHAT, "voice_delete_after_s": 5})

        assert await s.sweep_once() == 1


class TestRhythm:
    def test_voice_sweeps_every_half_the_window_but_never_faster_than_five_seconds(self):
        for after, interval in ((5, 5.0), (30, 15.0), (600, 300.0)):
            s = ChatSweeper(FakeChat(), {**CHAT, "voice_delete_after_s": after}, stage=False, limits=ProcessLimits())
            assert s.interval_s == interval

    def test_stage_sweeps_at_most_every_thirty_seconds(self):
        assert sweeper(FakeChat(), stage=True).interval_s == 30.0

    @pytest.mark.asyncio
    async def test_the_loop_sleeps_the_interval_between_sweeps_and_stops_when_told(self):
        ports = FakeChat()
        s = sweeper(ports)
        original = ports.sleep

        async def sleep_then_stop(seconds):
            await original(seconds)
            if len(ports.sleeps) == 3:
                s.running = False

        ports.sleep = sleep_then_stop
        await s.run()

        assert ports.sleeps == [15.0, 15.0, 15.0] and s.sweeps == 3


class TestPermission:
    @pytest.mark.asyncio
    async def test_a_forbidden_answer_turns_the_rule_off_after_one_attempt(self, caplog):
        ports = FakeChat([(1, 1_000_000.0 - 500, False)])
        ports.forbidden = True
        s = sweeper(ports)

        with caplog.at_level(logging.WARNING):
            first = await s.sweep_once()
            for _ in range(5):
                await s.sweep_once()

        assert first == 0 and s.disabled and ports.fetches == 1  # nunca insiste
        assert caplog.text.count("desligadas") == 1

    @pytest.mark.asyncio
    async def test_the_loop_ends_once_the_rule_is_off(self):
        ports = FakeChat()
        ports.forbidden = True
        s = sweeper(ports)

        await s.run()

        assert s.disabled and ports.sleeps == [15.0] and ports.fetches == 1


class TestTransientFailure:
    @pytest.mark.asyncio
    async def test_a_network_error_loses_one_sweep_and_keeps_the_rule_on(self, caplog):
        ports = FakeChat([(1, 1_000_000.0 - 500, False)])

        async def boom(limit):
            raise ConnectionError("rede com token secreto")

        original = ports.fetch_recent
        ports.fetch_recent = boom
        s = sweeper(ports)

        with caplog.at_level(logging.WARNING):
            assert await s.sweep_once() == 0
        ports.fetch_recent = original

        assert not s.disabled and "ConnectionError" in caplog.text and "secreto" not in caplog.text
        assert await s.sweep_once() == 1


class TestProcessLimit:
    @pytest.mark.asyncio
    async def test_thirty_sweeps_at_once_run_at_most_two_at_a_time(self):
        limits = ProcessLimits()
        shared = FakeChat()
        sweepers = [ChatSweeper(shared, CHAT, stage=False, limits=limits) for _ in range(30)]

        await asyncio.gather(*(s.sweep_once() for s in sweepers))

        assert shared.fetches == 30 and shared.max_in_flight <= 2


def test_logs_carry_counts_only(caplog):
    ports = FakeChat([(987654321, 1_000_000.0 - 500, False)])
    s = sweeper(ports)

    with caplog.at_level(logging.DEBUG, logger="empiremuse.utils.radio.chat"):
        asyncio.run(s.sweep_once())

    messages = [r.getMessage() for r in caplog.records]
    assert messages == ["Chat da rádio: 1 mensagens apagadas"] and "987654321" not in "".join(messages)
