#!/bin/sh
# Roda o EmpireMuse direto na máquina, sem container. NÃO é o modo recomendado (use o Docker: deploy/README.md); serve para quem
# não pode ou não quer usar container. Linux e macOS. No Windows, use deploy/local/run.bat.
#
# O que faz, e nada além disso: confere que não é root e que o uv existe, instala o Python 3.14 pelo uv, cria o .venv,
# instala as dependências do requirements-lock.txt COM verificação de hash e inicia o bot pela raiz do repositório.
# Não instala o uv, não pede privilégio elevado, não baixa nada além do Python 3.14 (instalado pelo uv, que confere o hash) e das
# dependências do lock (conferidas por hash), e nunca lê, mostra nem copia o arquivo .env.
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

# Pasta de execução local: com .runtime/ presente o bot grava ali os dados e lê o .runtime/.env (docs/development.md). Criá-la
# MUDA onde o bot lê e grava; por isso, se já houver .env, config.json ou local_database na raiz, ela não é criada e o bot segue na raiz.
env_file=.runtime/.env
if [ ! -d .runtime ]; then
    if [ -e .env ] || [ -e config.json ] || [ -d local_database ]; then
        aviso "achei .env, config.json ou local_database na raiz: NÃO criei a pasta .runtime (ela mudaria onde o bot lê e grava). O bot segue usando a raiz. Para migrar, crie .runtime e mova esses itens para lá."
        env_file=.env
    else
        mkdir -p .runtime
    fi
fi

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
elif ! .venv/bin/python -c 'import sys; raise SystemExit(sys.version_info < (3, 14))'; then
    erro "o .venv existente não usa o Python 3.14 ou mais novo. Apague a pasta .venv (rm -r .venv) e rode de novo; este script não apaga sozinho."
fi
uv pip install --python .venv/bin/python --require-hashes -r requirements-lock.txt

# O Python do .venv direto: um ambiente virtual ativo no terminal não pode tomar o lugar dele.
exec .venv/bin/python main.py
