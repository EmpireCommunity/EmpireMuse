"""Regras puras do firewall da 443 (deploy/host/cf-firewall.sh): validação das listas e o que o `on` aplicaria.

Nada aqui toca regra de firewall. O teste de integração só roda como root, dentro de um network namespace descartável
(`unshare -n`), e se recusa a rodar se o namespace não estiver vazio (isso garantiria que ele não é o do host).
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

HOST_DIR = Path(__file__).resolve().parents[2] / "deploy" / "host"
SCRIPT = HOST_DIR / "cf-firewall.sh"

pytestmark = [
    pytest.mark.critical,
    pytest.mark.skipif(shutil.which("sh") is None or shutil.which("python3") is None, reason="precisa de sh e python3"),
]

V4_OK = [f"{n}.0.0.0/16" for n in range(20, 30)]
V6_OK = [f"2606:{n:x}::/32" for n in range(0x4700, 0x4705)]


def run_script(*args: str, list_dir: Path, iface: str = "eth0") -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ["PATH"], "CF_FW_LIST_DIR": str(list_dir), "CF_FW_PUB_IF": iface}
    return subprocess.run(["sh", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=30, check=False)


def write_lists(directory: Path, v4: list[str], v6: list[str] | None = None) -> Path:
    (directory / "cf-ips-v4.txt").write_text("\n".join(v4) + "\n", encoding="utf-8")
    (directory / "cf-ips-v6.txt").write_text("\n".join(V6_OK if v6 is None else v6) + "\n", encoding="utf-8")
    return directory


def test_listas_do_repositorio_sao_validas() -> None:
    result = run_script("check", list_dir=HOST_DIR)

    assert result.returncode == 0, result.stderr
    assert "listas válidas" in result.stdout


@pytest.mark.parametrize(
    "linha_ruim",
    [
        "0.0.0.0/0",  # liberaria tudo
        "16.0.0.0/11",  # prefixo curto demais
        "192.168.0.0/16",  # privada
        "203.0.113.0",  # sem prefixo
        "203.0.113.1/24",  # bits de host ligados
        "não é uma faixa",
        "2606:4700::/32",  # IPv6 na lista IPv4
        V4_OK[0],  # repetida
    ],
)
def test_lista_v4_com_linha_ruim_e_recusada(tmp_path: Path, linha_ruim: str) -> None:
    write_lists(tmp_path, [*V4_OK, linha_ruim])

    result = run_script("check", list_dir=tmp_path)

    assert result.returncode != 0
    assert "cf-firewall" in result.stderr


@pytest.mark.parametrize("linha_ruim", ["::/0", "2a06:98c0::/28", "fd00::/32"])
def test_lista_v6_com_linha_ruim_e_recusada(tmp_path: Path, linha_ruim: str) -> None:
    write_lists(tmp_path, V4_OK, [*V6_OK, linha_ruim])

    assert run_script("check", list_dir=tmp_path).returncode != 0


def test_lista_com_poucas_faixas_ou_ausente_e_recusada(tmp_path: Path) -> None:
    write_lists(tmp_path, V4_OK[:9])
    assert run_script("check", list_dir=tmp_path).returncode != 0

    (tmp_path / "cf-ips-v4.txt").unlink()
    assert run_script("check", list_dir=tmp_path).returncode != 0


def test_print_mostra_cadeia_de_uma_transacao_so_e_gancho_com_estado(tmp_path: Path) -> None:
    write_lists(tmp_path, V4_OK)

    result = run_script("print", list_dir=tmp_path)

    assert result.returncode == 0, result.stderr
    ipv4 = result.stdout.split("# --- ip6tables-restore")[0].splitlines()
    assert ipv4[1:3] == ["*filter", ":CF-ONLY - [0:0]"]  # declarar a cadeia a esvazia na mesma transação
    regras = [linha for linha in ipv4 if linha.startswith("-A CF-ONLY")]
    assert regras[:-1] == [f"-A CF-ONLY -s {faixa} -j RETURN" for faixa in V4_OK]
    assert regras[-1] == "-A CF-ONLY -j DROP"
    assert "COMMIT" in ipv4
    assert "--ctstate NEW --ctdir ORIGINAL --ctorigdstport 443 -j CF-ONLY" in result.stdout


def test_interface_invalida_e_recusada(tmp_path: Path) -> None:
    write_lists(tmp_path, V4_OK)

    result = run_script("print", list_dir=tmp_path, iface="eth0;reboot")

    assert result.returncode != 0


def _namespace_isolado_disponivel() -> bool:
    if os.geteuid() != 0 or shutil.which("unshare") is None or shutil.which("iptables") is None:
        return False
    probe = subprocess.run(
        ["unshare", "-n", "sh", "-c", "iptables -S | wc -l"], capture_output=True, text=True, timeout=30, check=False
    )
    return probe.returncode == 0 and probe.stdout.strip() == "3"  # só as 3 políticas padrão: namespace vazio


@pytest.mark.integration
@pytest.mark.skipif(not _namespace_isolado_disponivel(), reason="precisa de root e de um network namespace isolado")
def test_on_off_em_namespace_isolado(tmp_path: Path) -> None:
    write_lists(tmp_path, V4_OK)
    roteiro = """
    set -eu
    [ "$(iptables -S | wc -l)" = 3 ]
    for f in iptables ip6tables; do $f -N DOCKER-USER; $f -A DOCKER-USER -j RETURN; done
    sh "$SCRIPT" on >/dev/null
    sh "$SCRIPT" on >/dev/null
    echo "ganchos=$(iptables -S DOCKER-USER | grep -c -- '-j CF-ONLY')"
    echo "regras=$(iptables -S CF-ONLY | wc -l)"
    sh "$SCRIPT" off >/dev/null
    sh "$SCRIPT" off >/dev/null
    echo "depois=$(iptables -S | grep -c CF-ONLY || true)"
    """
    env = {"PATH": os.environ["PATH"], "SCRIPT": str(SCRIPT), "CF_FW_LIST_DIR": str(tmp_path), "CF_FW_PUB_IF": "eth0"}

    result = subprocess.run(
        ["unshare", "-n", "sh", "-c", roteiro], env=env, capture_output=True, text=True, timeout=60, check=False
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["ganchos=1", f"regras={len(V4_OK) + 2}", "depois=0"]
