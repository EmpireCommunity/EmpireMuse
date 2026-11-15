"""A22 (item 18, P3) — o script do auxiliar (``HELPER_SCRIPT``, constante de ``empiremuse/updater/proxy.py``), rodado com ``sh`` de verdade.

Um ``docker`` falso no ``PATH`` grava as chamadas e responde conforme arquivos de controle; o compose é temporário. Argumentos, como o proxy
os passa: ``sh -euc SCRIPT updater <alvo> <projeto> <dir> <serviço> <repo>``. O ``docker image inspect`` falso entende ``--format`` com
``range`` (uma variável por linha) ou ``json`` (lista), e sem ``--format`` devolve o JSON completo; assim o teste vale para qualquer das
formas de ler ``Config.Env``. Códigos: 0 ok, 64 alvo, 65 compose, 66 pull, 67 downgrade, 68 versão da imagem, 70 rollback, 71 rollback falhou.
"""

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from empiremuse.updater.proxy import HELPER_SCRIPT

pytestmark = pytest.mark.critical

REPO = "empirecommunity/empiremuse"
ANTIGO = "sha256:" + "11" * 32
NOVO = "sha256:" + "22" * 32
PROXY_DIGEST = "sha256:" + "33" * 32
PREFIXO = "empiremuse-updater: "

DOCKER_FALSO = r"""#!/bin/sh
echo "$*" >> "$FAKE/calls.log"
ruido() { echo "ruido-do-docker-stdout"; echo "ruido-do-docker-stderr" >&2; }
ler_env() { cat "$FAKE/env"; }
case "$1" in
  pull)
    ruido
    [ -f "$FAKE/pull_falha" ] && exit 1
    exit 0 ;;
  image)
    formato=""
    while [ $# -gt 0 ]; do
      [ "$1" = "--format" ] && formato="$2"
      shift
    done
    case "$formato" in
      *range*) ler_env ;;
      *json*) printf '['; sep=""; while IFS= read -r v; do printf '%s"%s"' "$sep" "$v"; sep=","; done < "$FAKE/env"; printf ']\n' ;;
      *) printf '[{"Config":{"Env":['; sep=""; while IFS= read -r v; do printf '%s"%s"' "$sep" "$v"; sep=","; done < "$FAKE/env"; printf ']}}]\n' ;;
    esac
    exit 0 ;;
  compose)
    arquivo=""
    while [ $# -gt 0 ]; do
      [ "$1" = "-f" ] && arquivo="$2"
      case "$1" in config|up) break ;; esac
      shift
    done
    sub="$1"
    if [ "$sub" = "config" ]; then
      [ -f "$FAKE/config_falha" ] && exit 1
      if [ "$2" = "--images" ]; then
        if [ -f "$FAKE/images_saida" ]; then cat "$FAKE/images_saida"; else
          grep -E '^    image: ' "$arquivo" | grep -v 'docker.io/' | sed 's/^    image: //'
        fi
      fi
      exit 0
    fi
    if [ "$sub" = "up" ]; then
      ruido
      grep -E '^    image: ' "$arquivo" | grep -v 'docker.io/' >> "$FAKE/ups.log"
      resultado=$(head -n 1 "$FAKE/up_resultados" 2>/dev/null || echo ok)
      sed -i '1d' "$FAKE/up_resultados" 2>/dev/null || true
      [ "$resultado" = "ok" ] && exit 0
      exit 1
    fi
    exit 0 ;;
esac
exit 0
"""


class Cenario:
    def __init__(self, tmp_path: Path, linhas_de_imagem=None, repo=REPO, modo=0o640):
        self.pasta = tmp_path / "projeto"
        self.pasta.mkdir()
        self.fake = tmp_path / "fake"
        self.fake.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        docker = self.bin / "docker"
        docker.write_text(DOCKER_FALSO)
        docker.chmod(0o755)
        self.repo = repo
        linhas = linhas_de_imagem if linhas_de_imagem is not None else [f"    image: {repo}:2.1.0@{ANTIGO}"]
        self.compose = self.pasta / "docker-compose.yml"
        self.compose.write_text(
            "services:\n  bot:\n"
            + "\n".join(linhas)
            + f"\n    restart: unless-stopped\n  updater-proxy:\n    image: docker.io/{REPO}:2.1.0@{PROXY_DIGEST}\n"
        )
        self.compose.chmod(modo)
        self.original = self.compose.read_text()
        self.modo = modo
        (self.fake / "env").write_text("PATH=/usr/bin\nEMPIREMUSE_VERSION=2.1.1\n")
        (self.fake / "up_resultados").write_text("ok\nok\n")

    def marcar(self, nome: str, conteudo: str = "1\n"):
        (self.fake / nome).write_text(conteudo)

    def rodar(self, alvo: str, pasta: Path | None = None) -> subprocess.CompletedProcess:
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "FAKE": str(self.fake), "HOME": str(self.fake)}
        return subprocess.run(
            ["sh", "-euc", HELPER_SCRIPT, "updater", alvo, "e2e", str(pasta or self.pasta), "bot", self.repo],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    @property
    def chamadas(self) -> list[str]:
        arquivo = self.fake / "calls.log"
        return arquivo.read_text().splitlines() if arquivo.exists() else []

    @property
    def ups(self) -> list[str]:
        arquivo = self.fake / "ups.log"
        return arquivo.read_text().splitlines() if arquivo.exists() else []

    @property
    def backups(self) -> list[Path]:
        return sorted(self.pasta.glob("docker-compose.yml.bak-*"))

    def intacto(self) -> bool:
        return self.compose.read_text() == self.original


def alvo(versao="2.1.1", digest=NOVO):
    return f"{versao}@{digest}"


@pytest.fixture
def cenario(tmp_path):
    return Cenario(tmp_path)


def so_linhas_do_prefixo(resultado: subprocess.CompletedProcess) -> None:
    """(l) A saída visível é só de linhas próprias com o prefixo fixo: nada do docker passa (V4)."""
    visivel = [linha for linha in (resultado.stdout + resultado.stderr).splitlines() if linha.strip()]
    assert all(linha.startswith(PREFIXO) for linha in visivel), visivel
    assert "ruido-do-docker" not in resultado.stdout + resultado.stderr


def classe(chamada: str) -> str:
    if chamada.startswith("pull"):
        return "pull"
    if chamada.startswith("image inspect"):
        return "inspect"
    if chamada.startswith("compose") and " up " in f" {chamada} ":
        return "up"
    if "--images" in chamada:
        return "images"
    if chamada.startswith("compose") and " config" in chamada:
        return "config"
    return chamada


# ============================================================================ (a) sucesso


def test_success_swaps_the_line_keeps_the_proxy_line_and_calls_in_order(cenario):
    resultado = cenario.rodar(alvo())

    assert resultado.returncode == 0, resultado.stderr
    texto = cenario.compose.read_text()
    assert f"    image: {REPO}:2.1.1@{NOVO}\n" in texto
    assert f"    image: docker.io/{REPO}:2.1.0@{PROXY_DIGEST}\n" in texto  # (h) a linha do proxy nunca muda
    assert texto.count("image:") == 2 and "restart: unless-stopped" in texto
    (backup,) = cenario.backups
    assert backup.read_text() == cenario.original
    assert stat.S_IMODE(backup.stat().st_mode) == cenario.modo == stat.S_IMODE(cenario.compose.stat().st_mode)
    ordem = [classe(c) for c in cenario.chamadas]
    assert (
        ordem.index("pull") < ordem.index("inspect") < ordem.index("config") < ordem.index("images") < ordem.index("up")
    )
    assert f"{REPO}:2.1.1@{NOVO}" in cenario.chamadas[0]
    assert "--wait" in next(c for c in cenario.chamadas if classe(c) == "up") and "300" in next(
        c for c in cenario.chamadas if classe(c) == "up"
    )
    so_linhas_do_prefixo(resultado)


# ============================================================================ (b) pull, (c) compose, (d) downgrade, (g) alvo


def test_a_failed_pull_is_66_and_changes_nothing(cenario):
    cenario.marcar("pull_falha")

    resultado = cenario.rodar(alvo())

    assert resultado.returncode == 66
    assert cenario.intacto() and cenario.backups == [] and cenario.ups == []
    so_linhas_do_prefixo(resultado)


@pytest.mark.parametrize(
    "linhas",
    [
        [],
        [f"    image: {REPO}:2.1.0@{ANTIGO}", f"    image: {REPO}:2.1.0@{ANTIGO}"],
        [f"    image: {REPO}:2.1.0", f"    image: {REPO}:2.0.0@{ANTIGO}"],
        [f"    image: outro/repo:2.1.0@{ANTIGO}"],
        [f"    image: {REPO}:latest"],
        [f"    image: {REPO}:2.1.0@sha256:abc"],
        [f"    image: {REPO}:2.1.0 # comentário"],
    ],
    ids=["zero", "duas", "duas-formas", "outro-repo", "latest", "digest-curto", "comentario"],
)
def test_zero_or_two_matching_lines_is_65_and_intact(tmp_path, linhas):
    cenario = Cenario(tmp_path, linhas)

    resultado = cenario.rodar(alvo())

    assert resultado.returncode == 65
    assert cenario.intacto() and cenario.backups == [] and "pull" not in [classe(c) for c in cenario.chamadas]
    so_linhas_do_prefixo(resultado)


def test_the_proxy_docker_io_line_alone_never_matches(tmp_path):
    cenario = Cenario(tmp_path, [])

    assert cenario.rodar(alvo()).returncode == 65
    assert cenario.intacto()


@pytest.mark.parametrize("versao", ["2.1.0", "2.0.9", "1.9.9"])
def test_a_target_not_newer_than_the_current_line_is_67(cenario, versao):
    resultado = cenario.rodar(alvo(versao))

    assert resultado.returncode == 67
    assert cenario.intacto() and cenario.backups == [] and "pull" not in [classe(c) for c in cenario.chamadas]
    so_linhas_do_prefixo(resultado)


@pytest.mark.parametrize(
    "invalido",
    [
        "latest",
        "2.1",
        "2.1.1",
        "2.1.1@sha256:abc",
        f"v2.1.1@{NOVO}",
        f"02.1.1@{NOVO}",
        f"2.1.1@sha256:{'AB' * 32}",
        f"2.1.1@{NOVO};id",
        f"2.1.1@{NOVO} x",
        "",
    ],
)
def test_an_invalid_target_is_64_and_nothing_runs(cenario, invalido):
    resultado = cenario.rodar(invalido)

    assert resultado.returncode == 64
    assert cenario.intacto() and cenario.backups == [] and cenario.chamadas == []
    so_linhas_do_prefixo(resultado)


# ============================================================================ (i) versão da imagem (V1)


@pytest.mark.parametrize(
    "env", ["EMPIREMUSE_VERSION=2.0.0", "EMPIREMUSE_VERSION=9999.0.0", "EMPIREMUSE_VERSION=2.1.1-rc.1", "PATH=/usr/bin"]
)
def test_an_image_whose_embedded_version_differs_from_the_target_is_68(cenario, env):
    (cenario.fake / "env").write_text(f"PATH=/usr/bin\n{env}\n" if "VERSION" in env else f"{env}\n")

    resultado = cenario.rodar(alvo())

    assert resultado.returncode == 68
    assert cenario.intacto() and cenario.backups == [] and cenario.ups == []
    so_linhas_do_prefixo(resultado)


def test_a_digest_of_an_old_image_cannot_carry_a_newer_version(cenario):
    """Um bot comprometido manda ``9999.0.0@<digest antigo>``: a imagem puxada traz a versão 2.1.1 e a conferência recusa."""
    resultado = cenario.rodar(alvo("9999.0.0", ANTIGO))

    assert resultado.returncode == 68
    assert cenario.intacto() and cenario.backups == []


# ============================================================================ (e)(f)(k) rollback


def test_a_failed_up_restores_the_file_and_runs_up_again_then_is_70(cenario):
    cenario.marcar("up_resultados", "fail\nok\n")

    resultado = cenario.rodar(alvo())

    assert resultado.returncode == 70
    assert cenario.intacto()
    assert len(cenario.ups) == 2 and NOVO in cenario.ups[0] and ANTIGO in cenario.ups[1]
    assert len(cenario.backups) == 1
    so_linhas_do_prefixo(resultado)


def test_when_the_rollback_up_also_fails_it_is_71(cenario):
    cenario.marcar("up_resultados", "fail\nfail\n")

    resultado = cenario.rodar(alvo())

    assert resultado.returncode == 71
    assert cenario.intacto()
    so_linhas_do_prefixo(resultado)


def test_a_failing_compose_config_rolls_back(cenario):
    cenario.marcar("config_falha")

    resultado = cenario.rodar(alvo())

    assert resultado.returncode == 70
    assert cenario.intacto()
    so_linhas_do_prefixo(resultado)


def test_config_images_returning_another_reference_rolls_back(cenario):
    cenario.marcar("images_saida", f"{REPO}:9.9.9@{NOVO}\n")

    resultado = cenario.rodar(alvo())

    assert resultado.returncode == 70
    assert cenario.intacto()
    assert ANTIGO in cenario.ups[-1]
    so_linhas_do_prefixo(resultado)


# ============================================================================ (j) repo com ponto e porta casa só literalmente


def test_a_repo_with_dots_and_a_port_matches_only_literally(tmp_path):
    repo = "127.0.0.1:5000/e2e-bot"
    cenario = Cenario(tmp_path, [f"    image: {repo}:2.1.0@{ANTIGO}"], repo=repo)

    resultado = cenario.rodar(alvo())

    assert resultado.returncode == 0, resultado.stderr
    assert f"    image: {repo}:2.1.1@{NOVO}\n" in cenario.compose.read_text()


def test_the_script_never_writes_the_arguments_into_code(cenario):
    """Argumentos são posicionais: um repo com metacaracteres de shell não executa nada (nenhum arquivo é criado)."""
    marca = cenario.pasta / "executou"
    repo_hostil = f"x;touch {marca}"

    env = {"PATH": f"{cenario.bin}:/usr/bin:/bin", "FAKE": str(cenario.fake), "HOME": str(cenario.fake)}
    resultado = subprocess.run(
        ["sh", "-euc", HELPER_SCRIPT, "updater", alvo(), "e2e", str(cenario.pasta), "bot", repo_hostil],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert resultado.returncode == 65
    assert not marca.exists() and cenario.intacto()


# ============================================================================ (m) quem grava o projeto decide o que sobe: dono, link e escrita de grupo/outros


def recusado_antes_de_qualquer_docker(cenario, resultado) -> None:
    assert resultado.returncode == 65
    assert PREFIXO + "compose-perm" in resultado.stderr + resultado.stdout
    assert cenario.chamadas == [] and cenario.ups == [] and cenario.backups == [] and cenario.intacto()
    so_linhas_do_prefixo(resultado)


def test_a_project_directory_writable_by_others_is_refused(cenario):
    cenario.pasta.chmod(0o777)

    recusado_antes_de_qualquer_docker(cenario, cenario.rodar(alvo()))


def test_a_compose_file_that_is_a_symlink_is_refused(cenario, tmp_path):
    real = tmp_path / "compose-real.yml"
    cenario.compose.rename(real)
    cenario.compose.symlink_to(real)

    resultado = cenario.rodar(alvo())

    assert resultado.returncode == 65 and cenario.chamadas == [] and real.read_text() == cenario.original


def test_a_project_directory_that_is_a_symlink_is_refused(cenario, tmp_path):
    ligacao = tmp_path / "ligacao"
    ligacao.symlink_to(cenario.pasta)

    resultado = cenario.rodar(alvo(), pasta=ligacao)

    recusado_antes_de_qualquer_docker(cenario, resultado)


def test_an_env_file_that_is_a_symlink_is_refused(cenario, tmp_path):
    real = tmp_path / "env-real"
    real.write_text("X=1\n")
    real.chmod(0o600)
    (cenario.pasta / ".env").symlink_to(real)

    recusado_antes_de_qualquer_docker(cenario, cenario.rodar(alvo()))


def test_a_missing_env_file_does_not_stop_the_update(cenario):
    assert not (cenario.pasta / ".env").exists()

    assert cenario.rodar(alvo()).returncode == 0


@pytest.mark.parametrize("modo_compose", [0o600, 0o644])
def test_owned_and_not_group_or_other_writable_files_proceed(cenario, modo_compose):
    cenario.pasta.chmod(0o755)
    cenario.compose.chmod(modo_compose)
    env = cenario.pasta / ".env"
    env.write_text("X=1\n")
    env.chmod(0o600)

    resultado = cenario.rodar(alvo())

    assert resultado.returncode == 0, resultado.stderr
    assert cenario.ups


def test_a_missing_or_unreadable_compose_is_still_reported_before_the_trust_check(cenario):
    """Ordem: existência/legibilidade do compose (65 'compose') vem antes da confiança ('compose-perm')."""
    cenario.compose.unlink()
    cenario.pasta.chmod(0o777)

    resultado = cenario.rodar(alvo())

    assert resultado.returncode == 65 and "compose-perm" not in resultado.stderr + resultado.stdout


@pytest.mark.skipif(os.geteuid() != 0, reason="trocar o dono exige root")
@pytest.mark.parametrize("alvo_do_chown", ["pasta", "compose"])
def test_a_file_owned_by_another_user_is_refused(cenario, alvo_do_chown):
    os.chown(getattr(cenario, alvo_do_chown), 12345, 12345)

    recusado_antes_de_qualquer_docker(cenario, cenario.rodar(alvo()))


# ============================================================================ D13: a linha ATUAL do compose pode ser ``X.Y.Z-rc.N`` (A29 i)


def cenario_rc(tmp_path, atual="2.1.0-rc.2", *, com_digest=True, versao_da_imagem="2.1.0"):
    sufixo = f"@{ANTIGO}" if com_digest else ""
    cenario = Cenario(tmp_path, [f"    image: {REPO}:{atual}{sufixo}"])
    (cenario.fake / "env").write_text(f"PATH=/usr/bin\nEMPIREMUSE_VERSION={versao_da_imagem}\n")
    return cenario


@pytest.mark.parametrize("com_digest", [True, False], ids=["com-digest", "sem-digest"])
def test_an_rc_current_line_is_swapped_for_the_stable_target(tmp_path, com_digest):
    cenario = cenario_rc(tmp_path, com_digest=com_digest)

    resultado = cenario.rodar(alvo("2.1.0"))

    assert resultado.returncode == 0, resultado.stderr
    texto = cenario.compose.read_text()
    assert f"    image: {REPO}:2.1.0@{NOVO}\n" in texto and "-rc." not in texto.split("updater-proxy")[0]
    assert f"    image: docker.io/{REPO}:2.1.0@{PROXY_DIGEST}\n" in texto  # a linha do proxy nunca muda
    assert texto.count("image:") == 2
    (backup,) = cenario.backups
    assert backup.read_text() == cenario.original
    ordem = [classe(c) for c in cenario.chamadas]
    assert ordem.index("pull") < ordem.index("inspect") < ordem.index("config") < ordem.index("up")
    so_linhas_do_prefixo(resultado)


@pytest.mark.parametrize("versao", ["2.0.9", "2.0.0", "1.9.9"])
def test_a_target_older_than_the_rc_current_line_is_67_and_intact(tmp_path, versao):
    cenario = cenario_rc(tmp_path)

    resultado = cenario.rodar(alvo(versao))

    assert resultado.returncode == 67
    assert cenario.intacto() and cenario.backups == [] and "pull" not in [classe(c) for c in cenario.chamadas]
    so_linhas_do_prefixo(resultado)


@pytest.mark.parametrize(
    "atual",
    [
        "2.1.0-rc.02",
        "2.1.0-RC.2",
        "2.1.0-rc.",
        "2.1.0-rc",
        "2.1.0-rc2",
        "2.1.0-beta.1",
        "2.1.0-rc.10000",
        "2.1.0-rc.2+b",
        "2.1.0-rc.-1",
        "2.1.0-rc.2-rc.3",
    ],
)
def test_an_invalid_current_line_suffix_is_65_and_intact(tmp_path, atual):
    cenario = cenario_rc(tmp_path, atual)

    resultado = cenario.rodar(alvo("2.1.0"))

    assert resultado.returncode == 65
    assert cenario.intacto() and cenario.backups == [] and "pull" not in [classe(c) for c in cenario.chamadas]
    so_linhas_do_prefixo(resultado)


def test_a_failed_up_from_an_rc_line_restores_the_rc_line_and_is_70(tmp_path):
    cenario = cenario_rc(tmp_path)
    cenario.marcar("up_resultados", "fail\nok\n")

    resultado = cenario.rodar(alvo("2.1.0"))

    assert resultado.returncode == 70
    assert cenario.intacto() and f"{REPO}:2.1.0-rc.2@{ANTIGO}" in cenario.compose.read_text()
    assert (
        len(cenario.ups) == 2 and NOVO in cenario.ups[0] and "2.1.0-rc.2" in cenario.ups[1] and ANTIGO in cenario.ups[1]
    )
    so_linhas_do_prefixo(resultado)
