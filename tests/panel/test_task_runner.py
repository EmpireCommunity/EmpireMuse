"""E4c — TaskRunner (panel/services/tasks.py) com relógio, timeout e limites injetados: o que o HTTP não alcança."""

import asyncio

import pytest

from empiremuse.panel.services import tasks
from empiremuse.panel.services.tasks import TaskBusy, TaskFailure, TaskLimit, TaskRunner
from tests.panel.helpers_login import FakeClock


def run(coro):
    return asyncio.run(coro)


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


def blocker():
    gate = asyncio.Event()

    async def job():
        await gate.wait()
        return "feito"

    return gate, job


async def instant():
    return "ok"


class TestTimeoutAndStates:
    def test_states_pending_running_ok(self):
        async def scenario():
            runner = TaskRunner()
            gate, job = blocker()
            task = runner.submit("1", 100, "7", "setup", job)
            first = task.state
            await settle()
            running = task.state
            gate.set()
            await settle()
            return first, running, task

        first, running, task = run(scenario())
        assert (first, running, task.state, task.message) == ("pendente", "executando", "ok", "feito")

    def test_task_failure_shows_its_message_and_unexpected_errors_are_generic(self):
        async def scenario():
            runner = TaskRunner()

            async def known():
                raise TaskFailure("Texto seguro")

            async def unknown():
                raise RuntimeError("segredo")

            a = runner.submit("1", 100, "7", "setup", known)
            b = runner.submit("1", 200, "7", "setup", unknown)
            await settle()
            return a, b

        a, b = run(scenario())
        assert (a.state, a.message) == ("erro", "Texto seguro")
        assert (b.state, b.message) == ("erro", tasks.MSG_GENERIC) and "segredo" not in b.message


class TestLimitsAndExpiry:
    def test_one_active_task_per_guild(self):
        async def scenario():
            runner = TaskRunner()
            _, job = blocker()
            runner.submit("1", 100, "7", "setup", job)
            with pytest.raises(TaskBusy):
                runner.submit("2", 100, "7", "setup", job)
            runner.submit("2", 200, "7", "setup", job)
            await runner.shutdown()

        run(scenario())

    def test_per_user_limit_counts_active_tasks_and_evicts_the_oldest_finished(self):
        async def scenario():
            runner = TaskRunner(max_per_user=3)
            gates = []
            for guild in (1, 2, 3):
                gate, job = blocker()
                gates.append(gate)
                runner.submit("u", guild, "7", "setup", job)
            with pytest.raises(TaskLimit):
                runner.submit("u", 4, "7", "setup", instant)
            runner.submit("outro", 5, "7", "setup", instant)  # outro usuário não é afetado
            gates[0].set()
            await settle()
            runner.submit("u", 4, "7", "setup", instant)  # vaga: a terminada mais antiga sai
            kept = [t for t in runner._tasks.values() if t.owner == "u"]
            await runner.shutdown()
            return kept

        kept = run(scenario())
        assert len(kept) == 3 and all(t.guild_id != 1 for t in kept)

    def test_total_limit(self):
        async def scenario():
            runner = TaskRunner(max_total=2)
            _, job = blocker()
            runner.submit("a", 1, "7", "setup", job)
            runner.submit("b", 2, "7", "setup", job)
            with pytest.raises(TaskLimit):
                runner.submit("c", 3, "7", "setup", job)
            await runner.shutdown()

        run(scenario())

    def test_running_tasks_never_expire(self):
        async def scenario():
            clock = FakeClock()
            runner = TaskRunner(clock=clock)
            _, job = blocker()
            task = runner.submit("1", 100, "7", "setup", job)
            await settle()
            clock.advance(10 * tasks.TASK_TTL_SECONDS)
            found = runner.get(task.id)
            await runner.shutdown()
            return found

        assert run(scenario()) is not None

    def test_task_ids_are_32_urlsafe_chars_and_validated(self):
        async def scenario():
            runner = TaskRunner()
            task = runner.submit("1", 100, "7", "setup", instant)
            await settle()
            return task.id

        task_id = run(scenario())
        assert tasks.valid_task_id(task_id) and len(task_id) == 32
        for bad in ("", "x" * 31, "x" * 33, "x" * 31 + "/", "x" * 31 + "é", "x" * 31 + " "):
            assert not tasks.valid_task_id(bad), bad


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (TaskFailure("Texto seguro"), "refused"),
        (TaskFailure("Texto seguro", "channel_unavailable"), "channel_unavailable"),
        (RuntimeError("segredo"), "unexpected"),
        (TimeoutError(), "timeout"),
    ],
)
def test_an_errored_task_keeps_a_stable_reason_code_and_an_ok_one_has_none(failure, reason):
    """BK-3 (contrato rev. 9.4, ``JukeboxTaskReason``): a API devolve o código, nunca o texto; a tarefa que deu certo não tem código."""

    async def scenario():
        runner = TaskRunner()

        async def failing():
            raise failure

        errored = runner.submit("1", 100, "7", "setup", failing)
        done = runner.submit("1", 200, "7", "setup", instant)
        await settle()
        return errored, done

    errored, done = run(scenario())
    assert (errored.state, errored.reason) == ("erro", reason)
    assert (done.state, done.reason) == ("ok", None)
