#!/bin/sh
# Firewall da porta 443 do host: só as faixas da Cloudflare abrem conexões novas de fora (ver deploy/README.md).
#
# Uso: cf-firewall.sh COMANDO
#   on            valida as listas, monta a cadeia CF-ONLY de uma vez e só então liga os ganchos (DOCKER-USER; no IPv6
#                 também a INPUT, sempre)
#   off           tira os ganchos e apaga a cadeia (volta ao estado sem firewall; sem erro se nada estiver ligado)
#   status        mostra ganchos e contadores (precisa de root); sai com erro se o firewall estiver incompleto
#   check         só valida as duas listas e imprime as contagens (não precisa de root nem de iptables)
#   print         imprime o que o `on` aplicaria, sem aplicar nada (não precisa de root)
#   arm-undo [M]  agenda `off` daqui a M minutos (1 a 60; padrão 10) com systemd-run: rede de segurança antes do `on`
#   cancel-undo   cancela o desfazer agendado (e avisa, sem confirmar, se ele já começou)
#
# Como funciona: o Docker publica a 443 por DNAT, então o tráfego passa pela FORWARD e pela DOCKER-USER, não pelo INPUT.
# O gancho casa SÓ o primeiro pacote de uma conexão nova, na direção original, com destino original na porta 443, vindo da
# interface pública:
#   -m conntrack --ctstate NEW --ctdir ORIGINAL --ctorigdstport 443
# O `--ctstate NEW` é o que exclui as respostas das conexões HTTPS que os próprios containers abrem para fora (uma
# resposta nunca é NEW); o `--ctdir ORIGINAL` é defesa em profundidade e não deve ser removido junto com ele. A cadeia
# libera (RETURN) as faixas da Cloudflare e descarta (DROP) o resto.
#
# IPv6 e docker-proxy: se a rede do Traefik não tem IPv6 (ou o ip6tables do Docker está desligado), o Docker publica
# [::]:443 pelo docker-proxy, que é atendido pela INPUT e NUNCA passa pela DOCKER-USER. Por isso o `on` SEMPRE põe o mesmo
# gancho também na INPUT do ip6tables, só para a 443 (NEW, ORIGINAL, interface pública), haja ou não listener na hora:
# o Traefik pode subir depois do `on` (boot, recriação) e uma decisão tomada na hora do `on` deixaria a origem aberta por
# IPv6. O SSH e qualquer outra porta nunca são tocados. Se a INPUT do ip6tables não é filtrável (por exemplo
# ipv6.disable=1), o `on` falha fechado antes de aplicar qualquer coisa, e portanto o IPv4 também NÃO é ligado.
# O `status` falha se há listener em [::]:443 (ou se `ss` não existe: por precaução) e o gancho da INPUT não é exatamente 1.
#
# As faixas vêm de listas ESTÁTICAS e revisadas (padrão /etc/empire/cf-ips-v4.txt e cf-ips-v6.txt, as de deploy/host/):
# o script nunca baixa nada, então sem rede no boot ele continua funcionando. Cada lista é validada ANTES de tocar em
# qualquer regra; qualquer falha de validação ou de pré-condição sai com código diferente de 0 SEM tocar em regra. Se uma
# falha ocorrer depois de começar a aplicar (por exemplo o IPv6 depois do IPv4), o `on` desfaz tudo (`off`) e sai com erro,
# para não deixar metade ligado; a unit fica `failed` e o `status`/`systemctl is-active` pegam.
#   - cada linha é um CIDR válido, com barra, da família certa (comentário # e linha vazia são ignorados);
#   - prefixo no mínimo /12 (IPv4) e /29 (IPv6): isso recusa 0.0.0.0/0 e ::/0, que liberariam tudo;
#   - só endereços globais (nada privado, de loopback ou reservado), sem linha repetida;
#   - no mínimo 10 faixas IPv4 e 5 IPv6.
# A troca é atômica: o iptables-restore --noflush recebe o conteúdo inteiro da cadeia numa transação só, então a cadeia
# nunca fica vazia nem só com o DROP, e rodar `on` de novo (o systemd roda quando o Docker reinicia) troca as faixas sem
# janela. O gancho entra uma vez só, depois de a cadeia estar cheia.
#
# Concorrência: on, off e cancel-undo rodam sob um flock exclusivo (CF_FW_LOCK_FILE, padrão /var/lock/cf-firewall.lock; espera
# até CF_FW_LOCK_TIMEOUT segundos, padrão 60) que vale do começo ao fim do comando, inclusive o rollback. Sem isso, um `on`
# manual e o da unit poderiam ver o gancho ausente ao mesmo tempo, e o rollback de um apagaria as regras do outro.
#
# Variáveis (para teste e casos especiais): CF_FW_LIST_DIR (pasta das listas), CF_FW_PUB_IF (interface pública; sem ela,
# a da rota padrão, e para IPv6 a da rota padrão IPv6 se houver).
set -eu
set -f # as faixas e as linhas do iptables são palavras soltas: nunca expandir globs

LIST_DIR=${CF_FW_LIST_DIR:-/etc/empire}
CHAIN=CF-ONLY
PORT=443
UNDO_UNIT=cf-fw-rollback
MIN_V4_PREFIX=12
MIN_V6_PREFIX=29
MIN_V4_COUNT=10
MIN_V6_COUNT=5
LOCK_FILE=${CF_FW_LOCK_FILE:-/var/lock/cf-firewall.lock}
LOCK_TIMEOUT=${CF_FW_LOCK_TIMEOUT:-60}

die() {
    echo "cf-firewall: $*" >&2
    exit 1
}

need_root() {
    [ "$(id -u)" = 0 ] || die "precisa rodar como root"
}

# Reexecuta o script inteiro sob flock exclusivo (o lock dura até o processo sair). $@ = argumentos originais.
run_locked() {
    [ -z "${CF_FW_LOCKED:-}" ] || return 0
    command -v flock >/dev/null 2>&1 || die "flock não encontrado (util-linux): sem ele não há exclusão mútua"
    case $LOCK_TIMEOUT in
        '' | *[!0-9]*) die "CF_FW_LOCK_TIMEOUT deve ser um inteiro" ;;
    esac
    CF_FW_LOCKED=1 exec flock -x -w "$LOCK_TIMEOUT" "$LOCK_FILE" /bin/sh "$(readlink -f "$0")" "$@"
}

# Valida uma lista e imprime as faixas normalizadas, uma por linha. Argumentos: família (4 ou 6), prefixo mínimo,
# quantidade mínima e caminho.
validate_list() {
    python3 -I - "$1" "$2" "$3" "$4" <<'PY'
import ipaddress
import re
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
    if "%" in line:
        fail(f"linha {number}: escopo de interface (%) não é aceito")
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
SHAPE = re.compile(r"^[0-9A-Fa-f:.]+/[0-9]{1,3}$")
for network in networks:
    text = str(network)
    if not SHAPE.match(text):
        fail(f"formato inesperado: {text}")
    print(text)
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

fam_cmd() { # $1 = 4 ou 6; define ipt e restore
    if [ "$1" = 4 ]; then
        ipt=iptables
        restore=iptables-restore
    else
        ipt=ip6tables
        restore=ip6tables-restore
    fi
}

# Usado só pelo `status`. Sucesso se há (ou, sem `ss`, pode haver) um listener TCP IPv6 em [::]:443: o docker-proxy.
v6_listener_443() {
    command -v ss >/dev/null 2>&1 || return 0
    [ -n "$(ss -H -ltn -6 "sport = :$PORT" 2>/dev/null)" ]
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

# Quais cadeias recebem o gancho nesta família. IPv4: DOCKER-USER. IPv6: DOCKER-USER se existir e sempre a INPUT.
# Imprime os nomes, um por linha; sai com erro (e mensagem) se algo que precisa ser filtrado não pode ser.
hook_parents() { # $1 = 4 ou 6
    fam_cmd "$1"
    parents=""
    if "$ipt" -n -L DOCKER-USER >/dev/null 2>&1; then
        parents="DOCKER-USER"
    elif [ "$1" = 4 ]; then
        die "a cadeia DOCKER-USER não existe (o Docker está no ar?)"
    fi
    if [ "$1" = 6 ]; then
        "$ipt" -n -L INPUT >/dev/null 2>&1 || die "IPv6: o ip6tables não filtra a INPUT (ipv6.disable=1?): falha fechada, nada foi aplicado (o IPv4 também não foi ligado)"
        parents="$parents INPUT"
    fi
    echo "${parents# }"
}

apply_family() { # $1 = 4 ou 6, $2 = faixas validadas, $3 = interface, $4 = cadeias de gancho
    fam_cmd "$1"
    [ -n "$4" ] || return 0
    restore_text "$2" | "$restore" --noflush
    spec=$(hook_spec "$3")
    for parent in $4; do
        # shellcheck disable=SC2086 # a regra é uma lista de palavras de propósito
        "$ipt" -C "$parent" $spec 2>/dev/null || "$ipt" -I "$parent" 1 $spec
        jumps=$("$ipt" -S "$parent" | grep -c -- " -j $CHAIN\$" || true)
        [ "$jumps" = 1 ] || die "IPv$1: $jumps ganchos para $CHAIN na $parent (esperado 1): rode off e depois on"
    done
    echo "cf-firewall: IPv$1: ligado ($(printf '%s\n' "$2" | wc -l) faixas, interface $3, ganchos: $4)"
}

delete_rule() { # $1 = linha de iptables -S (-A CADEIA ...); apaga essa regra (as palavras viram argumentos de -D)
    # shellcheck disable=SC2086
    set -- $1
    shift
    "$ipt" -D "$@"
}

# Tira ganchos e cadeia. Devolve erro (e diz o que restou) se qualquer remoção falhar; só diz "desligado" se tudo saiu.
remove_family() { # $1 = 4 ou 6
    fam=$1
    fam_cmd "$fam"
    failed=0
    for parent in DOCKER-USER INPUT; do
        "$ipt" -n -L "$parent" >/dev/null 2>&1 || continue
        rules=$("$ipt" -S "$parent") || {
            failed=1
            continue
        }
        # here-doc, não pipe: o laço roda neste shell e não perde o status
        while read -r line; do
            case $line in
                "-A $parent "*" -j $CHAIN") delete_rule "$line" || failed=1 ;;
            esac
        done <<EOF
$rules
EOF
    done
    if "$ipt" -n -L "$CHAIN" >/dev/null 2>&1; then
        "$ipt" -F "$CHAIN" || failed=1
        "$ipt" -X "$CHAIN" || failed=1
    fi
    if [ "$failed" = 0 ]; then
        echo "cf-firewall: IPv$fam: desligado"
        return 0
    fi
    echo "cf-firewall: IPv$fam: FALHA ao remover; restou (confira com status e rode off de novo):" >&2
    "$ipt" -S 2>/dev/null | grep -- "$CHAIN" >&2 || true
    return 1
}

rollback_on_error() {
    trap - EXIT
    echo "cf-firewall: falha ao aplicar: desfazendo tudo (off) para não deixar metade ligado" >&2
    remove_family 4 || echo "cf-firewall: ATENÇÃO: o rollback do IPv4 não terminou" >&2
    remove_family 6 || echo "cf-firewall: ATENÇÃO: o rollback do IPv6 não terminou" >&2
}

cmd_on() {
    need_root
    run_locked on
    v4=$(valid_v4) || exit 1
    v6=$(valid_v6) || exit 1
    if4=$(public_interface -4)
    if6=$(public_interface -6)
    # Tudo o que pode falhar por pré-condição falha AQUI, antes de tocar em qualquer regra.
    parents4=$(hook_parents 4) || exit 1
    parents6=$(hook_parents 6) || exit 1
    trap rollback_on_error EXIT
    apply_family 4 "$v4" "$if4" "$parents4"
    apply_family 6 "$v6" "$if6" "$parents6"
    trap - EXIT
}

cmd_off() {
    need_root
    run_locked off
    rc=0
    remove_family 4 || rc=1
    remove_family 6 || rc=1
    [ "$rc" = 0 ] || die "off incompleto (ver acima)"
}

hooks_count() { # $1 = família, $2 = cadeia pai
    fam_cmd "$1"
    "$ipt" -S "$2" 2>/dev/null | grep -c -- " -j $CHAIN\$" || true
}

cmd_status() {
    need_root
    bad=0
    for fam in 4 6; do
        fam_cmd "$fam"
        if "$ipt" -n -L "$CHAIN" >/dev/null 2>&1; then
            du=$(hooks_count "$fam" DOCKER-USER)
            inp=$(hooks_count "$fam" INPUT)
            echo "IPv$fam: cadeia $CHAIN presente; ganchos na DOCKER-USER: $du; na INPUT: $inp"
            "$ipt" -L "$CHAIN" -n -v
            [ $((du + inp)) -ge 1 ] || bad=1
            [ "$fam" = 4 ] && [ "$du" != 1 ] && bad=1
            if [ "$fam" = 6 ]; then
                [ "$du" -le 1 ] || bad=1
                if v6_listener_443; then
                    [ "$inp" = 1 ] || {
                        echo "IPv6: ATENÇÃO: há (ou pode haver) listener em [::]:$PORT e o gancho da INPUT não é exatamente 1 ($inp)" >&2
                        bad=1
                    }
                else
                    [ "$inp" -le 1 ] || bad=1
                fi
            fi
        else
            echo "IPv$fam: cadeia $CHAIN ausente (firewall desligado)"
            [ "$fam" = 4 ] && bad=1
            if [ "$fam" = 6 ] && v6_listener_443; then
                echo "IPv$fam: ATENÇÃO: há listener em [::]:$PORT e nenhum filtro" >&2
                bad=1
            fi
        fi
    done
    [ "$bad" = 0 ] || die "firewall incompleto ou desligado (ver acima)"
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
    echo "# gancho (IPv6, sempre: cobre o docker-proxy em [::]:$PORT): ip6tables -I INPUT 1 $(hook_spec "$if6")"
}

cmd_arm_undo() {
    need_root
    minutes=${1:-10}
    case $minutes in
        '' | *[!0-9]*) die "os minutos devem ser um número inteiro" ;;
    esac
    { [ "$minutes" -ge 1 ] && [ "$minutes" -le 60 ]; } || die "minutos fora do intervalo 1 a 60"
    self=$(readlink -f "$0")
    # Um desfazer anterior que falhou deixa a unit transitória em failed e o nome ocupado.
    systemctl reset-failed "$UNDO_UNIT.service" "$UNDO_UNIT.timer" 2>/dev/null || true
    # /bin/sh: o arquivo pode não ter o bit de execução (modo 0644 no git).
    systemd-run --quiet --unit="$UNDO_UNIT" --on-active="${minutes}min" /bin/sh "$self" off
    echo "cf-firewall: desfazer agendado para daqui a $minutes min (cancel-undo cancela)"
    systemctl list-timers "$UNDO_UNIT.timer" --no-pager || true
}

cmd_cancel_undo() {
    need_root
    run_locked cancel-undo
    if systemctl is-active --quiet "$UNDO_UNIT.timer"; then
        systemctl stop "$UNDO_UNIT.timer"
        # O timer pode ter vencido entre o is-active e o stop: o serviço do desfazer (que roda o off) já seria dele. Parar o
        # timer não o para, e não o interrompemos (um off pela metade é pior). Se ele começou, não confirmamos o cancelamento.
        state=$(systemctl show -p ActiveState --value "$UNDO_UNIT.service" 2>/dev/null || true)
        case $state in
            active | activating | deactivating)
                die "o desfazer JÁ COMEÇOU (serviço $state) e vai rodar o off: o cancelamento NÃO vale. Quando ele terminar, rode status e, se o firewall caiu, rode on"
                ;;
        esac
        echo "cf-firewall: desfazer cancelado"
    elif [ "$(hooks_count 4 DOCKER-USER)" = 0 ]; then
        echo "cf-firewall: ATENÇÃO: nenhum desfazer agendado E o gancho IPv4 não está na DOCKER-USER: o desfazer provavelmente já rodou e o firewall está DESLIGADO" >&2
    else
        echo "cf-firewall: nenhum desfazer agendado"
    fi
    systemctl reset-failed "$UNDO_UNIT.service" "$UNDO_UNIT.timer" 2>/dev/null || true
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
