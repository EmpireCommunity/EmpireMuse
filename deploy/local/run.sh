#!/bin/sh
# Roda o EmpireMuse direto na máquina, sem container. NÃO é o modo recomendado (use o Docker: deploy/README.md); serve para quem
# não pode ou não quer usar container. Linux e macOS. No Windows, use deploy/local/run.bat.
#
# O que faz, e nada além disso: confere que não é root e que o uv existe, instala o Python 3.14 pelo uv, cria o .venv,
# instala as dependências do requirements-lock.txt COM verificação de hash e inicia o bot pela raiz do repositório.
# Não instala o uv, não baixa mais nada, não pede privilégio elevado e nunca lê, mostra nem copia o .runtime/.env.
# Banco: sem MONGO o bot usa o banco local; com o MongoDB do compose de deps (deploy/docker-compose.deps.yml) use o host 127.0.0.1.
set -eu
umask 077

aviso() {
    printf '%s\n' "aviso: $*" >&2
}

erro() {
    printf '%s\n' "erro: $*" >&2
    exit 1
}

if [ "$(id -u)" -eq 0 ]; then
    erro "não rode como root: o bot não precisa de privilégio elevado. Use um usuário comum."
fi

if ! command -v uv >/dev/null 2>&1; then
    erro "uv não encontrado. Instale-o pelo gerenciador de pacotes do sistema, pelo pipx ou pelo binário da release com checksum (veja docs/development.md). Este script não instala o uv."
fi

aqui=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
raiz=$(CDPATH='' cd -- "$aqui/../.." && pwd)
cd -- "$raiz"

if [ ! -f main.py ] || [ ! -f requirements-lock.txt ]; then
    erro "não achei main.py e requirements-lock.txt em $raiz. Rode este script de dentro do repositório do EmpireMuse."
fi

# Pasta de execução local: com .runtime/ presente o bot grava ali os dados e lê o .runtime/.env (docs/development.md).
mkdir -p .runtime

env_file=.runtime/.env
if [ ! -f "$env_file" ]; then
    aviso "$env_file não existe. Crie-o a partir do .example.env e restrinja: chmod 600 $env_file. Sem ele o bot não terá token."
else
    # Só as permissões (nunca o conteúdo): algum bit de leitura, escrita ou execução para grupo ou outros.
    if [ -n "$(find "$env_file" -prune \( -perm -040 -o -perm -020 -o -perm -010 -o -perm -004 -o -perm -002 -o -perm -001 \) -print)" ]; then
        aviso "$env_file pode ser lido por grupo ou outros usuários. Restrinja: chmod 600 $env_file"
    fi
fi

uv python install 3.14
if [ ! -d .venv ]; then
    uv venv --python 3.14 .venv
fi
uv pip install --python .venv/bin/python --require-hashes -r requirements-lock.txt

exec uv run --no-project python main.py
