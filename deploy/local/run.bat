@echo off
rem Roda o EmpireMuse direto no Windows, sem container. NAO e o modo recomendado: use o Docker, veja deploy/README.md.
rem O que faz, e nada alem disso: recusa terminal de Administrador, confere que o uv existe, instala o Python 3.14 pelo uv,
rem cria o .venv, instala as dependencias do requirements-lock.txt COM verificacao de hash e inicia o bot pela raiz do repositorio.
rem Nao instala o uv, nao baixa mais nada, nao pede privilegio elevado e nunca le, mostra nem copia o .runtime\.env.
rem Banco: sem MONGO o bot usa o banco local; com o MongoDB do compose de deps use o host 127.0.0.1.
setlocal EnableExtensions
chcp 65001 >nul

rem Terminal elevado: "net session" e "fltmc" so respondem com sucesso quando e Administrador.
net session >nul 2>&1
if not errorlevel 1 goto elevado
fltmc >nul 2>&1
if not errorlevel 1 goto elevado
goto comum

:elevado
echo erro: não rode como Administrador: o bot não precisa de privilégio elevado. Abra um terminal comum. 1>&2
exit /b 1

:comum
where uv >nul 2>&1
if errorlevel 1 (
    echo erro: uv não encontrado. Instale-o pelo gerenciador de pacotes, como o winget, ou pelo binário da release com checksum. Veja docs/development.md. Este script não instala o uv. 1>&2
    exit /b 1
)

cd /d "%~dp0..\.."
if not exist "main.py" (
    echo erro: não achei main.py na raiz do repositório. Rode este script de dentro do repositório do EmpireMuse. 1>&2
    exit /b 1
)
if not exist "requirements-lock.txt" (
    echo erro: não achei requirements-lock.txt na raiz do repositório. 1>&2
    exit /b 1
)

rem Pasta de execução local: com .runtime presente o bot grava ali os dados e lê o .runtime\.env.
if not exist ".runtime" mkdir ".runtime"
if not exist ".runtime\.env" (
    echo aviso: .runtime\.env não existe. Crie-o a partir do .example.env. Sem ele o bot não terá token. 1>&2
)

uv python install 3.14
if errorlevel 1 exit /b 1
if not exist ".venv" (
    uv venv --python 3.14 .venv
    if errorlevel 1 exit /b 1
)
uv pip install --python .venv\Scripts\python.exe --require-hashes -r requirements-lock.txt
if errorlevel 1 exit /b 1

uv run --no-project python main.py
exit /b %ERRORLEVEL%
