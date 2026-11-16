#!/bin/sh
# Firewall da porta 443 do host: só as faixas da Cloudflare abrem conexões novas de fora (ver deploy/README.md).
#
# Uso: cf-firewall.sh COMANDO
#   on            valida as listas, monta a cadeia CF-ONLY de uma vez e só então liga o gancho na DOCKER-USER
#   off           tira o gancho e apaga a cadeia (volta ao estado de antes; sem erro se nada estiver ligado)
#   status        mostra se o gancho existe e os contadores da cadeia (precisa de root)
#   check         só valida as duas listas e imprime as contagens (não precisa de root nem de iptables)
#   print         imprime o que o `on` aplicaria, sem aplicar nada (não precisa de root)
#   arm-undo [M]  agenda `off` daqui a M minutos (1 a 60; padrão 10) com systemd-run: rede de segurança antes do `on`
#   cancel-undo   cancela o desfazer agendado
#
# Como funciona: o Docker publica a 443 por DNAT, então o tráfego passa pela FORWARD e pela DOCKER-USER, não pelo INPUT
# (o SSH, que é INPUT, nunca é tocado). O gancho casa SÓ o primeiro pacote de uma conexão nova, na direção original, com
# destino original na porta 443, vindo da interface pública:
#   -m conntrack --ctstate NEW --ctdir ORIGINAL --ctorigdstport 443
# Sem o `--ctdir ORIGINAL`, as respostas das conexões HTTPS que os próprios containers abrem para fora (que chegam pela
# mesma interface e também têm a 443 como destino original) cairiam no DROP e a saída dos containers quebraria. A cadeia
# libera (RETURN) as faixas da Cloudflare e descarta (DROP) o resto.
#
# As faixas vêm de listas ESTÁTICAS e revisadas (padrão /etc/empire/cf-ips-v4.txt e cf-ips-v6.txt, as de deploy/host/):
# o script nunca baixa nada, então sem rede no boot ele continua funcionando. Cada lista é validada ANTES de tocar em
# qualquer regra; qualquer falha sai com código diferente de 0 e deixa tudo como estava (falha fechada).
#   - cada linha é um CIDR válido, com barra, da família certa (comentário # e linha vazia são ignorados);
#   - prefixo no mínimo /12 (IPv4) e /29 (IPv6): isso recusa 0.0.0.0/0 e ::/0, que liberariam tudo;
#   - só endereços globais (nada privado, de loopback ou reservado), sem linha repetida;
#   - no mínimo 10 faixas IPv4 e 5 IPv6.
# A troca é atômica: o iptables-restore --noflush recebe o conteúdo inteiro da cadeia numa transação só, então a cadeia
# nunca fica vazia nem só com o DROP, e rodar `on` de novo (o systemd roda quando o Docker reinicia) troca as faixas sem
# janela. O gancho entra uma vez só, depois de a cadeia estar cheia.
#
# Variáveis (para teste e casos especiais): CF_FW_LIST_DIR (pasta das listas), CF_FW_PUB_IF (interface pública; sem ela,
# a da rota padrão, e para IPv6 a da rota padrão IPv6 se houver). Sem IPv6 na DOCKER-USER, o IPv6 é pulado com um aviso.
set -eu

LIST_DIR=${CF_FW_LIST_DIR:-/etc/empire}
CHAIN=CF-ONLY
PORT=443
UNDO_UNIT=cf-fw-rollback
MIN_V4_PREFIX=12
MIN_V6_PREFIX=29
MIN_V4_COUNT=10
MIN_V6_COUNT=5

die() {
    echo "cf-firewall: $*" >&2
    exit 1
}

need_root() {
    [ "$(id -u)" = 0 ] || die "precisa rodar como root"
}

# Valida uma lista e imprime as faixas normalizadas, uma por linha. Argumentos: família (4 ou 6), prefixo mínimo,
# quantidade mínima e caminho.
validate_list() {
    python3 -I - "$1" "$2" "$3" "$4" <<'PY'
import ipaddress
import sys

version, min_prefix, min_count, path = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]


def fail(message):
    print(f"cf-firewall: {path}: {message}", file=sys.stderr)
    sys.exit(1)


try:
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
except (OSError, UnicodeDecodeError):
    fail("não foi possível ler a lista")

networks = []
for number, raw in enumerate(lines, 1):
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    if "/" not in line:
        fail(f"linha {number}: falta o prefixo")
    try:
        network = ipaddress.ip_network(line, strict=True)
    except ValueError:
        fail(f"linha {number}: não é uma faixa CIDR válida")
    if network.version != version:
        fail(f"linha {number}: família de endereço errada")
    if network.prefixlen < min_prefix:
        fail(f"linha {number}: prefixo menor que /{min_prefix}")
    if not network.is_global:
        fail(f"linha {number}: faixa não global")
    if network in networks:
        fail(f"linha {number}: faixa repetida")
    networks.append(network)

if len(networks) < min_count:
    fail(f"só {len(networks)} faixas, o mínimo é {min_count}")
for network in networks:
    print(network)
PY
}

valid_v4() { validate_list 4 "$MIN_V4_PREFIX" "$MIN_V4_COUNT" "$LIST_DIR/cf-ips-v4.txt"; }
valid_v6() { validate_list 6 "$MIN_V6_PREFIX" "$MIN_V6_COUNT" "$LIST_DIR/cf-ips-v6.txt"; }

# Nome da interface pública. $1 é -4 ou -6; para IPv6 sem rota padrão própria, cai na interface do IPv4.
public_interface() {
    iface=${CF_FW_PUB_IF:-}
    if [ -z "$iface" ]; then
        iface=$(ip "$1" -o route show default 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p' | sed -n 1p)
    fi
    if [ -z "$iface" ] && [ "$1" = "-6" ]; then
        iface=$(ip -4 -o route show default 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p' | sed -n 1p)
    fi
    case $iface in
        '' | *[!A-Za-z0-9_.-]*) die "não achei um nome de interface pública válido (defina CF_FW_PUB_IF)" ;;
    esac
    [ "${#iface}" -le 15 ] || die "nome de interface longo demais: $iface"
    echo "$iface"
}

hook_spec() {
    echo "-i $1 -p tcp -m conntrack --ctstate NEW --ctdir ORIGINAL --ctorigdstport $PORT -j $CHAIN"
}

# Conteúdo do iptables-restore: a declaração da cadeia (que a esvazia na mesma transação) e as regras novas.
restore_text() {
    echo "*filter"
    echo ":$CHAIN - [0:0]"
    printf '%s\n' "$1" | while read -r net; do
        echo "-A $CHAIN -s $net -j RETURN"
    done
    echo "-A $CHAIN -j DROP"
    echo "COMMIT"
}

apply_family() { # $1 = 4 ou 6, $2 = faixas validadas, $3 = interface
    if [ "$1" = 4 ]; then
        ipt=iptables
        restore=iptables-restore
    else
        ipt=ip6tables
        restore=ip6tables-restore
    fi
    if ! "$ipt" -n -L DOCKER-USER >/dev/null 2>&1; then
        if [ "$1" = 6 ]; then
            echo "cf-firewall: IPv6: sem a cadeia DOCKER-USER, pulado (nada publicado por IPv6 para filtrar)" >&2
            return 0
        fi
        die "a cadeia DOCKER-USER não existe (o Docker está no ar?)"
    fi
    restore_text "$2" | "$restore" --noflush
    spec=$(hook_spec "$3")
    # shellcheck disable=SC2086 # a regra é uma lista de palavras de propósito
    "$ipt" -C DOCKER-USER $spec 2>/dev/null || "$ipt" -I DOCKER-USER 1 $spec
    jumps=$("$ipt" -S DOCKER-USER | grep -c -- " -j $CHAIN\$" || true)
    [ "$jumps" = 1 ] || die "IPv$1: $jumps ganchos para $CHAIN na DOCKER-USER (esperado 1): rode off e depois on"
    echo "cf-firewall: IPv$1: ligado ($(printf '%s\n' "$2" | wc -l) faixas, interface $3)"
}

remove_family() { # $1 = 4 ou 6
    if [ "$1" = 4 ]; then ipt=iptables; else ipt=ip6tables; fi
    if "$ipt" -n -L DOCKER-USER >/dev/null 2>&1; then
        "$ipt" -S DOCKER-USER | while read -r line; do
            case $line in
                "-A DOCKER-USER "*" -j $CHAIN")
                    # shellcheck disable=SC2086
                    set -- $line
                    shift
                    "$ipt" -D "$@"
                    ;;
            esac
        done
    fi
    if "$ipt" -n -L "$CHAIN" >/dev/null 2>&1; then
        "$ipt" -F "$CHAIN"
        "$ipt" -X "$CHAIN"
    fi
    echo "cf-firewall: IPv$1: desligado"
}

cmd_on() {
    need_root
    v4=$(valid_v4) || exit 1
    v6=$(valid_v6) || exit 1
    if4=$(public_interface -4)
    if6=$(public_interface -6)
    apply_family 4 "$v4" "$if4"
    apply_family 6 "$v6" "$if6"
}

cmd_off() {
    need_root
    remove_family 4
    remove_family 6
}

cmd_status() {
    need_root
    for fam in 4 6; do
        if [ "$fam" = 4 ]; then ipt=iptables; else ipt=ip6tables; fi
        if "$ipt" -n -L "$CHAIN" >/dev/null 2>&1; then
            echo "IPv$fam: cadeia $CHAIN presente; ganchos na DOCKER-USER: $("$ipt" -S DOCKER-USER 2>/dev/null | grep -c -- " -j $CHAIN\$" || true)"
            "$ipt" -L "$CHAIN" -n -v
        else
            echo "IPv$fam: cadeia $CHAIN ausente (firewall desligado)"
        fi
    done
}

cmd_check() {
    v4=$(valid_v4) || exit 1
    v6=$(valid_v6) || exit 1
    echo "cf-firewall: IPv4 $(printf '%s\n' "$v4" | wc -l) faixas, IPv6 $(printf '%s\n' "$v6" | wc -l) faixas: listas válidas"
}

cmd_print() {
    v4=$(valid_v4) || exit 1
    v6=$(valid_v6) || exit 1
    if4=$(public_interface -4)
    if6=$(public_interface -6)
    echo "# --- iptables-restore --noflush (IPv4) ---"
    restore_text "$v4"
    echo "# gancho (IPv4), depois da cadeia: iptables -I DOCKER-USER 1 $(hook_spec "$if4")"
    echo "# --- ip6tables-restore --noflush (IPv6) ---"
    restore_text "$v6"
    echo "# gancho (IPv6), depois da cadeia: ip6tables -I DOCKER-USER 1 $(hook_spec "$if6")"
}

cmd_arm_undo() {
    need_root
    minutes=${1:-10}
    case $minutes in
        '' | *[!0-9]*) die "os minutos devem ser um número inteiro" ;;
    esac
    { [ "$minutes" -ge 1 ] && [ "$minutes" -le 60 ]; } || die "minutos fora do intervalo 1 a 60"
    self=$(readlink -f "$0")
    systemd-run --quiet --unit="$UNDO_UNIT" --on-active="${minutes}min" "$self" off
    echo "cf-firewall: desfazer agendado para daqui a $minutes min (cancel-undo cancela)"
}

cmd_cancel_undo() {
    need_root
    if systemctl is-active --quiet "$UNDO_UNIT.timer"; then
        systemctl stop "$UNDO_UNIT.timer"
        echo "cf-firewall: desfazer cancelado"
    else
        echo "cf-firewall: nenhum desfazer agendado"
    fi
}

case ${1:-} in
    on) cmd_on ;;
    off) cmd_off ;;
    status) cmd_status ;;
    check) cmd_check ;;
    print) cmd_print ;;
    arm-undo) cmd_arm_undo "${2:-}" ;;
    cancel-undo) cmd_cancel_undo ;;
    *) die "uso: cf-firewall.sh on|off|status|check|print|arm-undo [MINUTOS]|cancel-undo" ;;
esac
