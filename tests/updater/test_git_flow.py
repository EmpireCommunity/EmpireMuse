"""A12-A13 (item 18, P2) — o fluxo git do auto-update: escolha da tag, plano sem shell e rollback.

Repositórios git reais e locais em ``tmp_path`` (``file://`` com ``allow_protocol="file"``). O ``HOME`` e a configuração do git do
ambiente são isolados; o interpretador das dependências e do smoke é um ``python`` falso (script ``sh``) que registra o que recebeu.
Assinaturas do contrato (3.5, 3.6): ``plan_git_update(repo_dir, repo_url, *, allow_protocol, runner=run_exec)`` e
``apply_git_update(plan, request, repo_dir, runtime_dir, *, python, runner=run_exec, now=time.time) -> PendingUpdate``. O ``runner`` injetado
só grava as chamadas e delega ao ``run_exec`` real; o resto (``update-state.json``) fica em ``RUNTIME_DIR``.
"""

import asyncio
import dataclasses
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from empiremuse.rules.release import Version
from empiremuse.updater import git_flow
from empiremuse.updater.git_flow import UpdateError, apply_git_update, git_env, plan_git_update, run_exec
from empiremuse.updater.state import UpdateRequest

pytestmark = pytest.mark.critical

AMBIENTE_GIT = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.org",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.org",
    "GIT_TERMINAL_PROMPT": "0",
}


def git(cwd: Path, *args: str) -> str:
    env = {"PATH": os.environ["PATH"], "HOME": str(cwd), **AMBIENTE_GIT}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, arquivos: dict[str, str], mensagem: str) -> str:
    for nome, conteudo in arquivos.items():
        (repo / nome).write_text(conteudo, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", mensagem)
    return git(repo, "rev-parse", "HEAD")


PYPROJECT = '[project]\nname = "empiremuse"\nrequires-python = ">=3.10"\n'
CHANGELOG_21 = "# Changelog\n\n## [2.1.0] - 2026-11-01\n- novidade da 2.1.0\n\n## [2.0.0] - 2026-09-01\n- base\n"


@pytest.fixture
def mundo(tmp_path, monkeypatch):
    """``remoto`` (main com v2.0.0 e v2.1.0 anotadas e v2.5.0 leve) e ``local`` (clone destacado em v2.0.0)."""
    monkeypatch.setenv("EMPIREMUSE_HOME", str(tmp_path / "runtime"))
    (tmp_path / "runtime").mkdir()
    remoto = tmp_path / "remoto"
    remoto.mkdir()
    git(remoto, "init", "-q", "-b", "main")
    commit(
        remoto,
        {"pyproject.toml": PYPROJECT, "CHANGELOG.md": "# Changelog\n\n## [2.0.0]\n- base\n", "app.txt": "v2.0.0\n"},
        "v2.0.0",
    )
    git(remoto, "tag", "-a", "v2.0.0", "-m", "2.0.0")
    commit(
        remoto,
        {"CHANGELOG.md": CHANGELOG_21, "app.txt": "v2.1.0\n", "build-constraints.txt": "setuptools==1\n"},
        "v2.1.0",
    )
    git(remoto, "tag", "-a", "v2.1.0", "-m", "2.1.0")
    git(remoto, "tag", "v2.5.0")  # leve: tem de ser ignorada
    local = tmp_path / "local"
    git(tmp_path, "clone", "-q", str(remoto), str(local))
    git(local, "checkout", "-q", "--detach", "v2.0.0")
    return remoto, local


def url(remoto: Path) -> str:
    return f"file://{remoto}"


def planejar(remoto: Path, local: Path, runner=run_exec):
    return asyncio.run(plan_git_update(local, url(remoto), allow_protocol="file", runner=runner))


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


# ============================================================================ git_env


def test_git_env_keeps_only_the_minimal_keys_and_no_secret():
    base = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "LANG": "C.UTF-8",
        "LC_ALL": "C",
        "TOKEN": "segredo",
        "MONGO": "mongodb://u:s@h",
        "GITHUB_TOKEN": "ghp_x",
        "GIT_DIR": "/outro/.git",
        "GIT_WORK_TREE": "/outro",
        "GIT_ASKPASS": "/bin/evil",
    }

    env = git_env(base)

    assert set(env) <= {"PATH", "HOME", "LANG", "GIT_TERMINAL_PROMPT", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL"}
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/home/x" and env["LANG"] == "C.UTF-8"
    assert (
        env["GIT_TERMINAL_PROMPT"] == "0"
        and env["GIT_CONFIG_NOSYSTEM"] == "1"
        and env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    )


# ============================================================================ A12 — escolha da tag e plano


def test_the_plan_targets_the_newest_annotated_tag_that_is_an_ancestor_of_main(mundo):
    remoto, local = mundo
    antes = head(local)

    plano = planejar(remoto, local)

    assert plano.current == Version(2, 0, 0) and plano.target == Version(2, 1, 0)
    assert plano.prev_sha == antes
    assert plano.tag_object_sha == git(remoto, "rev-parse", "refs/tags/v2.1.0")
    assert plano.target_commit == git(remoto, "rev-parse", "v2.1.0^{commit}")
    assert plano.tag_object_sha != plano.target_commit  # é o objeto da tag anotada, não o commit
    assert "novidade da 2.1.0" in plano.changelog
    assert head(local) == antes  # planejar não mexe no HEAD


def test_a_lightweight_tag_is_never_chosen(mundo):
    remoto, local = mundo

    assert planejar(remoto, local).target == Version(2, 1, 0)  # a v2.5.0 leve foi ignorada


def test_already_on_the_latest_annotated_tag_means_no_plan(mundo):
    remoto, local = mundo
    git(local, "fetch", "-q", url(remoto), "+refs/tags/v2.1.0:refs/tags/v2.1.0")
    git(local, "checkout", "-q", "--detach", "v2.1.0")

    assert planejar(remoto, local) is None


def test_a_tag_that_is_not_on_main_is_refused_and_head_stays(mundo):
    remoto, local = mundo
    git(remoto, "checkout", "-q", "-b", "lateral")
    commit(remoto, {"lateral.txt": "x\n"}, "fora da main")
    git(remoto, "tag", "-a", "v2.3.0", "-m", "2.3.0")
    git(remoto, "checkout", "-q", "main")
    antes = head(local)

    with pytest.raises(UpdateError):
        planejar(remoto, local)

    assert head(local) == antes


def test_a_dirty_tree_is_refused_listing_the_files(mundo):
    remoto, local = mundo
    (local / "app.txt").write_text("alterado à mão\n", encoding="utf-8")
    (local / "novo.txt").write_text("x\n", encoding="utf-8")
    antes = head(local)

    with pytest.raises(UpdateError) as erro:
        planejar(remoto, local)

    assert "app.txt" in str(erro.value) and head(local) == antes


def test_a_seen_tag_that_changed_its_object_sha_aborts(mundo, tmp_path):
    remoto, local = mundo
    (tmp_path / "runtime" / "update-state.json").write_text(json.dumps({"v2.1.0": "0" * 40}), encoding="utf-8")
    antes = head(local)

    with pytest.raises(UpdateError):
        planejar(remoto, local)

    assert head(local) == antes


@pytest.mark.parametrize(
    "conteudo",
    ["{isto não é json", "[]", json.dumps({"v2.1.0": "curto"}), json.dumps({"nao-e-tag": "a" * 40})],
    ids=["lixo", "nao-e-dict", "sha-invalido", "tag-invalida"],
)
def test_an_invalid_seen_tags_file_aborts_without_resetting_the_history(mundo, tmp_path, conteudo):
    """Sem o histórico não dá para saber se a tag mudou: abortar citando o arquivo, sem reescrevê-lo."""
    remoto, local = mundo
    arquivo = tmp_path / "runtime" / "update-state.json"
    arquivo.write_text(conteudo, encoding="utf-8")
    antes = head(local)

    with pytest.raises(UpdateError) as erro:
        planejar(remoto, local)

    assert "update-state.json" in str(erro.value)
    assert head(local) == antes and arquivo.read_bytes() == conteudo.encode("utf-8")


def test_a_seen_tags_path_that_cannot_be_read_aborts(mundo, tmp_path):
    remoto, local = mundo
    (tmp_path / "runtime" / "update-state.json").mkdir()
    antes = head(local)

    with pytest.raises(UpdateError) as erro:
        planejar(remoto, local)

    assert "update-state.json" in str(erro.value) and head(local) == antes


def test_a_missing_seen_tags_file_is_an_empty_history_and_records_the_new_tag(mundo, tmp_path):
    remoto, local = mundo
    arquivo = tmp_path / "runtime" / "update-state.json"
    assert not arquivo.exists()

    plano = planejar(remoto, local)

    assert plano.target == Version(2, 1, 0)
    assert json.loads(arquivo.read_text(encoding="utf-8")) == {"v2.1.0": plano.tag_object_sha}


def test_origin_pointing_elsewhere_is_never_used(mundo):
    remoto, local = mundo
    git(local, "remote", "set-url", "origin", "file:///nao/existe/de/jeito/nenhum")

    assert planejar(remoto, local).target == Version(2, 1, 0)


def test_a_requires_python_above_the_interpreter_is_refused_before_any_checkout(mundo):
    remoto, local = mundo
    git(remoto, "tag", "-d", "v2.1.0")
    commit(
        remoto,
        {"pyproject.toml": '[project]\nname = "empiremuse"\nrequires-python = ">=99.0"\n'},
        "exige python futuro",
    )
    git(remoto, "tag", "-a", "v2.1.0", "-m", "2.1.0")
    antes = head(local)

    with pytest.raises(UpdateError):
        planejar(remoto, local)

    assert head(local) == antes


def test_a_tag_whose_header_names_another_tag_is_refused_even_if_the_message_says_its_name(mundo):
    """``refs/tags/v2.1.0`` aponta para um objeto de tag cujo cabeçalho é ``tag outro-nome``; a mensagem repete ``tag v2.1.0`` de isca."""
    remoto, local = mundo
    git(remoto, "tag", "-d", "v2.1.0")
    commit(remoto, {"app.txt": "v2.1.0 trocada\n"}, "v2.1.0 trocada")
    git(remoto, "tag", "-a", "outro-nome", "-m", "isca\ntag v2.1.0\nfim")
    objeto = git(remoto, "rev-parse", "refs/tags/outro-nome")
    git(remoto, "update-ref", "refs/tags/v2.1.0", objeto)
    git(remoto, "tag", "-d", "outro-nome")
    antes = head(local)

    with pytest.raises(UpdateError):
        planejar(remoto, local)

    assert head(local) == antes


def test_a_hostile_global_gitconfig_has_no_effect(mundo, tmp_path, monkeypatch):
    remoto, local = mundo
    casa = tmp_path / "casa"
    casa.mkdir()
    marca = tmp_path / "fsmonitor-rodou"
    gancho = tmp_path / "gancho.sh"
    gancho.write_text(f"#!/bin/sh\ntouch {marca}\n", encoding="utf-8")
    gancho.chmod(0o755)
    (casa / ".gitconfig").write_text(
        f'[url "file:///repo/malicioso"]\n\tinsteadOf = {url(remoto)}\n[core]\n\tfsmonitor = {gancho}\n\thooksPath = {casa}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(casa))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(casa))

    plano = planejar(remoto, local)

    assert plano.target == Version(2, 1, 0)
    assert not marca.exists()


# ============================================================================ A13 — sem shell, ambiente mínimo, -c de segurança

OPCOES_DE_SEGURANCA = [
    "protocol.allow=never",
    "protocol.file.allow=always",
    "core.hooksPath=/dev/null",
    "http.sslVerify=true",
    "credential.helper=",
    "core.fsmonitor=false",
    "transfer.fsckObjects=true",
]
CHAVES_PERMITIDAS = {"PATH", "HOME", "LANG", "GIT_TERMINAL_PROMPT", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL"}


@pytest.fixture
def gravador(monkeypatch):
    """Runner injetado: grava ``(argv, env)`` de cada chamada e delega ao ``run_exec`` real; o shell fica proibido."""

    class Gravador:
        def __init__(self):
            self.chamadas = []

        async def __call__(self, *args, **kwargs):
            self.chamadas.append((args, kwargs.get("env")))
            return await run_exec(*args, **kwargs)

    async def sem_shell(*args, **kwargs):
        raise AssertionError("create_subprocess_shell não pode ser usado no fluxo de atualização")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", sem_shell)
    return Gravador()


def test_every_git_runs_without_shell_with_the_security_options_and_the_minimal_env(mundo, gravador, monkeypatch):
    remoto, local = mundo
    for nome in ("TOKEN", "MONGO", "GITHUB_TOKEN", "GIT_DIR", "GIT_WORK_TREE", "GIT_ASKPASS"):
        monkeypatch.setenv(nome, "valor-secreto")

    planejar(remoto, local, runner=gravador)

    gits = [(a, e) for a, e in gravador.chamadas if Path(str(a[0])).name == "git"]
    assert gits and len(gits) == len(gravador.chamadas)
    for argv, env in gits:
        argv = [str(a) for a in argv]
        for opcao in OPCOES_DE_SEGURANCA:
            assert opcao in argv, (opcao, argv)
        assert f"--git-dir={local}/.git" in argv and f"--work-tree={local}" in argv
        assert set(env) <= CHAVES_PERMITIDAS and "valor-secreto" not in env.values()
    assert any("--end-of-options" in [str(a) for a in argv] for argv, _ in gits)


# ============================================================================ apply: dependências, smoke e rollback

AGORA = 1_800_000_000.0
PEDIDO = UpdateRequest(bot_id=111, channel_id=222, requested_by=333)

PYTHON_FALSO = """#!/bin/sh
d=$(dirname "$0")
if [ "$2 $3" = "pip install" ] && [ -f "$d/pip_falha" ]; then rm -f "$d/pip_falha"; exit 1; fi
case "$1" in
  -c) [ -f "$d/smoke_falha" ] && exit 1 ;;
esac
exit 0
"""


UV_FALSO = """#!/bin/sh
d=$(dirname "$0")
if [ "$1 $2" = "pip install" ] && [ -f "$d/pip_falha" ]; then rm -f "$d/pip_falha"; exit 1; fi
exit 0
"""


def instalar_uv_falso(tmp_path, monkeypatch) -> Path:
    """``uv`` falso no PATH do código testado (o updater exige o uv). A marca ``pip_falha`` fica AO LADO do script."""
    script = tmp_path / "uv-falso"
    script.write_text(UV_FALSO, encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: str(script))
    return script


@pytest.fixture
def python_falso(tmp_path, monkeypatch):
    """Interpretador falso e ``uv`` falso (``uv pip install``). As marcas de falha ficam AO LADO do script, porque o
    ambiente mínimo do contrato (V6) não repassa variáveis do teste ao processo."""
    script = tmp_path / "python-falso"
    script.write_text(PYTHON_FALSO, encoding="utf-8")
    script.chmod(0o755)
    instalar_uv_falso(tmp_path, monkeypatch)
    return script


def aplicar(plano, local, script, tmp_path, runner=run_exec):
    return asyncio.run(
        apply_git_update(
            plano, PEDIDO, local, tmp_path / "runtime", python=str(script), runner=runner, now=lambda: AGORA
        )
    )


def pendente_em_disco(tmp_path) -> dict:
    return json.loads((tmp_path / "runtime" / "update-pending.json").read_text())


def test_apply_leaves_head_detached_on_the_tag_commit_and_returns_the_pending_update(
    mundo, python_falso, gravador, tmp_path
):
    remoto, local = mundo
    plano = planejar(remoto, local)

    pendente = aplicar(plano, local, python_falso, tmp_path, gravador)

    assert head(local) == plano.target_commit
    assert subprocess.run(["git", "-C", str(local), "symbolic-ref", "-q", "HEAD"], capture_output=True).returncode != 0
    chamadas = [" ".join(str(a) for a in argv) for argv, _ in gravador.chamadas]
    assert any(
        "pip install" in c
        and "--require-hashes" in c
        and "requirements-lock.txt" in c
        and "--build-constraint build-constraints.txt" in c
        for c in chamadas
    )
    assert any("import empiremuse.utils.client" in c for c in chamadas)
    assert any("-m compileall" in c and "empiremuse" in c for c in chamadas)
    assert (pendente.v, pendente.flow, pendente.boots, pendente.status) == (1, "git", 0, "pending")
    assert (str(pendente.from_version), str(pendente.to_version)) == ("2.0.0", "2.1.0")
    assert (pendente.bot_id, pendente.channel_id, pendente.requested_by) == (111, 222, 333)
    assert pendente.created_at == AGORA and pendente.prev_sha == plano.prev_sha


def test_the_pending_file_is_written_with_the_contract_fields_and_mode_0600(mundo, python_falso, tmp_path):
    remoto, local = mundo
    plano = planejar(remoto, local)

    aplicar(plano, local, python_falso, tmp_path)

    arquivo = tmp_path / "runtime" / "update-pending.json"
    assert arquivo.stat().st_mode & 0o777 == 0o600
    assert pendente_em_disco(tmp_path) == {
        "v": 1,
        "flow": "git",
        "from_version": "2.0.0",
        "to_version": "2.1.0",
        "bot_id": 111,
        "channel_id": 222,
        "requested_by": 333,
        "created_at": AGORA,
        "prev_sha": plano.prev_sha,
        "boots": 0,
        "status": "pending",
        "restore_attempts": 0,
        "target_commit": plano.target_commit,
    }


def test_the_dependency_install_and_the_smoke_never_see_secrets(mundo, python_falso, gravador, tmp_path, monkeypatch):
    remoto, local = mundo
    plano = planejar(remoto, local)
    for nome in ("TOKEN", "MONGO", "GITHUB_TOKEN", "GIT_DIR"):
        monkeypatch.setenv(nome, "valor-secreto")

    aplicar(plano, local, python_falso, tmp_path, gravador)

    do_python = [
        (argv, env) for argv, env in gravador.chamadas if Path(str(argv[0])).name in {"python-falso", "uv-falso"}
    ]
    assert len(do_python) == 3  # instalação, import e compileall
    for _, env in do_python:
        assert "valor-secreto" not in env.values()
        assert not {"TOKEN", "MONGO", "GITHUB_TOKEN"} & set(env) and not any(k.startswith("GIT_DIR") for k in env)


def test_a_failing_smoke_rolls_back_to_prev_sha_reinstalls_the_lock_and_leaves_no_pending(
    mundo, python_falso, gravador, tmp_path
):
    remoto, local = mundo
    plano = planejar(remoto, local)
    (tmp_path / "smoke_falha").write_text("1", encoding="utf-8")

    with pytest.raises(UpdateError):
        aplicar(plano, local, python_falso, tmp_path, gravador)

    assert head(local) == plano.prev_sha
    instalacoes = [a for a, _ in gravador.chamadas if "pip" in [str(x) for x in a] and "install" in [str(x) for x in a]]
    assert len(instalacoes) == 2  # a do alvo e a do lock anterior
    assert not (tmp_path / "runtime" / "update-pending.json").exists()


def test_the_rollback_reinstall_of_a_tree_without_the_constraints_file_goes_on_without_the_flag(
    mundo, python_falso, gravador, tmp_path
):
    """O v2.0.0 é anterior ao build-constraints.txt: a reversão tem de continuar funcionando, sem o flag."""
    remoto, local = mundo
    plano = planejar(remoto, local)
    (tmp_path / "smoke_falha").write_text("1", encoding="utf-8")

    with pytest.raises(UpdateError):
        aplicar(plano, local, python_falso, tmp_path, gravador)

    instalacoes = [
        [str(x) for x in a]
        for a, _ in gravador.chamadas
        if "pip" in [str(x) for x in a] and "install" in [str(x) for x in a]
    ]
    assert "--build-constraint" in instalacoes[0] and "--build-constraint" not in instalacoes[1]


def test_without_uv_the_update_is_refused_before_touching_anything(
    mundo, python_falso, gravador, monkeypatch, tmp_path
):
    """O pip não confere o hash do backend de build dos sdists do lock: sem o uv nada roda, nem o git."""
    remoto, local = mundo
    plano = planejar(remoto, local)
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: None)
    chamadas_antes = len(gravador.chamadas)

    with pytest.raises(UpdateError, match="uv"):
        aplicar(plano, local, python_falso, tmp_path, gravador)

    assert head(local) == plano.prev_sha
    assert len(gravador.chamadas) == chamadas_antes
    assert not (tmp_path / "runtime" / "update-pending.json").exists()


def test_an_update_whose_target_has_no_constraints_file_is_refused_before_touching_anything(
    mundo, python_falso, gravador, tmp_path
):
    """Fail closed: o alvo sem build-constraints.txt (como o v2.0.0) não é aplicado; nada é gravado nem trocado."""
    remoto, local = mundo
    plano = planejar(remoto, local)
    sem_arquivo = git(remoto, "rev-parse", "v2.0.0^{commit}")
    plano = dataclasses.replace(plano, target_commit=sem_arquivo)

    with pytest.raises(UpdateError, match="build-constraints.txt"):
        aplicar(plano, local, python_falso, tmp_path, gravador)

    assert head(local) == plano.prev_sha
    assert not any("checkout" in [str(a) for a in argv] for argv, _ in gravador.chamadas)
    assert not (tmp_path / "runtime" / "update-pending.json").exists()


def test_a_failing_dependency_install_also_rolls_back(mundo, python_falso, gravador, tmp_path):
    """A 1ª instalação (a do alvo) falha; a reinstalação do lock anterior passa."""
    remoto, local = mundo
    plano = planejar(remoto, local)
    (tmp_path / "pip_falha").write_text("1", encoding="utf-8")

    with pytest.raises(UpdateError):
        aplicar(plano, local, python_falso, tmp_path, gravador)

    assert head(local) == plano.prev_sha
    instalacoes = [a for a, _ in gravador.chamadas if "pip" in [str(x) for x in a] and "install" in [str(x) for x in a]]
    assert len(instalacoes) == 2
    assert not (tmp_path / "runtime" / "update-pending.json").exists()


# ============================================================================ cancelamento: filho morto e reversão


def test_cancelling_run_exec_kills_and_reaps_the_child_and_reraises():
    """O shutdown cancela a tarefa: o git/pip não pode seguir vivo."""
    capturado = []
    criado = asyncio.Event()
    original = asyncio.create_subprocess_exec

    async def espiao(*args, **kwargs):
        processo = await original(*args, **kwargs)
        capturado.append(processo)
        criado.set()
        return processo

    async def principal():
        tarefa = asyncio.create_task(
            run_exec(sys.executable, "-c", "import time; time.sleep(30)", cwd=None, env=os.environ, timeout=600)
        )
        await asyncio.wait_for(criado.wait(), 30)
        tarefa.cancel()
        propagou = vivo = False
        try:
            await tarefa
        except asyncio.CancelledError:
            propagou = True
        finally:
            vivo = capturado[0].returncode is None
            if capturado[0].returncode is None:  # não deixa o filho de pé se a correção faltar
                capturado[0].kill()
                await capturado[0].wait()
        return propagou, vivo

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(asyncio, "create_subprocess_exec", espiao)
        propagou, vivo = asyncio.run(principal())

    assert propagou and not vivo


def test_a_timeout_still_returns_124_with_the_child_dead():
    capturado = []
    original = asyncio.create_subprocess_exec

    async def espiao(*args, **kwargs):
        processo = await original(*args, **kwargs)
        capturado.append(processo)
        return processo

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(asyncio, "create_subprocess_exec", espiao)
        resultado = asyncio.run(
            run_exec(sys.executable, "-c", "import time; time.sleep(30)", cwd=None, env=os.environ, timeout=0.2)
        )

    assert resultado.returncode == 124 and capturado[0].returncode is not None


def runner_que_falha(plano, *, no_smoke=None, no_restore=None):
    """Delega ao ``run_exec`` real, mas levanta ``no_smoke`` no teste de fumaça e ``no_restore`` no checkout do ``prev_sha``."""

    async def runner(*args, **kwargs):
        texto = [str(a) for a in args]
        if no_smoke and "import empiremuse.utils.client" in texto:
            raise no_smoke
        if "checkout" in texto and plano.prev_sha in texto:
            runner.restore_tentado = True
            if no_restore:
                raise no_restore
        return await run_exec(*args, **kwargs)

    runner.restore_tentado = False
    return runner


def test_a_cancellation_during_the_smoke_restores_the_checkout_and_reraises(mundo, python_falso, tmp_path):
    remoto, local = mundo
    plano = planejar(remoto, local)

    with pytest.raises(asyncio.CancelledError):
        aplicar(plano, local, python_falso, tmp_path, runner_que_falha(plano, no_smoke=asyncio.CancelledError()))

    assert head(local) == plano.prev_sha
    assert not (tmp_path / "runtime" / "update-pending.json").exists()


def test_a_failing_write_of_the_applying_mark_runs_no_git_and_reraises(
    mundo, python_falso, gravador, tmp_path, monkeypatch
):
    """D9: sem a marca no disco nenhum git roda; o erro do disco sobe e o clone nem chega a mudar."""
    remoto, local = mundo
    plano = planejar(remoto, local)

    def falha(*args, **kwargs):
        raise OSError(28, "sem espaço")

    monkeypatch.setattr(git_flow, "write_pending", falha)

    with pytest.raises(OSError):
        aplicar(plano, local, python_falso, tmp_path, gravador)

    # só o cat-file de leitura roda antes da marca; nenhum checkout nem instalação
    assert head(local) == plano.prev_sha
    assert not any("checkout" in [str(a) for a in argv] for argv, _ in gravador.chamadas)
    assert not (tmp_path / "runtime" / "update-pending.json").exists()


def test_when_the_restore_also_fails_the_original_exception_is_the_one_that_rises(mundo, python_falso, tmp_path):
    remoto, local = mundo
    plano = planejar(remoto, local)
    runner = runner_que_falha(plano, no_smoke=asyncio.CancelledError(), no_restore=RuntimeError("falha do restore"))

    with pytest.raises(asyncio.CancelledError):
        aplicar(plano, local, python_falso, tmp_path, runner)

    assert runner.restore_tentado
    # D9: o restore falhou, então a marca ``applying`` FICA para o boot_guard reverter no próximo início
    assert pendente_em_disco(tmp_path)["status"] == "applying"


# ============================================================================ D9: pendente ``applying`` antes de qualquer efeito colateral


def roteiro(plano, *, smoke=None, restore=None, instalacao=None):
    """Delega ao ``run_exec`` real; ``smoke``/``restore``/``instalacao`` (exceção ou ``ExecResult``) substituem a 1ª instalação (a do alvo),
    o teste de fumaça e o checkout do ``prev_sha``. Anota o que o disco tinha no 1º ``checkout``."""
    estado = {"instalacoes": 0, "restore_tentado": False, "pendente_no_1o_checkout": None, "checkouts": 0}

    async def runner(*args, **kwargs):
        texto = [str(a) for a in args]
        if "checkout" in texto:
            estado["checkouts"] += 1
            arquivo = Path(kwargs["cwd"]).parent / "runtime" / "update-pending.json"
            if estado["pendente_no_1o_checkout"] is None:
                estado["pendente_no_1o_checkout"] = json.loads(arquivo.read_text()) if arquivo.exists() else False
            if plano.prev_sha in texto:
                estado["restore_tentado"] = True
                resposta = restore
            else:
                resposta = None
        elif "pip" in texto and "install" in texto:
            estado["instalacoes"] += 1
            resposta = instalacao if estado["instalacoes"] == 1 else None
        elif "import empiremuse.utils.client" in texto:
            resposta = smoke
        else:
            resposta = None
        if isinstance(resposta, BaseException):
            raise resposta
        if resposta is not None:
            return resposta
        return await run_exec(*args, **kwargs)

    runner.estado = estado
    return runner


def test_the_applying_mark_is_on_disk_before_the_first_checkout(mundo, python_falso, tmp_path):
    remoto, local = mundo
    plano = planejar(remoto, local)
    runner = roteiro(plano)

    aplicar(plano, local, python_falso, tmp_path, runner)

    marca = runner.estado["pendente_no_1o_checkout"]
    assert marca and marca["status"] == "applying" and marca["boots"] == 0 and marca["prev_sha"] == plano.prev_sha
    assert marca["flow"] == "git"


def test_a_failing_restore_keeps_the_applying_mark_and_raises_rollback_failed(mundo, python_falso, tmp_path):
    remoto, local = mundo
    plano = planejar(remoto, local)
    runner = roteiro(plano, smoke=git_flow.ExecResult(1, "falhou"), restore=git_flow.ExecResult(1, "falhou"))

    with pytest.raises(git_flow.UpdateRollbackFailed) as erro:
        aplicar(plano, local, python_falso, tmp_path, runner)

    assert isinstance(erro.value, UpdateError) and "próximo início" in str(erro.value)
    assert isinstance(erro.value.__cause__, UpdateError)
    assert head(local) == plano.target_commit  # o clone ficou no código novo...
    assert pendente_em_disco(tmp_path)["status"] == "applying"  # ...e a marca fica para o boot_guard reverter


def test_a_cancellation_with_a_failing_restore_keeps_the_mark_and_reraises_the_cancellation(
    mundo, python_falso, tmp_path
):
    remoto, local = mundo
    plano = planejar(remoto, local)
    runner = roteiro(plano, smoke=asyncio.CancelledError(), restore=git_flow.ExecResult(1, "falhou"))

    with pytest.raises(asyncio.CancelledError):
        aplicar(plano, local, python_falso, tmp_path, runner)

    assert runner.estado["restore_tentado"] and pendente_em_disco(tmp_path)["status"] == "applying"


def test_a_double_cancellation_never_leaves_new_code_without_a_pending_mark(mundo, python_falso, tmp_path):
    """Cancelamento no install e de novo no restore: ou o restore terminou, ou a marca ``applying`` ficou no disco."""
    remoto, local = mundo
    plano = planejar(remoto, local)
    runner = roteiro(plano, instalacao=asyncio.CancelledError(), restore=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        aplicar(plano, local, python_falso, tmp_path, runner)

    arquivo = tmp_path / "runtime" / "update-pending.json"
    assert head(local) == plano.prev_sha or (arquivo.exists() and pendente_em_disco(tmp_path)["status"] == "applying")


def test_an_unexpected_error_with_a_failing_restore_raises_rollback_failed_and_keeps_the_mark(
    mundo, python_falso, tmp_path
):
    """Só o cancelamento sobe como cancelamento; outro erro com restore falho vira ``UpdateRollbackFailed`` (o comando reinicia)."""
    remoto, local = mundo
    plano = planejar(remoto, local)
    runner = roteiro(plano, smoke=RuntimeError("boom"), restore=git_flow.ExecResult(1, "falhou"))

    with pytest.raises(git_flow.UpdateRollbackFailed) as erro:
        aplicar(plano, local, python_falso, tmp_path, runner)

    assert "boom" not in str(erro.value) and "próximo início" in str(erro.value)
    assert isinstance(erro.value.__cause__, RuntimeError) and pendente_em_disco(tmp_path)["status"] == "applying"


def test_an_unexpected_error_with_a_good_restore_reraises_the_original_and_clears_the_mark(
    mundo, python_falso, tmp_path
):
    remoto, local = mundo
    plano = planejar(remoto, local)

    with pytest.raises(RuntimeError, match="boom"):
        aplicar(plano, local, python_falso, tmp_path, roteiro(plano, smoke=RuntimeError("boom")))

    assert head(local) == plano.prev_sha and not (tmp_path / "runtime" / "update-pending.json").exists()


def test_a_failed_restore_logs_the_original_failure_too_when_it_is_a_safe_update_error(
    mundo, python_falso, tmp_path, caplog
):
    remoto, local = mundo
    plano = planejar(remoto, local)
    runner = roteiro(plano, smoke=git_flow.ExecResult(1, "falhou"), restore=git_flow.ExecResult(1, "falhou"))

    with caplog.at_level(logging.ERROR), pytest.raises(git_flow.UpdateRollbackFailed):
        aplicar(plano, local, python_falso, tmp_path, runner)

    erros = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any(plano.prev_sha in m and "o teste de fumaça do código novo falhou" in m for m in erros), erros


# ============================================================================ A12c (D10): restore robusto, em repositórios git reais

ARQUIVOS_DO_ALVO = [
    "novo/sub/n.txt",
    "r_novo.txt",
    "com espaço.txt",
    "linha\nquebrada.txt",
    "bin/tool.sh",
    "ligacao",
    "script.bat",
]


class CenarioRestore:
    """``prev`` e ``alvo`` reais; ``interromper()`` deixa HEAD e índice no ``prev`` com os arquivos do ``alvo`` na árvore (checkout interrompido)."""

    def __init__(self, tmp_path: Path) -> None:
        self.repo = tmp_path / "clone"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "dir_antigo").mkdir()
        self.prev = commit(
            self.repo,
            {
                "a.txt": "a1\n",
                "b.txt": "b1\n",
                "r_antigo.txt": "conteúdo que será renomeado\n",
                "dir_antigo/d.txt": "d\n",
                ".gitignore": "ignorado.log\n",
                ".gitattributes": "*.bat eol=crlf\n",
            },
            "prev",
        )
        (self.repo / "a.txt").write_text("a2\n", encoding="utf-8")
        (self.repo / "b.txt").unlink()
        (self.repo / "r_antigo.txt").unlink()
        (self.repo / "novo" / "sub").mkdir(parents=True)
        for nome in ARQUIVOS_DO_ALVO[:4]:
            (self.repo / nome).write_text("do alvo\n", encoding="utf-8")
        (self.repo / "r_novo.txt").write_text("conteúdo que será renomeado\n", encoding="utf-8")
        (self.repo / "bin").mkdir()
        (self.repo / "bin" / "tool.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (self.repo / "bin" / "tool.sh").chmod(0o755)  # modo 100755 no alvo
        (self.repo / "ligacao").symlink_to("a.txt")  # modo 120000 no alvo
        (self.repo / "script.bat").write_bytes(b"linha1\nlinha2\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "alvo")
        self.alvo = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "script.bat").write_bytes(b"linha1\r\nlinha2\r\n")  # como o checkout do alvo grava (eol=crlf)
        self.python = tmp_path / "python-falso"
        self.python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.python.chmod(0o755)

    def interromper(self) -> None:
        git(self.repo, "reset", "-q", self.prev)  # HEAD e índice no prev; a árvore continua com os arquivos do alvo

    def restaurar(self, runner=run_exec):
        return asyncio.run(
            git_flow.restore_checkout(self.repo, self.prev, self.alvo, python=str(self.python), runner=runner)
        )

    def sujo(self) -> str:
        return git(self.repo, "status", "--porcelain", "--untracked-files=no")


@pytest.fixture
def restore(tmp_path, monkeypatch):
    instalar_uv_falso(tmp_path, monkeypatch)
    cenario = CenarioRestore(tmp_path)
    git(cenario.repo, "checkout", "-q", "--detach", cenario.alvo)
    cenario.interromper()
    return cenario


def test_after_an_interrupted_checkout_the_tree_is_exactly_the_previous_commit(restore):
    dono = restore.repo / "meu.txt"
    dono.write_text("do dono\n", encoding="utf-8")
    ignorado = restore.repo / "ignorado.log"
    ignorado.write_text("lixo ignorado\n", encoding="utf-8")
    assert restore.sujo()  # o cenário está mesmo meio aplicado (modificado e apagados)

    restore.restaurar()

    assert restore.sujo() == "" and git(restore.repo, "rev-parse", "HEAD") == restore.prev
    for nome in ARQUIVOS_DO_ALVO:
        assert not os.path.lexists(restore.repo / nome), nome
    assert not (restore.repo / "novo").exists() and not (restore.repo / "bin").exists()  # diretórios novos vazios somem
    assert (restore.repo / "dir_antigo" / "d.txt").exists() and (restore.repo / "a.txt").read_text() == "a1\n"
    assert dono.read_text() == "do dono\n" and ignorado.read_text() == "lixo ignorado\n"  # nunca git clean


def test_an_orphan_index_lock_file_is_removed_and_the_restore_passes(restore):
    (restore.repo / ".git" / "index.lock").write_text("", encoding="utf-8")

    restore.restaurar()

    assert not (restore.repo / ".git" / "index.lock").exists() and restore.sujo() == ""


def test_an_orphan_index_lock_symlink_loses_only_the_link_not_its_target(restore, tmp_path):
    alvo = tmp_path / "fora.txt"
    alvo.write_text("não mexa\n", encoding="utf-8")
    (restore.repo / ".git" / "index.lock").symlink_to(alvo)

    restore.restaurar()

    assert not os.path.lexists(restore.repo / ".git" / "index.lock")
    assert alvo.read_text() == "não mexa\n" and restore.sujo() == ""


def test_a_directory_swapped_for_a_symlink_out_of_the_repo_is_skipped_with_a_warning_and_the_restore_passes(
    restore, tmp_path, caplog
):
    fora = tmp_path / "fora"
    (fora / "sub").mkdir(parents=True)
    (fora / "sub" / "n.txt").write_text("de fora\n", encoding="utf-8")
    shutil.rmtree(restore.repo / "novo")
    (restore.repo / "novo").symlink_to(fora)  # o caminho novo/sub/n.txt do diff passa a resolver para fora do repo

    with caplog.at_level(logging.WARNING):
        restore.restaurar()  # não levanta: o que está fora do clone não é problema do restore

    assert (fora / "sub" / "n.txt").read_text() == "de fora\n"  # nunca seguiu o link
    assert "novo/sub/n.txt" in caplog.text and restore.sujo() == ""


def test_a_failing_diff_makes_the_restore_fail(restore):
    async def runner(*args, **kwargs):
        if "diff" in [str(a) for a in args]:
            return git_flow.ExecResult(1, "falhou")
        return await run_exec(*args, **kwargs)

    with pytest.raises(UpdateError):
        restore.restaurar(runner)


def test_the_restore_asks_git_for_the_files_the_target_added_without_following_renames(restore):
    chamadas: list[list[str]] = []

    async def runner(*args, **kwargs):
        chamadas.append([str(a) for a in args])
        return await run_exec(*args, **kwargs)

    restore.restaurar(runner)

    (diff,) = [c for c in chamadas if "diff" in c and "--name-only" in c]
    assert {"--name-only", "--no-renames", "--diff-filter=A", "-z"} <= set(diff)
    assert diff[-2:] == [restore.prev, restore.alvo]
    forcado = [c for c in chamadas if "checkout" in c]
    assert forcado and "--force" in forcado[0] and "--detach" in forcado[0] and restore.prev in forcado[0]
    assert not any("clean" in c for c in chamadas)  # nunca git clean


# ---------------------------------------------------------------------------- _reap: SIGTERM, 5 s, SIGKILL


async def lancar_e_cancelar(codigo_do_filho: str, *argumentos: str, antes_de_cancelar=None):
    """Roda ``run_exec`` com um filho real, espera o processo existir, cancela e devolve o ``Process`` já reapado."""
    capturado: list[asyncio.subprocess.Process] = []
    criado = asyncio.Event()
    original = asyncio.create_subprocess_exec

    async def espiao(*args, **kwargs):
        processo = await original(*args, **kwargs)
        capturado.append(processo)
        criado.set()
        return processo

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(asyncio, "create_subprocess_exec", espiao)
        tarefa = asyncio.create_task(
            run_exec(sys.executable, "-c", codigo_do_filho, *argumentos, cwd=None, env=os.environ, timeout=600)
        )
        await asyncio.wait_for(criado.wait(), 30)
        if antes_de_cancelar:
            await antes_de_cancelar()
        tarefa.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarefa
    processo = capturado[0]
    if processo.returncode is None:  # não deixa o filho de pé se a correção faltar
        processo.kill()
        await processo.wait()
    return processo


def test_a_child_that_exits_on_sigterm_is_never_sent_sigkill():
    async def principal():
        vivo_ao_cancelar = []
        processo = await lancar_e_cancelar("import time; time.sleep(60)")
        vivo_ao_cancelar.append(processo.returncode)
        return processo

    processo = asyncio.run(principal())

    assert processo.returncode == -15  # SIGTERM; com SIGKILL seria -9


def test_a_child_that_ignores_sigterm_gets_sigkill_after_the_grace_period(tmp_path, monkeypatch):
    monkeypatch.setattr(git_flow, "REAP_GRACE_SECONDS", 0.3)  # a constante é lida em tempo de chamada
    marca = tmp_path / "pronto"
    codigo = "import signal, sys, time, pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN); pathlib.Path(sys.argv[1]).write_text('x'); time.sleep(60)"

    async def pronto():
        while not marca.exists():  # o filho só avisa depois de instalar o handler: sem isso o SIGTERM o mataria
            await asyncio.sleep(0)

    async def principal():
        return await lancar_e_cancelar(codigo, str(marca), antes_de_cancelar=lambda: asyncio.wait_for(pronto(), 30))

    processo = asyncio.run(principal())

    assert processo.returncode == -9  # SIGKILL, depois da tolerância


# ---------------------------------------------------------------------------- Médio do Vigil: arquivo do dono num caminho que o alvo acrescenta


def trocar_alvo(remoto: Path, arquivos: dict[str, str]) -> None:
    """Refaz a tag ``v2.1.0`` com um commit que também ACRESCENTA ``arquivos``."""
    git(remoto, "tag", "-d", "v2.1.0")
    for nome in arquivos:
        (remoto / nome).parent.mkdir(parents=True, exist_ok=True)
    commit(remoto, arquivos, "v2.1.0 com arquivos novos")
    git(remoto, "tag", "-a", "v2.1.0", "-m", "2.1.0")


def plano_recusado(mundo, arquivos: dict[str, str], preparar) -> str:
    remoto, local = mundo
    trocar_alvo(remoto, arquivos)
    preparar(local)
    antes = head(local)

    with pytest.raises(UpdateError) as erro:
        planejar(remoto, local)

    assert head(local) == antes
    assert not (local.parent / "runtime" / "update-pending.json").exists()
    return str(erro.value)


FRASE = "A versão 2.1.0 acrescenta {caminho}, que já existe no seu clone sem estar versionado; mova-o e tente de novo"


def test_the_plan_refuses_when_an_untracked_owner_file_sits_on_a_path_the_target_adds(mundo):
    def preparar(local: Path) -> None:
        (local / "empiremuse_cogs").mkdir()
        (local / "empiremuse_cogs" / "nova.py").write_text("do dono\n", encoding="utf-8")

    mensagem = plano_recusado(mundo, {"empiremuse_cogs/nova.py": "do alvo\n"}, preparar)

    assert FRASE.format(caminho="empiremuse_cogs/nova.py") in mensagem


def test_the_plan_refuses_when_an_ignored_owner_file_sits_on_a_path_the_target_adds(mundo):
    def preparar(local: Path) -> None:
        (local / ".git" / "info" / "exclude").write_text("ignorado.txt\n", encoding="utf-8")
        (local / "ignorado.txt").write_text("do dono\n", encoding="utf-8")
        assert git(local, "status", "--porcelain") == ""  # de fato ignorado: o git não o vê

    mensagem = plano_recusado(mundo, {"ignorado.txt": "do alvo\n"}, preparar)

    assert FRASE.format(caminho="ignorado.txt") in mensagem


def test_the_plan_refuses_when_a_symlink_sits_on_a_path_the_target_adds(mundo):
    def preparar(local: Path) -> None:
        (local / "ligacao.txt").symlink_to("/caminho/que/nao/existe")  # mesmo pendurado, o caminho está ocupado

    mensagem = plano_recusado(mundo, {"ligacao.txt": "do alvo\n"}, preparar)

    assert FRASE.format(caminho="ligacao.txt") in mensagem


def test_the_plan_refuses_when_a_parent_directory_of_an_added_path_is_an_untracked_symlink_out_of_the_clone(
    mundo, tmp_path
):
    fora = tmp_path / "fora_do_clone"
    fora.mkdir()

    def preparar(local: Path) -> None:
        (local / "empiremuse_cogs").symlink_to(
            fora
        )  # cogs/ vira link NÃO rastreado para fora; o arquivo em si não existe

    mensagem = plano_recusado(mundo, {"empiremuse_cogs/nova.py": "do alvo\n"}, preparar)

    assert FRASE.format(caminho="empiremuse_cogs/nova.py") in mensagem
    assert list(fora.iterdir()) == []  # nada foi escrito lá


def test_the_plan_refuses_when_a_parent_of_an_added_path_is_a_regular_file(mundo):
    def preparar(local: Path) -> None:
        (local / "pai").write_text("sou um arquivo, não um diretório\n", encoding="utf-8")

    mensagem = plano_recusado(mundo, {"pai/filho.txt": "do alvo\n"}, preparar)

    assert FRASE.format(caminho="pai/filho.txt") in mensagem


# ---------------------------------------------------------------------------- restore: só apaga o que é EXATAMENTE o do alvo


def owner_file(restore, nome: str, conteudo: str = "do dono\n") -> Path:
    caminho = restore.repo / nome
    caminho.write_text(conteudo, encoding="utf-8")
    return caminho


def test_an_owner_file_with_other_content_on_an_added_path_is_kept_and_logged(restore, caplog):
    dono = owner_file(restore, "com espaço.txt")  # o alvo acrescentou o MESMO caminho, com outro conteúdo

    with caplog.at_level(logging.WARNING):
        restore.restaurar()  # o restore passa

    assert dono.read_text() == "do dono\n"  # intacto
    assert "com espaço.txt" in caplog.text
    for nome in (
        "novo/sub/n.txt",
        "r_novo.txt",
        "bin/tool.sh",
        "ligacao",
    ):  # os idênticos ao alvo continuam sendo removidos
        assert not os.path.lexists(restore.repo / nome), nome
    assert restore.sujo() == "" and git(restore.repo, "rev-parse", "HEAD") == restore.prev


def test_an_executable_target_file_that_lost_its_execute_bit_is_treated_as_the_owners(restore, caplog):
    ferramenta = restore.repo / "bin" / "tool.sh"
    ferramenta.chmod(0o644)  # mesmo conteúdo do alvo, mas o modo difere do 100755

    with caplog.at_level(logging.WARNING):
        restore.restaurar()

    assert ferramenta.exists() and "bin/tool.sh" in caplog.text


def test_a_regular_file_where_the_target_has_a_symlink_is_kept(restore, caplog):
    (restore.repo / "ligacao").unlink()
    dono = owner_file(restore, "ligacao", "arquivo do dono\n")

    with caplog.at_level(logging.WARNING):
        restore.restaurar()

    assert dono.read_text() == "arquivo do dono\n" and "ligacao" in caplog.text


def test_a_bat_file_written_with_crlf_by_the_checkout_is_still_recognized_as_the_targets_and_removed(restore):
    """O .gitattributes tem ``*.bat eol=crlf``: o arquivo na árvore tem CRLF e o hash cru dos bytes nunca bateria com o blob (LF)."""
    assert (restore.repo / "script.bat").read_bytes() == b"linha1\r\nlinha2\r\n"

    restore.restaurar()

    assert not os.path.lexists(restore.repo / "script.bat")


# ---------------------------------------------------------------------------- restore: o .gitattributes é do PRÓPRIO alvo (a regra some depois do checkout do prev)


class CenarioGitattributesDoAlvo:
    """prev com ``.gitattributes`` sem regra; o alvo o ALTERA para ``*.bat eol=crlf`` e acrescenta ``script.bat``.
    A interrupção deixa HEAD/índice no prev, o ``.gitattributes`` do alvo e o ``script.bat`` com CRLF (como o checkout do alvo gravou) na árvore."""

    def __init__(self, tmp_path: Path) -> None:
        self.repo = tmp_path / "clone_attr"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        self.prev = commit(self.repo, {"a.txt": "a1\n", ".gitattributes": "# sem regras\n"}, "prev")
        self.alvo = commit(self.repo, {".gitattributes": "*.bat eol=crlf\n", "script.bat": "linha1\nlinha2\n"}, "alvo")
        git(self.repo, "reset", "-q", self.prev)  # HEAD/índice no prev; a árvore segue com os arquivos do alvo
        (self.repo / "script.bat").write_bytes(b"linha1\r\nlinha2\r\n")
        self.python = tmp_path / "python-falso-attr"
        self.python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.python.chmod(0o755)

    def restaurar(self):
        return asyncio.run(
            git_flow.restore_checkout(self.repo, self.prev, self.alvo, python=str(self.python), runner=run_exec)
        )


@pytest.fixture
def attr_do_alvo(tmp_path, monkeypatch):
    instalar_uv_falso(tmp_path, monkeypatch)
    return CenarioGitattributesDoAlvo(tmp_path)


def test_a_crlf_file_written_under_the_targets_own_gitattributes_is_still_the_targets_and_is_removed(attr_do_alvo):
    """Depois do ``checkout --force prev`` a regra ``eol=crlf`` já não vale: o hash com filtros dos bytes CRLF não bate com o blob (LF)."""
    attr_do_alvo.restaurar()

    assert not os.path.lexists(attr_do_alvo.repo / "script.bat")
    assert (attr_do_alvo.repo / ".gitattributes").read_text() == "# sem regras\n"  # o do prev voltou
    assert git(attr_do_alvo.repo, "status", "--porcelain", "--untracked-files=no") == ""
