"""A14 (item 18, P2) — a guarda de boot do fluxo git: ``boot_guard.run(runtime_dir, repo_dir, now, *, python, runner)``.

Cada subida com um pendente ``git`` soma 1 em ``boots``; no 3º boot sem o pool ter ficado pronto (``boots > 2``) o código volta ao
``prev_sha`` (checkout + reinstalação do lock), o pendente vira ``rolled_back`` e o processo sai com 75. O fluxo ``docker`` não é tocado,
e um JSON inválido (ou velho demais) é apagado sem mexer em nada. Repositório git real em ``tmp_path``; o ``runner`` só grava e delega.
"""

import asyncio
import dataclasses
import json
import logging
import shutil
import subprocess

import pytest

from empiremuse.rules.release import Version
from empiremuse.updater import boot_guard
from empiremuse.updater.git_flow import EXIT_RESTART, ExecResult, UpdateError, restore_checkout, run_exec
from tests.support.alvo_git import ALVO_GIT
from empiremuse.updater.state import PendingUpdate, pending_path, write_pending
from tests.updater.test_git_flow import commit, git, instalar_uv_falso

pytestmark = pytest.mark.critical

AGORA = 1_800_000_000.0
DIGEST = "sha256:" + "ab" * 32


class Morte(BaseException):
    """Simula o processo morrendo no meio do restore (``KeyboardInterrupt``/``SystemExit`` de fora)."""


class Gravador:
    """Grava as chamadas e seus códigos de saída; ``resposta(args)`` pode devolver um ``ExecResult`` no lugar da execução real."""

    def __init__(self, resposta=None):
        self.chamadas = []
        self.resultados = []
        self.resposta = resposta

    async def __call__(self, *args, **kwargs):
        self.chamadas.append(args)
        resultado = (self.resposta(args) if self.resposta else None) or await run_exec(*args, **kwargs)
        self.resultados.append((args, resultado.returncode))
        return resultado


def e_instalacao(args) -> bool:
    return "pip" in [str(a) for a in args] and "install" in [str(a) for a in args]


def instalacao_falha(args):
    return ExecResult(1, "falhou") if e_instalacao(args) else None


@pytest.fixture
def mundo(tmp_path, monkeypatch):
    """Repositório com dois commits, HEAD destacado no segundo (o código novo) e o primeiro como ``prev_sha``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    anterior = commit(repo, {"app.txt": "antigo\n"}, "antigo")
    ALVO_DO_CENARIO["sha"] = commit(repo, {"app.txt": "novo\n"}, "novo")
    git(repo, "checkout", "-q", "--detach", "HEAD")
    python = tmp_path / "python-falso"
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    instalar_uv_falso(tmp_path, monkeypatch)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    return repo, runtime, anterior, str(python)


ALVO_DO_CENARIO: dict[
    str, str
] = {}  # o ``mundo`` põe aqui o commit 'novo' real: o restore faz ``diff`` entre o anterior e ele


def pendente(anterior: str, *, flow="git", boots=0, status="pending", criado=AGORA - 60) -> PendingUpdate:
    return PendingUpdate(
        flow=flow,
        from_version=Version(2, 0, 0),
        to_version=Version(2, 1, 0),
        bot_id=1,
        channel_id=2,
        requested_by=3,
        created_at=criado,
        prev_sha=anterior if flow == "git" else None,
        digest=DIGEST if flow == "docker" else None,
        boots=boots,
        status=status,
        target_commit=ALVO_DO_CENARIO.get("sha", ALVO_GIT) if flow == "git" else None,
    )


def guardar(mundo, gravador):
    repo, runtime, _, python = mundo
    boot_guard.run(runtime, repo, AGORA, python=python, runner=gravador)


def em_disco(runtime) -> dict:
    return json.loads(pending_path(runtime).read_text())


def test_without_a_pending_update_nothing_happens(mundo):
    gravador = Gravador()

    guardar(mundo, gravador)

    assert gravador.chamadas == []


@pytest.mark.parametrize(("antes", "depois"), [(0, 1), (1, 2)], ids=["1o-boot", "2o-boot"])
def test_the_first_two_boots_only_count(mundo, antes, depois):
    repo, runtime, anterior, _ = mundo
    write_pending(runtime, pendente(anterior, boots=antes))
    head = git(repo, "rev-parse", "HEAD")
    gravador = Gravador()

    guardar(mundo, gravador)

    assert em_disco(runtime)["boots"] == depois and em_disco(runtime)["status"] == "pending"
    assert gravador.chamadas == [] and git(repo, "rev-parse", "HEAD") == head


def test_the_third_boot_rolls_back_marks_the_pending_and_exits_with_75(mundo):
    repo, runtime, anterior, python = mundo
    write_pending(runtime, pendente(anterior, boots=2))
    gravador = Gravador()

    with pytest.raises(SystemExit) as saida:
        guardar(mundo, gravador)

    assert saida.value.code == EXIT_RESTART == 75
    assert git(repo, "rev-parse", "HEAD") == anterior
    assert subprocess.run(["git", "-C", str(repo), "symbolic-ref", "-q", "HEAD"], capture_output=True).returncode != 0
    argvs = [[str(a) for a in args] for args in gravador.chamadas]
    assert any("checkout" in a and "--detach" in a and anterior in a for a in argvs)
    assert any(python in a and "pip" in a and "install" in a and "--require-hashes" in a for a in argvs)
    assert em_disco(runtime)["status"] == "rolled_back" and em_disco(runtime)["prev_sha"] == anterior
    assert [codigo for args, codigo in gravador.resultados if e_instalacao(args)] == [
        0
    ]  # a reinstalação do lock deu certo
    assert all(codigo == 0 for _, codigo in gravador.resultados)


def test_a_failing_reinstall_during_the_rollback_still_exits_with_75_and_keeps_the_state(mundo):
    """D11: restore falho NÃO marca ``rolled_back``; o pendente fica (com o contador) para a próxima subida tentar de novo."""
    repo, runtime, anterior, _ = mundo
    write_pending(runtime, pendente(anterior, boots=2))
    gravador = Gravador(instalacao_falha)

    with pytest.raises(SystemExit) as saida:
        guardar(mundo, gravador)

    assert saida.value.code == EXIT_RESTART
    assert [codigo for args, codigo in gravador.resultados if e_instalacao(args)] == [1]
    gravado = em_disco(runtime)
    assert gravado["status"] == "pending" and gravado["boots"] == 3 and gravado["restore_attempts"] == 1


def test_restore_checkout_treats_a_non_zero_reinstall_as_an_update_error(mundo):
    repo, _, anterior, python = mundo

    with pytest.raises(UpdateError):
        asyncio.run(
            restore_checkout(repo, anterior, ALVO_DO_CENARIO["sha"], python=python, runner=Gravador(instalacao_falha))
        )

    assert git(repo, "rev-parse", "HEAD") == anterior


def test_a_docker_pending_update_is_never_touched(mundo):
    repo, runtime, anterior, _ = mundo
    write_pending(runtime, pendente(anterior, flow="docker", boots=7))
    antes = pending_path(runtime).read_bytes()
    gravador = Gravador()

    guardar(mundo, gravador)

    assert pending_path(runtime).read_bytes() == antes and gravador.chamadas == []


@pytest.mark.parametrize(
    "conteudo", ["isto não é json", '{"v": 1, "flow": "git"}', "[]"], ids=["texto", "campos-faltando", "lista"]
)
def test_an_invalid_pending_file_is_deleted_without_any_action(mundo, conteudo):
    repo, runtime, _, _ = mundo
    pending_path(runtime).write_text(conteudo, encoding="utf-8")
    head = git(repo, "rev-parse", "HEAD")
    gravador = Gravador()

    guardar(mundo, gravador)

    assert not pending_path(runtime).exists() and gravador.chamadas == [] and git(repo, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("status", ["ready", "rolled_back"])
def test_a_pending_update_already_ready_or_rolled_back_is_left_untouched(mundo, status):
    """``ready`` = a subida deu certo (a guarda para de contar); ``rolled_back`` já foi revertido: nenhum dos dois conta nem reverte de novo."""
    repo, runtime, anterior, _ = mundo
    write_pending(runtime, pendente(anterior, boots=2, status=status))
    antes = pending_path(runtime).read_bytes()
    head = git(repo, "rev-parse", "HEAD")
    gravador = Gravador()

    guardar(mundo, gravador)

    assert pending_path(runtime).read_bytes() == antes and gravador.chamadas == []
    assert git(repo, "rev-parse", "HEAD") == head


# ============================================================================ D9: ``applying`` reverte já no primeiro boot


@pytest.mark.parametrize("idade", [60, 3 * 24 * 3600], ids=["recente", "mais-de-um-dia"])
def test_an_applying_mark_is_rolled_back_on_the_first_boot_without_counting(mundo, idade):
    repo, runtime, anterior, python = mundo
    write_pending(runtime, pendente(anterior, boots=0, status="applying", criado=AGORA - idade))
    gravador = Gravador()

    with pytest.raises(SystemExit) as saida:
        guardar(mundo, gravador)

    assert saida.value.code == EXIT_RESTART
    assert git(repo, "rev-parse", "HEAD") == anterior
    argvs = [[str(a) for a in args] for args in gravador.chamadas]
    assert any("checkout" in a and "--detach" in a and anterior in a for a in argvs)
    assert any(python in a and "pip" in a and "install" in a for a in argvs)  # reinstala o lock anterior
    gravado = em_disco(runtime)
    assert gravado["status"] == "rolled_back" and gravado["boots"] == 0 and gravado["prev_sha"] == anterior


def test_an_applying_mark_with_the_docker_flow_is_invalid_and_discarded(mundo):
    repo, runtime, anterior, _ = mundo
    write_pending(runtime, pendente(anterior, flow="docker", boots=0, status="applying"))
    head = git(repo, "rev-parse", "HEAD")
    gravador = Gravador()

    guardar(mundo, gravador)

    assert not pending_path(runtime).exists() and gravador.chamadas == [] and git(repo, "rev-parse", "HEAD") == head


# ============================================================================ D11: restore falho não vira ``rolled_back``; tentativas limitadas


def subir(mundo, gravador) -> int:
    with pytest.raises(SystemExit) as saida:
        guardar(mundo, gravador)
    return saida.value.code


def test_a_failed_restore_of_an_applying_mark_keeps_it_and_counts_one_attempt(mundo):
    repo, runtime, anterior, _ = mundo
    write_pending(runtime, pendente(anterior, boots=0, status="applying"))

    assert subir(mundo, Gravador(instalacao_falha)) == EXIT_RESTART

    gravado = em_disco(runtime)
    assert gravado["status"] == "applying" and gravado["restore_attempts"] == 1 and gravado["boots"] == 0


def test_the_fourth_boot_gives_up_marks_rolled_back_and_never_calls_the_restore(mundo, caplog):
    _, runtime, anterior, _ = mundo
    write_pending(runtime, dataclasses.replace(pendente(anterior, boots=0, status="applying"), restore_attempts=3))
    gravador = Gravador()

    with caplog.at_level(logging.ERROR):
        assert subir(mundo, gravador) == EXIT_RESTART

    assert gravador.chamadas == [] and em_disco(runtime)["status"] == "rolled_back"
    assert "confira o servidor" in caplog.text


def test_the_attempts_grow_on_each_boot_until_three(mundo):
    _, runtime, anterior, _ = mundo
    write_pending(runtime, pendente(anterior, boots=0, status="applying"))

    for esperado in (1, 2, 3):
        assert subir(mundo, Gravador(instalacao_falha)) == EXIT_RESTART
        assert em_disco(runtime)["restore_attempts"] == esperado and em_disco(runtime)["status"] == "applying"


def test_a_successful_restore_on_a_later_attempt_marks_rolled_back(mundo):
    repo, runtime, anterior, _ = mundo
    write_pending(runtime, pendente(anterior, boots=0, status="applying"))
    subir(mundo, Gravador(instalacao_falha))

    assert subir(mundo, Gravador()) == EXIT_RESTART

    assert git(repo, "rev-parse", "HEAD") == anterior and em_disco(runtime)["status"] == "rolled_back"


def test_the_counter_is_on_disk_before_the_restore_so_a_death_in_the_middle_still_counts(mundo):
    _, runtime, anterior, _ = mundo
    write_pending(runtime, pendente(anterior, boots=0, status="applying"))

    def morrer(args):
        raise Morte

    with pytest.raises(Morte):
        guardar(mundo, Gravador(morrer))

    assert em_disco(runtime)["restore_attempts"] == 1 and em_disco(runtime)["status"] == "applying"


def sempre_sem_espaco(monkeypatch):
    def falha(*args, **kwargs):
        raise OSError(28, "sem espaço no dispositivo")

    monkeypatch.setattr(boot_guard, "write_pending", falha)


@pytest.mark.parametrize(
    "preparar",
    [
        lambda runtime, anterior: write_pending(runtime, pendente(anterior, boots=0, status="applying")),
        lambda runtime, anterior: write_pending(runtime, pendente(anterior, boots=2)),
    ],
    ids=["applying", "pending-no-3o-boot"],
)
def test_an_unwritable_counter_means_no_restore_no_exit_75_and_the_boot_goes_on(mundo, monkeypatch, caplog, preparar):
    """D11: sem como gravar o contador não dá para limitar as tentativas; restaurar sem teto seria um laço. A subida segue como está."""
    repo, runtime, anterior, _ = mundo
    preparar(runtime, anterior)
    sempre_sem_espaco(monkeypatch)
    gravador = Gravador()
    head = git(repo, "rev-parse", "HEAD")

    with caplog.at_level(logging.ERROR):
        for _ in range(8):  # oito subidas seguidas: nenhuma restaura nem sai com 75
            guardar(mundo, gravador)  # não levanta SystemExit

    assert gravador.chamadas == [] and git(repo, "rev-parse", "HEAD") == head
    assert "não consegui gravar o estado da atualização" in caplog.text and "confira o servidor" in caplog.text


def test_giving_up_marks_rolled_back_with_four_attempts_and_says_so(mundo, caplog):
    _, runtime, anterior, _ = mundo
    write_pending(runtime, dataclasses.replace(pendente(anterior, boots=0, status="applying"), restore_attempts=3))
    gravador = Gravador()

    with caplog.at_level(logging.ERROR):
        assert subir(mundo, gravador) == EXIT_RESTART

    gravado = em_disco(runtime)
    assert gravador.chamadas == [] and gravado["status"] == "rolled_back" and gravado["restore_attempts"] == 4
    assert "desisti" in caplog.text and "3 tentativas" in caplog.text and "confira o servidor" in caplog.text


def test_the_restore_gets_the_target_commit_so_it_can_remove_what_the_target_added(mundo):
    repo, runtime, anterior, _ = mundo
    write_pending(runtime, pendente(anterior, boots=0, status="applying"))
    gravador = Gravador()

    subir(mundo, gravador)

    argvs = [[str(a) for a in args] for args in gravador.chamadas]
    assert any("diff" in a and "--diff-filter=A" in a and anterior in a and ALVO_DO_CENARIO["sha"] in a for a in argvs)


@pytest.mark.parametrize("status_e_boots", [("applying", 0), ("pending", 2)], ids=["applying", "pending-no-3o-boot"])
def test_a_giving_up_that_cannot_be_recorded_does_not_exit_75_in_a_loop(mundo, monkeypatch, caplog, status_e_boots):
    """D11: com o ``RUNTIME_DIR`` sem escrita cada boot releria ``restore_attempts=3``, falharia ao gravar a desistência e sairia com 75 de novo."""
    status, boots = status_e_boots
    repo, runtime, anterior, _ = mundo
    write_pending(runtime, dataclasses.replace(pendente(anterior, boots=boots, status=status), restore_attempts=3))
    sempre_sem_espaco(monkeypatch)
    gravador = Gravador()
    head = git(repo, "rev-parse", "HEAD")

    with caplog.at_level(logging.ERROR):
        for _ in range(8):
            guardar(mundo, gravador)  # nenhuma subida sai com 75

    assert gravador.chamadas == [] and git(repo, "rev-parse", "HEAD") == head
    assert "confira o servidor" in caplog.text


def test_with_a_working_disk_giving_up_still_records_rolled_back_with_four_attempts_and_exits_75(mundo):
    _, runtime, anterior, _ = mundo
    write_pending(runtime, dataclasses.replace(pendente(anterior, boots=0, status="applying"), restore_attempts=3))

    assert subir(mundo, Gravador()) == EXIT_RESTART

    assert em_disco(runtime)["status"] == "rolled_back" and em_disco(runtime)["restore_attempts"] == 4
