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
    ("linha_ruim", "motivo"),
    [
        ("0.0.0.0/0", "prefixo menor"),  # liberaria tudo
        ("16.0.0.0/11", "prefixo menor"),
        ("192.168.0.0/16", "não global"),  # privada
        ("8.8.8.0", "falta o prefixo"),  # global, então só o prefixo ausente a recusa
        ("8.8.8.1/24", "não é uma faixa CIDR válida"),  # global, bits de host ligados
        ("abc/24", "não é uma faixa CIDR válida"),
        ("2606:4700::/32", "família de endereço errada"),  # IPv6 na lista IPv4
        (V4_OK[0], "repetida"),
        ("20.0.0.0%1/16", "escopo"),
    ],
)
def test_lista_v4_com_linha_ruim_e_recusada_pelo_motivo_certo(tmp_path: Path, linha_ruim: str, motivo: str) -> None:
    write_lists(tmp_path, [*V4_OK, linha_ruim])

    result = run_script("check", list_dir=tmp_path)

    assert result.returncode != 0
    assert motivo in result.stderr, result.stderr


@pytest.mark.parametrize(
    ("linha_ruim", "motivo"),
    [
        ("::/0", "prefixo menor"),
        ("2a06:98c0::/28", "prefixo menor"),
        ("fd00::/32", "não global"),
        ("2606:4700::%1,2001:db8::/32", "escopo"),  # F-m1: injetaria um RETURN para outra faixa no ip6tables-restore
        ("2606:4700::%eth0/32", "escopo"),
    ],
)
def test_lista_v6_com_linha_ruim_e_recusada_pelo_motivo_certo(tmp_path: Path, linha_ruim: str, motivo: str) -> None:
    write_lists(tmp_path, V4_OK, [*V6_OK, linha_ruim])

    result = run_script("check", list_dir=tmp_path)

    assert result.returncode != 0
    assert motivo in result.stderr, result.stderr


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


EMPTY_NAMESPACE_PROBE = '[ "$(iptables -S | wc -l)" = 3 ] && [ "$(ip6tables -S | wc -l)" = 3 ]'


def _run_in_namespace(roteiro: str, list_dir: Path) -> subprocess.CompletedProcess[str]:
    """Roda o roteiro num network namespace novo e vazio; recusa começar se ele não estiver vazio."""
    if os.geteuid() != 0 or shutil.which("unshare") is None or shutil.which("iptables") is None:
        pytest.skip("precisa de root, unshare e iptables (rodado à mão e no passo dedicado do CI)")
    env = {"PATH": os.environ["PATH"], "SCRIPT": str(SCRIPT), "CF_FW_LIST_DIR": str(list_dir), "CF_FW_PUB_IF": "eth0"}
    guarda = f"set -eu\n{EMPTY_NAMESPACE_PROBE} || {{ echo 'namespace não está vazio' >&2; exit 99; }}\n"
    result = subprocess.run(
        ["unshare", "-n", "sh", "-c", guarda + roteiro],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode == 99:
        pytest.skip("o network namespace não veio vazio: recusa tocar em regra")
    return result


def test_on_off_em_namespace_isolado(tmp_path: Path) -> None:
    write_lists(tmp_path, V4_OK)
    ruins = tmp_path / "ruins"
    ruins.mkdir()
    write_lists(ruins, [*V4_OK, "0.0.0.0/0"])
    roteiro = """
    for f in iptables ip6tables; do $f -N DOCKER-USER; $f -A DOCKER-USER -j RETURN; done
    sh "$SCRIPT" on >/dev/null 2>&1
    sh "$SCRIPT" on >/dev/null 2>&1
    echo "ganchos4=$(iptables -S DOCKER-USER | grep -c -- '-j CF-ONLY')"
    echo "regras4=$(iptables -S CF-ONLY | wc -l)"
    echo "ganchos6=$(ip6tables -S DOCKER-USER | grep -c -- '-j CF-ONLY')"
    echo "regras6=$(ip6tables -S CF-ONLY | wc -l)"
    sh "$SCRIPT" status >/dev/null
    # caminho de erro: uma lista inválida recusa o on e deixa cadeia e gancho como estavam
    if CF_FW_LIST_DIR="$RUINS" sh "$SCRIPT" on >/dev/null 2>&1; then echo "on-invalido=aceito"; fi
    echo "apos-erro4=$(iptables -S DOCKER-USER | grep -c -- '-j CF-ONLY') $(iptables -S CF-ONLY | wc -l)"
    sh "$SCRIPT" off >/dev/null
    sh "$SCRIPT" off >/dev/null
    echo "depois=$(iptables -S | grep -c CF-ONLY || true) $(ip6tables -S | grep -c CF-ONLY || true)"
    if sh "$SCRIPT" status >/dev/null 2>&1; then echo "status-desligado=ok"; fi
    """

    result = _run_in_namespace(roteiro.replace('"$RUINS"', f'"{ruins}"'), tmp_path)

    assert result.returncode == 0, result.stderr
    n = len(V4_OK) + 2
    assert result.stdout.split("\n")[:-1] == [
        "ganchos4=1",
        f"regras4={n}",
        "ganchos6=1",
        f"regras6={len(V6_OK) + 2}",
        f"apos-erro4=1 {n}",
        "depois=0 0",
    ]


def test_ipv6_publicado_pelo_docker_proxy_e_filtrado_pela_input(tmp_path: Path) -> None:
    """N-m1: o gancho da INPUT v6 entra sempre, mesmo sem listener; o listener que sobe depois já está coberto."""
    write_lists(tmp_path, V4_OK)
    roteiro = """
    iptables -N DOCKER-USER; iptables -A DOCKER-USER -j RETURN
    ip6tables -A INPUT -p tcp --dport 22 -j ACCEPT
    sh "$SCRIPT" on >/dev/null 2>&1
    echo "input-sem-listener=$(ip6tables -S INPUT | grep -c -- '-j CF-ONLY')"
    python3 -c 'import socket,time
s=socket.socket(socket.AF_INET6); s.bind(("::",443)); s.listen(1); time.sleep(30)' &
    pid=$!
    for _ in $(seq 50); do [ -n "$(ss -H -ltn -6 'sport = :443')" ] && break; sleep 0.1; done
    echo "listener=$(ss -H -ltn -6 'sport = :443' | wc -l)"
    if sh "$SCRIPT" status >/dev/null 2>&1; then echo "status-depois=ok"; fi
    echo "ssh-intacto=$(ip6tables -S INPUT | grep -c -- '--dport 22')"
    echo "gancho=$(ip6tables -S INPUT | grep -- '-i eth0 ' | grep -c -- '--ctorigdstport 443')"
    ip6tables -D INPUT -i eth0 -p tcp -m conntrack --ctstate NEW --ctdir ORIGINAL --ctorigdstport 443 -j CF-ONLY
    if sh "$SCRIPT" status >/dev/null 2>&1; then echo "status-sem-gancho=ok"; fi
    sh "$SCRIPT" off >/dev/null
    echo "depois=$(ip6tables -S | grep -c CF-ONLY || true)"
    kill $pid
    """
    if shutil.which("ss") is None:
        pytest.skip("precisa de ss para o listener ser detectado")

    result = _run_in_namespace(roteiro, tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [
        "input-sem-listener=1",
        "listener=1",
        "status-depois=ok",
        "ssh-intacto=1",
        "gancho=1",
        "depois=0",
    ]


def test_sem_input_v6_filtravel_o_on_falha_fechado_e_nao_liga_o_ipv4(tmp_path: Path) -> None:
    """N-m1/N-s1: sem INPUT v6 filtrável o `on` recusa tudo (IPv4 inclusive), em vez de deixar o IPv6 aberto."""
    write_lists(tmp_path, V4_OK)
    roteiro = """
    for f in iptables ip6tables; do $f -N DOCKER-USER; $f -A DOCKER-USER -j RETURN; done
    mkdir -p "$SHIM"
    real=$(command -v ip6tables)
    printf '#!/bin/sh\\ncase "$*" in *"-L INPUT"*) exit 1;; esac\\nexec %s "$@"\\n' "$real" > "$SHIM/ip6tables"
    chmod +x "$SHIM/ip6tables"
    if PATH="$SHIM:$PATH" sh "$SCRIPT" on >/dev/null 2>&1; then echo "on=aceito"; fi
    echo "restou=$(iptables -S | grep -c CF-ONLY || true) $(ip6tables -S | grep -c CF-ONLY || true)"
    """

    result = _run_in_namespace(roteiro.replace("$SHIM", f"{tmp_path}/shim2"), tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["restou=0", "0"]


def test_falha_no_ipv6_depois_do_ipv4_desfaz_tudo(tmp_path: Path) -> None:
    """Um ip6tables-restore que falha não pode deixar o IPv4 ligado pela metade."""
    write_lists(tmp_path, V4_OK)
    roteiro = """
    for f in iptables ip6tables; do $f -N DOCKER-USER; $f -A DOCKER-USER -j RETURN; done
    mkdir -p "$SHIM"
    printf '#!/bin/sh\\nexit 1\\n' > "$SHIM/ip6tables-restore"
    chmod +x "$SHIM/ip6tables-restore"
    if PATH="$SHIM:$PATH" sh "$SCRIPT" on >/dev/null 2>&1; then echo "on=aceito"; fi
    echo "restou=$(iptables -S | grep -c CF-ONLY || true)"
    """

    result = _run_in_namespace(roteiro.replace("$SHIM", f"{tmp_path}/shim"), tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["restou=0"]
