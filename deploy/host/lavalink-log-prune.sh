#!/bin/sh
# Apaga do volume de dados as cópias gz rotacionadas do spring.log do Lavalink local com mais de N dias.
# O Lavalink gira o spring.log por dia mas nunca apaga as cópias; este script é a única limpeza.
#
# Uso: lavalink-log-prune.sh [PASTA [DIAS]]
#   PASTA  termina em /.logs/lavalink (padrão: o volume empiremuse_empiremuse-data da VPS)
#   DIAS   30 a 183 (padrão 30; 183 se o spring.log trouxer ID de usuário ou servidor, ver docs/runbook/log-retention.md)
#   DRY_RUN=1 só lista o que apagaria.
#
# Só apaga arquivo regular (nunca link simbólico) cujo nome é exatamente spring.log.AAAA-MM-DD.N.gz, direto na pasta,
# e nunca o spring.log em uso. A idade é a do arquivo (a compressão mantém a data da última gravação do dia).
# O caminho da PASTA precisa ser o canônico (sem nenhum link simbólico em nenhum componente, inclusive .logs): quem
# controla o volume não pode trocar um componente por um link para apagar fora dele. O serviço systemd roda com o dono do
# volume, não como root, para que mesmo uma troca no meio da execução alcance só o que esse usuário pode apagar.
set -eu

DEFAULT_DIR=/var/lib/docker/volumes/empiremuse_empiremuse-data/_data/.logs/lavalink
LOG_DIR=${1:-$DEFAULT_DIR}
MAX_AGE_DAYS=${2:-30}

case $MAX_AGE_DAYS in
    '' | *[!0-9]*)
        echo "DIAS deve ser um número inteiro" >&2
        exit 2
        ;;
esac
if [ "$MAX_AGE_DAYS" -lt 30 ] || [ "$MAX_AGE_DAYS" -gt 183 ]; then
    echo "DIAS fora do intervalo 30 a 183: $MAX_AGE_DAYS" >&2
    exit 2
fi

case $LOG_DIR in
    */.logs/lavalink) ;;
    *)
        echo "PASTA deve terminar em /.logs/lavalink: $LOG_DIR" >&2
        exit 2
        ;;
esac
if [ ! -d "$LOG_DIR" ]; then
    echo "pasta inexistente: $LOG_DIR" >&2
    exit 1
fi
CANONICAL_DIR=$(cd "$LOG_DIR" && pwd -P)
if [ "$CANONICAL_DIR" != "$LOG_DIR" ]; then
    echo "a pasta não é o caminho canônico (há link simbólico ou caminho relativo): $LOG_DIR -> $CANONICAL_DIR" >&2
    exit 2
fi

# O regex casa o caminho inteiro: a pasta (já canônica) seguida do nome exato.
NAME_REGEX="$LOG_DIR/spring\\.log\\.[0-9]{4}-[0-9]{2}-[0-9]{2}\\.[0-9]+\\.gz"

if [ "${DRY_RUN:-0}" = "1" ]; then
    echo "DRY_RUN: arquivos que seriam apagados (mais de $MAX_AGE_DAYS dias):"
    find "$LOG_DIR" -maxdepth 1 -type f -regextype posix-extended -regex "$NAME_REGEX" -mtime +"$MAX_AGE_DAYS" -print
    exit 0
fi

find "$LOG_DIR" -maxdepth 1 -type f -regextype posix-extended -regex "$NAME_REGEX" -mtime +"$MAX_AGE_DAYS" -print -delete
