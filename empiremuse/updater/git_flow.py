"""Atualização por git (hosts fora de container): escolhe a tag anotada oficial, confere, troca o código e reverte se falhar.

Só stdlib no import (o ``boot_guard`` usa este módulo antes de o bot e as dependências carregarem). Nada passa por shell:
todo processo sai de ``run_exec`` com lista de argumentos, ambiente mínimo (sem ``TOKEN``, ``MONGO`` nem ``GIT_*`` herdados)
e saída truncada e mascarada. Todo git roda com ``--git-dir``/``--work-tree`` explícitos e opções de segurança (só o
protocolo permitido, sem hooks, sem fsmonitor, sem credential helper, ``fsckObjects``); a URL é sempre a oficial
(``branding.SOURCE_REPO_GIT``), nunca ``SOURCE_REPO`` nem o ``origin`` do clone. O plano só lê (``ls-remote``, ``fetch`` de
tag, ``rev-parse``…) e não mexe no HEAD; quem troca o código é ``apply_git_update``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import logging
import os
import re
import shutil
import stat
import sys
import time
import tomllib
import urllib.request
import http.client
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from empiremuse import paths
from empiremuse.rules.owner_shell import collect_secret_values, mask_secrets
from empiremuse.rules.release import (
    Version,
    changelog_sections,
    parse_ls_remote_annotated,
    parse_tag,
    pick_target,
)
from empiremuse.updater.state import (
    PendingUpdate,
    SeenTagsError,
    UpdateRequest,
    clear_pending,
    read_seen_tags,
    write_pending,
    write_seen_tags,
)
from empiremuse.utils import branding

logger = logging.getLogger(__name__)

EXIT_RESTART = 75
MAIN_REF = "refs/remotes/official/main"
LOCK_FILE = "requirements-lock.txt"
BUILD_CONSTRAINTS_FILE = "build-constraints.txt"
CHANGELOG_FILE = "CHANGELOG.md"
CHANGELOG_MAX_CHARS = 3500
MAX_CHANGELOG_BYTES = 1024 * 1024
RAW_BASE = "https://raw.githubusercontent.com/"
GIT_TIMEOUT = 60
GIT_NETWORK_TIMEOUT = 300
INSTALL_TIMEOUT = 600
SMOKE_TIMEOUT = 120
MAX_OUTPUT_CHARS = 4000
MAX_LISTED_FILES = 10
MAX_LISTED_CONFLICTS = 5

_SHA1 = re.compile(r"[0-9a-f]{40}")
_UNFINISHED_OPERATIONS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")
_WINDOWS_ENV_KEYS = ("SystemRoot", "COMSPEC", "TEMP", "TMP", "USERPROFILE")


class UpdateError(Exception):
    """Falha esperada do update; a mensagem (pt-BR) é segura para ir ao Discord."""


class UpdateRollbackFailed(UpdateError):
    """A atualização falhou e a reversão também: o pendente ``applying`` fica e o ``boot_guard`` reverte no próximo início."""


@dataclasses.dataclass(frozen=True)
class ExecResult:
    returncode: int
    output: str


Runner = Callable[..., Awaitable[ExecResult]]


@dataclasses.dataclass(frozen=True)
class GitPlan:
    """O que o update vai fazer, já conferido: de ``current`` para ``target`` (commit ``target_commit`` da tag anotada)."""

    current: Version
    target: Version
    tag_object_sha: str
    target_commit: str
    prev_sha: str
    changelog: str


def _base_env(base: Mapping[str, str], extra: Mapping[str, str]) -> dict[str, str]:
    keys = ("PATH", "HOME", "LANG") + (_WINDOWS_ENV_KEYS if os.name == "nt" else ())
    env = {key: base[key] for key in keys if key in base}
    env.update(extra)
    return env


def git_env(base: Mapping[str, str]) -> dict[str, str]:
    """Ambiente do git: só ``PATH``, ``HOME`` e ``LANG`` do ``base`` mais as travas; nada de ``GIT_DIR`` nem de token."""
    return _base_env(base, {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})


def minimal_env(base: Mapping[str, str]) -> dict[str, str]:
    """Ambiente do ``pip``/``uv`` e do smoke: o mesmo mínimo, sem as variáveis ``GIT_*``; o sdist não vê ``TOKEN`` nem ``MONGO``."""
    return _base_env(base, {})


def git_command(repo_dir: Path | None, allow_protocol: str, *args: str) -> list[str]:
    """``git`` apontado para ``repo_dir`` (ou sem repositório, para ``ls-remote``) com as opções de segurança; ``args`` começa pelo subcomando."""
    options = (
        "protocol.allow=never",
        f"protocol.{allow_protocol}.allow=always",
        "core.hooksPath=/dev/null",
        "http.sslVerify=true",
        "credential.helper=",
        "core.fsmonitor=false",
        "transfer.fsckObjects=true",
    )
    command = ["git"]
    if repo_dir is not None:
        command += [f"--git-dir={repo_dir}/.git", f"--work-tree={repo_dir}"]
    for option in options:
        command += ["-c", option]
    return command + list(args)


REAP_GRACE_SECONDS = 5  # tolerância entre o SIGTERM e o SIGKILL; lida em tempo de chamada (os testes a encurtam)


async def _reap(process: asyncio.subprocess.Process) -> None:
    """Encerra o processo (SIGTERM, espera ``REAP_GRACE_SECONDS``, só então SIGKILL) e aguarda o término.

    O git que recebe SIGTERM apaga o próprio ``index.lock``; o SIGKILL o deixaria órfão e a árvore pela metade.
    """
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), REAP_GRACE_SECONDS)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()  # cancelado de novo durante a tolerância: sem tempo para esperar, mas sem filho de pé
            raise
    await process.wait()


async def run_exec(
    *args: str,
    cwd: str | os.PathLike[str] | None,
    env: Mapping[str, str],
    timeout: float,
    max_output: int = MAX_OUTPUT_CHARS,
) -> ExecResult:
    """Roda ``args`` sem shell. Saída (stdout e stderr juntos) mascarada e truncada nos últimos ``max_output`` caracteres.

    Falha ao iniciar vira o código 127 e tempo esgotado, 124.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            env=dict(env),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as error:
        return ExecResult(127, f"não foi possível executar {os.path.basename(str(args[0]))}: {error.strerror}")
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        await _reap(process)
        return ExecResult(124, "tempo esgotado")
    except asyncio.CancelledError:
        await _reap(process)  # cancelado (ex.: desligamento): o git/pip não pode seguir vivo sem dono
        raise
    text = mask_secrets(stdout.decode("utf-8", errors="replace"), collect_secret_values(os.environ))
    return ExecResult(process.returncode or 0, text[-max_output:])


class _Git:
    """``git`` sobre um clone: monta o comando seguro, usa o ambiente mínimo e traduz falha em ``UpdateError``."""

    def __init__(self, repo_dir: Path, allow_protocol: str, runner: Runner) -> None:
        self._repo_dir = repo_dir
        self._allow_protocol = allow_protocol
        self._runner = runner

    async def __call__(
        self, *args: str, error: str | None = None, timeout: float = GIT_TIMEOUT, max_output: int | None = None
    ) -> ExecResult:
        """Roda o git; com ``error`` uma saída diferente de 0 levanta ``UpdateError(error)`` (o detalhe vai só para o log).

        ``max_output`` (para arquivos lidos com ``git show``) só é repassado ao runner quando pedido: o padrão guarda o fim
        da saída e cortaria o começo de um arquivo grande.
        """
        command = git_command(self._repo_dir, self._allow_protocol, *args)
        extra = {} if max_output is None else {"max_output": max_output}
        result = await self._runner(*command, cwd=self._repo_dir, env=git_env(os.environ), timeout=timeout, **extra)
        if error is not None and result.returncode != 0:
            logger.warning("update: git %s falhou (código %s): %s", args[0], result.returncode, result.output)
            raise UpdateError(error)
        return result

    async def single_line(self, *args: str, error: str) -> str:
        result = await self(*args, error=error)
        lines = result.output.strip().splitlines()
        return lines[-1].strip() if lines else ""

    async def sha(self, revision: str, *, error: str) -> str:
        value = await self.single_line("rev-parse", "--verify", "--end-of-options", revision, error=error)
        if not _SHA1.fullmatch(value):
            raise UpdateError(error)
        return value


def _printable(text: str) -> str:
    return "".join(char if char.isprintable() and char != "`" else "?" for char in text)


async def _current_version(git: _Git) -> Version:
    for args in (
        ("describe", "--tags", "--exact-match", "--match", "v[0-9]*"),
        ("describe", "--tags", "--abbrev=0", "--match", "v[0-9]*"),
    ):
        result = await git(*args)
        if result.returncode == 0 and (version := parse_tag(result.output.strip())) is not None:
            return version
    raise UpdateError("Não consegui descobrir a versão atual: este clone não está em nenhuma tag `vX.Y.Z`.")


async def latest_official_version(
    repo_url: str = branding.SOURCE_REPO_GIT, *, allow_protocol: str = "https", runner: Runner = run_exec
) -> Version | None:
    """Maior tag anotada oficial (``ls-remote`` não precisa de clone), ou ``None`` se a consulta falhar."""
    command = git_command(None, allow_protocol, "ls-remote", "--tags", "--end-of-options", repo_url)
    result = await runner(
        *command, cwd=None, env=git_env(os.environ), timeout=GIT_NETWORK_TIMEOUT, max_output=MAX_CHANGELOG_BYTES
    )
    if result.returncode != 0:
        return None
    return max(parse_ls_remote_annotated(result.output), default=None)


def fetch_remote_changelog(tag: str, *, timeout: float = 10) -> str:
    """``CHANGELOG.md`` da tag oficial (bloqueante: rode em executor). Vazio se a tag for inválida ou a consulta falhar."""
    raw_base = branding.REPO_URL.replace("https://github.com/", RAW_BASE, 1)
    if parse_tag(tag) is None or not raw_base.startswith(RAW_BASE):
        return ""
    url = f"{raw_base}/{tag}/{CHANGELOG_FILE}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - URL https fixa, host conferido abaixo
            if not response.geturl().startswith(RAW_BASE):
                return ""
            return response.read(MAX_CHANGELOG_BYTES).decode("utf-8", errors="replace")
    except OSError, ValueError, http.client.HTTPException:
        return ""


async def installed_version(repo_dir: Path, *, runner: Runner = run_exec) -> Version | None:
    """Versão do clone em ``repo_dir`` (tag no HEAD ou a mais próxima), ou ``None`` se não der para descobrir."""
    try:
        return await _current_version(_Git(Path(repo_dir), "https", runner))
    except UpdateError:
        return None


def _python_supported(pyproject: str) -> bool:
    """``requires-python`` do ``pyproject.toml`` da tag atendido pelo interpretador atual (sem a chave: sem restrição)."""
    from packaging.specifiers import InvalidSpecifier, SpecifierSet

    try:
        requires = tomllib.loads(pyproject).get("project", {}).get("requires-python")
        if requires is None:
            return True
        current = ".".join(str(part) for part in sys.version_info[:3])
        return SpecifierSet(requires).contains(current, prereleases=True)
    except tomllib.TOMLDecodeError, InvalidSpecifier, TypeError, AttributeError:
        raise UpdateError("Não consegui ler o `requires-python` da versão nova; a atualização foi recusada.") from None


async def _require_clean_tree(git: _Git, repo_dir: Path) -> None:
    for name in _UNFINISHED_OPERATIONS:
        if (repo_dir / ".git" / name).exists():
            raise UpdateError(
                "Há um merge, rebase ou cherry-pick em andamento neste clone; conclua-o antes de atualizar."
            )
    # Só arquivos rastreados: o que o bot cria no projeto (dados, .env) não bloqueia o checkout; um conflito real o faria falhar e reverter.
    result = await git("status", "--porcelain", "--untracked-files=no", error="Não consegui ler o estado do clone.")
    changed = [line[3:] for line in result.output.splitlines() if len(line) > 3]
    if changed:
        listed = ", ".join(f"`{_printable(name)}`" for name in changed[:MAX_LISTED_FILES])
        more = f" e mais {len(changed) - MAX_LISTED_FILES}" if len(changed) > MAX_LISTED_FILES else ""
        raise UpdateError(
            f"Há alterações locais em arquivos rastreados: {listed}{more}. Descarte-as ou faça commit antes de atualizar."
        )


async def _added_paths(git: _Git, base: str, target_commit: str) -> list[str]:
    """Caminhos que ``target_commit`` acrescenta em relação a ``base`` (``diff --diff-filter=A -z``, sem seguir renomeios)."""
    result = await git(
        "diff",
        "--name-only",
        "--no-renames",
        "--diff-filter=A",
        "-z",
        base,
        target_commit,
        error="Não consegui listar os arquivos que a versão nova acrescenta.",
        max_output=MAX_CHANGELOG_BYTES,
    )
    return [name for name in result.output.split("\0") if name]


def _blocks(path: Path, repo_dir: Path) -> bool:
    """Já há algo no caminho (inclusive link pendurado) ou um diretório pai do caminho, no clone, que não é um diretório real."""
    if os.path.lexists(path):
        return True
    for parent in path.parents:
        if parent == repo_dir:
            break
        if os.path.lexists(parent) and (os.path.islink(parent) or not os.path.isdir(parent)):
            return True
    return False


async def _require_no_conflicting_paths(git: _Git, repo_dir: Path, prev_sha: str, target_commit: str, target) -> None:
    """Recusa o update se a versão nova acrescenta um caminho em que o dono já tem algo (não rastreado, ignorado ou link).

    O checkout abortaria (ou, num arquivo ignorado, o sobrescreveria) e o restore poderia levar embora o que é do dono.
    """
    repo = Path(repo_dir)
    conflicts = [name for name in await _added_paths(git, prev_sha, target_commit) if _blocks(repo / name, repo)]
    if not conflicts:
        return
    shown = [_printable(name) for name in conflicts[:MAX_LISTED_CONFLICTS]]
    message = (
        f"A versão {target} acrescenta {shown[0]}, que já existe no seu clone sem estar versionado; "
        "mova-o e tente de novo."
    )
    if len(conflicts) > 1:
        others = ", ".join(shown[1:])
        more = f" …e mais {len(conflicts) - MAX_LISTED_CONFLICTS}" if len(conflicts) > MAX_LISTED_CONFLICTS else ""
        message += f" Outros caminhos no mesmo caso: {others}{more}."
    raise UpdateError(message)


async def plan_git_update(
    repo_dir: Path,
    repo_url: str = branding.SOURCE_REPO_GIT,
    *,
    allow_protocol: str = "https",
    runner: Runner = run_exec,
) -> GitPlan | None:
    """Plano para a maior tag anotada oficial acima da atual, ou ``None`` se já está na última. Não mexe no HEAD."""
    repo_dir = Path(repo_dir)
    git = _Git(repo_dir, allow_protocol, runner)
    current = await _current_version(git)

    listing = await git(
        "ls-remote",
        "--tags",
        "--end-of-options",
        repo_url,
        error="Não consegui consultar as versões no repositório oficial.",
        timeout=GIT_NETWORK_TIMEOUT,
        max_output=MAX_CHANGELOG_BYTES,
    )
    annotated = parse_ls_remote_annotated(listing.output)
    target = pick_target(current, annotated)
    if target is None:
        return None
    tag, tag_object_sha = f"v{target}", annotated[target]

    await git(
        "fetch",
        "--no-tags",
        "--end-of-options",
        repo_url,
        f"+refs/heads/main:{MAIN_REF}",
        f"+refs/tags/{tag}:refs/tags/{tag}",
        error=f"Não consegui baixar a versão {target} do repositório oficial.",
        timeout=GIT_NETWORK_TIMEOUT,
    )
    refused = f"A tag {tag} não passou na conferência e a atualização foi recusada."
    if await git.sha(f"refs/tags/{tag}", error=refused) != tag_object_sha:
        raise UpdateError(refused)
    if await git.single_line("cat-file", "-t", "--end-of-options", f"refs/tags/{tag}", error=refused) != "tag":
        raise UpdateError(refused)
    # O campo "tag" do objeto (não o texto da mensagem, que pode ser grande e conter qualquer linha).
    named = await git.single_line(
        "for-each-ref", "--format=%(tag)", "--end-of-options", f"refs/tags/{tag}", error=refused
    )
    if named != tag:
        raise UpdateError(refused)
    target_commit = await git.sha(f"refs/tags/{tag}^{{commit}}", error=refused)
    ancestor = await git("merge-base", "--is-ancestor", "--end-of-options", target_commit, MAIN_REF)
    if ancestor.returncode != 0:
        raise UpdateError(f"A tag {tag} não está na `main` oficial; a atualização foi recusada.")

    runtime_dir = paths.get_runtime_dir()
    try:
        seen = read_seen_tags(runtime_dir)
    except SeenTagsError as error:
        raise UpdateError(
            "Não consegui ler o histórico de versões (update-state.json); a atualização foi abortada e o arquivo não foi alterado."
            " Corrija ou apague esse arquivo e tente de novo."
        ) from error
    if seen.get(tag, tag_object_sha) != tag_object_sha:
        raise UpdateError(f"A tag {tag} mudou desde que foi vista pela primeira vez; a atualização foi recusada.")
    if tag not in seen:
        write_seen_tags(runtime_dir, {**seen, tag: tag_object_sha})

    pyproject = await git(
        "show", "--end-of-options", f"{target_commit}:pyproject.toml", error=refused, max_output=MAX_CHANGELOG_BYTES
    )
    if not _python_supported(pyproject.output):
        raise UpdateError(
            f"A versão {target} exige uma versão do Python maior que a deste servidor; a atualização foi recusada."
        )
    await _require_clean_tree(git, repo_dir)

    log = await git("show", "--end-of-options", f"{target_commit}:{CHANGELOG_FILE}", max_output=MAX_CHANGELOG_BYTES)
    changelog = changelog_sections(log.output, current, target, CHANGELOG_MAX_CHARS) if log.returncode == 0 else ""
    prev_sha = await git.sha("HEAD", error="Não consegui ler o commit atual do clone.")
    await _require_no_conflicting_paths(git, repo_dir, prev_sha, target_commit, target)
    return GitPlan(current, target, tag_object_sha, target_commit, prev_sha, changelog)


def _require_uv() -> str:
    """O ``uv`` é obrigatório: só ele confere o hash do backend de build dos sdists do lock (``--build-constraint``); o
    ``pip`` não confere, e instalar sem essa proteção reabriria a janela de supply chain que o lock fecha."""
    uv = shutil.which("uv")
    if not uv:
        raise UpdateError(
            "o uv não está no PATH do processo do bot (serviço systemd ou pm2 costuma ter um PATH mínimo, sem "
            "~/.local/bin); a atualização foi recusada porque só ele confere o hash do backend de build das "
            "dependências. Deixe o uv no PATH do processo e tente de novo."
        )
    return uv


async def _install_dependencies(runner: Runner, repo_dir: Path, python: str, *, constraints_required: bool) -> None:
    """``constraints_required``: na atualização o ``build-constraints.txt`` é obrigatório (falha fechada); só a reversão
    para uma árvore antiga, anterior a esse arquivo, tolera a falta dele (e avisa)."""
    command = [_require_uv(), "pip", "install", "--require-hashes", "-r", LOCK_FILE, "--python", python]
    if (repo_dir / BUILD_CONSTRAINTS_FILE).is_file():
        command += ["--build-constraint", BUILD_CONSTRAINTS_FILE]
    elif constraints_required:
        raise UpdateError(f"o código novo não traz o {BUILD_CONSTRAINTS_FILE}; a atualização foi recusada")
    else:
        logger.warning(
            "update: %s ausente no clone; o backend de build dos sdists não terá versão nem hash",
            BUILD_CONSTRAINTS_FILE,
        )
    result = await runner(*command, cwd=repo_dir, env=minimal_env(os.environ), timeout=INSTALL_TIMEOUT)
    if result.returncode != 0:
        logger.warning("update: instalação das dependências falhou (código %s): %s", result.returncode, result.output)
        raise UpdateError("a instalação das dependências falhou")


async def _require_constraints_in_target(git: _Git, target_commit: str) -> None:
    """O commit alvo precisa trazer o ``build-constraints.txt``: sem ele o backend de build dos sdists ficaria sem hash."""
    result = await git("cat-file", "-e", f"{target_commit}:{BUILD_CONSTRAINTS_FILE}")
    if result.returncode != 0:
        raise UpdateError(f"o código novo não traz o {BUILD_CONSTRAINTS_FILE}; a atualização foi recusada")


async def _same_install_inputs(git: _Git, prev_sha: str, target_commit: str) -> bool:
    """``True`` se o ``requirements-lock.txt`` é idêntico nos dois commits (``git diff --quiet``): o ambiente instalado já
    é o do commit anterior. O ``build-constraints.txt`` fica de fora de propósito: ele decide COMO uma roda é construída,
    não QUAIS pacotes a reversão precisa instalar. Qualquer dúvida (erro do git inclusive) vale ``False``: reinstala."""
    result = await git("diff", "--quiet", prev_sha, target_commit, "--", LOCK_FILE)
    return result.returncode == 0


async def _smoke_test(runner: Runner, repo_dir: Path, python: str) -> None:
    for args in (("-c", "import empiremuse.utils.client"), ("-m", "compileall", "-q", "empiremuse")):
        result = await runner(python, *args, cwd=repo_dir, env=minimal_env(os.environ), timeout=SMOKE_TIMEOUT)
        if result.returncode != 0:
            logger.warning("update: teste de fumaça falhou (código %s): %s", result.returncode, result.output)
            raise UpdateError("o teste de fumaça do código novo falhou")


def _remove_stale_index_lock(repo: Path) -> None:
    """Apaga o ``.git/index.lock`` órfão (arquivo ou link: só a entrada, nunca o alvo do link).

    Só é seguro porque nenhum git nosso está vivo: o ``run_exec`` sempre espera o filho terminar e o ``boot_guard`` roda
    antes de qualquer outro git.
    """
    lock = repo / ".git" / "index.lock"
    try:
        mode = os.lstat(lock).st_mode
    except FileNotFoundError:
        return
    except OSError as error:
        raise UpdateError("Não consegui inspecionar o trava do índice do git.") from error
    if stat.S_ISREG(mode) or stat.S_ISLNK(mode):
        try:
            lock.unlink()
        except OSError as error:
            raise UpdateError("Não consegui remover a trava órfã do índice do git.") from error


async def _target_tree(git: _Git, target_commit: str) -> dict[str, tuple[str, str]]:
    """Caminho → ``(modo, sha do blob)`` de todos os arquivos do alvo (``ls-tree -r -z``)."""
    result = await git(
        "ls-tree",
        "-r",
        "-z",
        "--end-of-options",
        target_commit,
        error="Não consegui ler a árvore da versão nova.",
        max_output=MAX_CHANGELOG_BYTES,
    )
    entries: dict[str, tuple[str, str]] = {}
    for entry in result.output.split("\0"):
        meta, _, name = entry.partition("\t")
        fields = meta.split()
        if name and len(fields) == 3:
            entries[name] = (fields[0], fields[2])
    return entries


async def _is_target_file(git: _Git, repo: Path, name: str, entry: tuple[str, str] | None) -> bool:
    """O que há no caminho é exatamente o arquivo (ou link) que o alvo acrescentou? Só assim o restore o apaga.

    Arquivo comum: o ``git hash-object`` (que aplica os filtros do caminho, como o ``eol=crlf`` do ``.gitattributes``) tem
    de dar o sha do blob do alvo e o bit de execução tem de bater com o modo. Link: o destino tem de ser o conteúdo do
    blob. Qualquer outra coisa (conteúdo do dono, tipo diferente, submódulo) fica onde está.
    """
    if entry is None:
        return False
    mode, sha = entry
    path = repo / name
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        if mode != "120000":
            return False
        raw = os.fsencode(os.readlink(path))
        return hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest() == sha  # noqa: S324 - é o hash de objeto do git
    if not stat.S_ISREG(info.st_mode) or mode not in ("100644", "100755"):
        return False
    if (mode == "100755") != bool(info.st_mode & stat.S_IXUSR):
        return False
    # Com os filtros do caminho (o ``eol=crlf`` do ``.gitattributes``) e sem eles: o restore roda DEPOIS do checkout do
    # commit anterior, e se o próprio alvo trouxe a regra ela já não vale mais na árvore.
    for flags in ((), ("--no-filters",)):
        result = await git("hash-object", *flags, "--end-of-options", name)
        if result.returncode == 0 and result.output.strip() == sha:
            return True
    try:
        content = path.read_bytes()
    except OSError:
        return False
    normalized = content.replace(b"\r\n", b"\n")  # o checkout do alvo gravou CRLF onde o blob tem LF
    return hashlib.sha1(b"blob %d\0" % len(normalized) + normalized).hexdigest() == sha  # noqa: S324 - hash de objeto do git


async def _remove_target_additions(git: _Git, repo: Path, names: list[str], target_commit: str) -> None:
    """Remove os arquivos que o alvo acrescentou e ainda estão na árvore, e os diretórios que ficaram vazios.

    Só apaga o que for idêntico ao alvo (``_is_target_file``); o que for do dono fica, com um aviso no log. Cada caminho é
    removido com ``unlink`` (que não segue link) e só se o diretório pai REAL ficar dentro do clone: um diretório trocado
    por link para fora é pulado, com aviso, e nada de fora é tocado. Caminho absoluto ou com ``..`` é recusado. Arquivos
    não rastreados e ignorados nunca são apagados por engano (nada de ``git clean``).
    """
    root = repo.resolve()
    tree: dict[str, tuple[str, str]] | None = None
    for name in names:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise UpdateError("O restore recusou um caminho fora do clone.")
        path = repo / relative
        parent = path.parent.resolve()
        if parent != root and root not in parent.parents:
            logger.warning("update: o restore pulou %s: o diretório dele sai do clone.", _printable(name))
            continue
        if not os.path.lexists(path):
            continue
        if tree is None:
            tree = await _target_tree(git, target_commit)
        if not await _is_target_file(git, repo, name, tree.get(name)):
            logger.warning("update: o restore deixou %s: não é o arquivo da versão nova.", _printable(name))
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise UpdateError("Não consegui remover um arquivo que a versão nova acrescentou.") from error
        for directory in path.parents:
            if directory == repo:
                break
            try:
                directory.rmdir()
            except OSError:
                break


async def restore_checkout(
    repo_dir: Path,
    prev_sha: str,
    target_commit: str,
    *,
    python: str = sys.executable,
    runner: Runner = run_exec,
    allow_protocol: str = "https",
) -> None:
    """Volta o clone ao commit anterior de forma que uma troca interrompida no meio não deixe código misturado.

    Ordem: (1) tira o ``index.lock`` órfão; (2) ``checkout --force --detach`` do ``prev_sha`` (seguro: o apply exige a
    árvore rastreada limpa) restaura modificados e apagados; (3) remove o que o alvo acrescentou
    (``diff --diff-filter=A prev alvo``), pois um checkout interrompido deixa esses arquivos para trás e o carregador de
    cogs os carregaria; (4) reinstala o lock anterior. Qualquer falha, inclusive a do ``diff``, levanta ``UpdateError``.
    """
    if not (_SHA1.fullmatch(prev_sha) and _SHA1.fullmatch(target_commit)):
        raise UpdateError("O commit anterior é inválido.")
    repo = Path(repo_dir)
    _remove_stale_index_lock(repo)
    git = _Git(repo, allow_protocol, runner)
    await git(
        "checkout", "--force", "--detach", "--end-of-options", prev_sha, error="Não consegui voltar ao commit anterior."
    )
    await _remove_target_additions(git, repo, await _added_paths(git, prev_sha, target_commit), target_commit)
    if await _same_install_inputs(git, prev_sha, target_commit):
        logger.info("update: lock igual nos dois commits; reinstalação dispensada na reversão")
        return  # sem reinstalar não precisa do uv: a reversão funciona num servidor que ainda não o tem
    await _install_dependencies(runner, repo, python, constraints_required=False)


async def apply_git_update(
    plan: GitPlan,
    request: UpdateRequest,
    repo_dir: Path,
    runtime_dir: Path,
    *,
    python: str = sys.executable,
    runner: Runner = run_exec,
    now: Callable[[], float] = time.time,
    allow_protocol: str = "https",
) -> PendingUpdate:
    """Troca o código pelo commit da tag, instala o lock e testa; qualquer falha reverte e levanta ``UpdateError``.

    O pendente ``applying`` (``prev_sha``, ``boots=0``) é gravado ANTES do primeiro ``checkout``: se o processo morrer ou
    a reversão falhar, a árvore meio aplicada não fica sem proteção e o ``boot_guard`` a reverte no próximo início. A
    reversão roda sob ``asyncio.shield`` e só um restore bem-sucedido apaga o pendente; restore falho levanta
    ``UpdateRollbackFailed`` (o chamador reinicia com ``EXIT_RESTART``). No sucesso o mesmo pendente passa a ``pending``
    (``boots=0``) e é devolvido; quem chamou encerra o pool com ``EXIT_RESTART``.
    """
    repo_dir = Path(repo_dir)
    if not (_SHA1.fullmatch(plan.target_commit) and _SHA1.fullmatch(plan.prev_sha)):
        raise UpdateError("O plano de atualização é inválido.")
    git = _Git(repo_dir, allow_protocol, runner)
    applying = PendingUpdate(
        flow="git",
        from_version=plan.current,
        to_version=plan.target,
        bot_id=request.bot_id,
        channel_id=request.channel_id,
        requested_by=request.requested_by,
        created_at=now(),
        prev_sha=plan.prev_sha,
        target_commit=plan.target_commit,
        boots=0,
        status="applying",
    )
    # Antes de gravar o pendente ou trocar a árvore: sem o uv, ou sem o build-constraints.txt no alvo, nada muda.
    _require_uv()
    await _require_constraints_in_target(git, plan.target_commit)
    write_pending(runtime_dir, applying)  # fora do try: se a escrita falhar, nenhum git roda e a árvore fica intacta
    try:
        await git(
            "checkout",
            "--detach",
            "--end-of-options",
            plan.target_commit,
            error="Não consegui trocar para o código novo.",
        )
        await _install_dependencies(runner, repo_dir, python, constraints_required=True)
        await _smoke_test(runner, repo_dir, python)
        pending = dataclasses.replace(applying, status="pending")
        write_pending(runtime_dir, pending)
    except UpdateError as failure:
        await _revert_or_keep(plan, repo_dir, runtime_dir, python, runner, allow_protocol, failure)
        raise UpdateError(
            f"A atualização falhou ({failure}); o código foi revertido para a versão {plan.current}."
        ) from failure
    except (Exception, asyncio.CancelledError) as failure:
        # Cancelamento, erro do disco...: o checkout novo não pode ficar aplicado sem proteção.
        await _revert_or_keep(plan, repo_dir, runtime_dir, python, runner, allow_protocol, failure)
        raise  # sempre a exceção original (nunca engole o CancelledError)
    return pending


def safe_failure(failure: BaseException) -> str:
    """Texto de log de uma falha: a mensagem só para ``UpdateError`` (escrita para o Discord, sem segredos); senão o tipo."""
    return str(failure) if isinstance(failure, UpdateError) else type(failure).__name__


async def _revert_or_keep(
    plan: GitPlan,
    repo_dir: Path,
    runtime_dir: Path,
    python: str,
    runner: Runner,
    allow_protocol: str,
    failure: BaseException,
) -> None:
    """Volta ao ``prev_sha`` sob ``asyncio.shield`` e, só com o restore OK, apaga o pendente ``applying``.

    Restore falho: o ``applying`` FICA (o ``boot_guard`` reverte no próximo início) e levanta ``UpdateRollbackFailed``;
    para um ``CancelledError`` o cancelamento é que continua subindo. Cancelado durante o restore: o restore segue em
    segundo plano e o ``applying`` fica no disco.
    """
    try:
        await asyncio.shield(
            restore_checkout(
                repo_dir, plan.prev_sha, plan.target_commit, python=python, runner=runner, allow_protocol=allow_protocol
            )
        )
    except asyncio.CancelledError:
        raise
    except Exception as restore_failure:
        # Sem exc_info: o traceback encadeado traria a mensagem da falha original, que pode citar a saída de um comando.
        logger.error(
            "update: a reversão para %s falhou (%s); falha original: %s",
            plan.prev_sha,
            safe_failure(restore_failure),
            safe_failure(failure),
        )
        if isinstance(failure, asyncio.CancelledError):
            raise failure from restore_failure
        detail = f" ({failure})" if isinstance(failure, UpdateError) else ""
        raise UpdateRollbackFailed(
            f"A atualização falhou{detail} e a reversão automática também. O bot volta para a versão {plan.current} "
            f"no próximo início; reiniciando agora. O clone pode estar na versão {plan.target} até lá."
        ) from failure
    clear_pending(runtime_dir)
