# Guia de desenvolvimento local

Como preparar o ambiente para desenvolver e testar o EmpireMuse. Para entender o código, leia antes
[`architecture.md`](architecture.md); para bugs conhecidos, [`known-issues.md`](known-issues.md).

## 1. Requisitos

| Item | Versão | Observação |
|---|---|---|
| Python | **3.14** | Runtime e **mínimo** do projeto (item 6): o código usa `except A, B:` (PEP 758) e não importa no 3.13. A CI também roda o 3.15 sem bloquear. `audioop-lts` (`requirements.txt`) cobre o módulo que saiu da biblioteca padrão. |
| [uv](https://docs.astral.sh/uv/) | recente | Gerenciador de venv/pacotes recomendado. Instale-o como na seção 4.1. |
| Git | qualquer | Só para clonar o repositório e os submódulos; nenhuma dependência Python é instalada via Git. |
| Java | **17+** | **Só** se for rodar o Lavalink local. Sem Java 17+, o bot tenta baixar um JDK sozinho (`validate_java` e `run_lavalink` em `empiremuse/utils/music/local_lavalink.py`). |
| Aplicação no Discord | — | Token do bot em <https://discord.com/developers/applications>, com as *Privileged Intents* ativadas (a falta delas é detectada e explicada na inicialização, `BotPool.start_bot` em `empiremuse/utils/client.py`). |

## 2. Ambiente virtual e instalação

```bash
git clone https://github.com/EmpireCommunity/EmpireMuse.git
cd EmpireMuse

uv venv --python 3.14
source .venv/bin/activate          # Windows: .venv\Scripts\activate

uv pip install --require-hashes -r requirements-lock.txt
```

Observações:

- `disnake` vem do PyPI (`disnake==2.12.2`) e o `jishaku` é o fork `empiremuse-jishaku` (release `v2.6.6.post3` de [EmpireCommunity/EmpireMuse-jishaku](https://github.com/EmpireCommunity/EmpireMuse-jishaku)), instalado pelo wheel da release por URL. Ambos estão no `requirements-lock.txt` com hash SHA-256; não há dependência VCS.
- As dependências do PyPI são travadas com hashes SHA-256 no arquivo de lock universal `requirements-lock.txt` (compilado via `uv pip compile --generate-hashes --universal --exclude-newer <corte>`), garantindo integridade criptográfica da cadeia de suprimentos (conforme `.claude/rules/empiremuse/security.md` §8).
- **Suporte universal multiplataforma (Windows/Linux)**: os lockfiles utilizam resolução universal (`--universal`), preservando marcadores de ambiente como `uvloop==... ; sys_platform != 'win32'`. Isso viabiliza a instalação nativa tanto em ambientes Linux/Docker quanto no Windows (onde `uvloop` é automaticamente ignorado sem quebrar o processo).
- **Janela de corte temporal (`--exclude-newer`)**: a compilação dos lockfiles adota corte temporal de segurança de aproximadamente 7 dias para mitigar riscos de supply chain de pacotes recém-publicados.
- O arquivo `requirements.txt` permanece como especificação de alto nível do projeto e ponto de entrada para plataformas gerenciadas (como a Discloud).
- Dependências obsoletas expurgadas (Bloco C2): 8 bibliotecas declaradas sem nenhum uso no código (`aiosqlite`, `syncedlyrics`, `pylrc`, `undetected-chromedriver`, `nest_asyncio`, `curl_cffi`, `emoji`, `regex`) foram removidas de `requirements.txt`, saneando a superfície de dependências e resolvendo o item KI-05 (ver [`known-issues.md`](known-issues.md)).

## 3. Configuração

```bash
cp .example.env .env
```

Se usar a pasta `.runtime/` (seção 4, "Pasta de execução local"), o bot lê `.runtime/.env`: copie para lá (`cp .example.env .runtime/.env`).

Edite o `.env` (se um valor tiver espaço, use aspas). **Nunca** versione o `.env`, o `config.json` nem
qualquer token (`.env` e `config.json` já são ignorados pelo git; nunca use `git add -f` neles).

### Precedência (`load_config` em `empiremuse/config_loader.py`)

Do menor para o maior peso — o último vence:

1. `DEFAULT_CONFIG` em `empiremuse/config_loader.py`;
2. variáveis de ambiente — **somente** as chaves de `DEFAULT_CONFIG`, `TOKEN`, `MONGO` e as que começam
   com `TOKEN_BOT_`, `TEST_GUILDS_` e `LAVALINK_NODE_`;
3. `config.json` na raiz (JSON simples `{"CHAVE": "valor"}`);
4. `.env` — **sobrescreve** todos os anteriores.

Depois da mescla, números e booleanos são convertidos. Os booleanos são convertidos via `bools` em
`empiremuse/config_loader.py` e aceitam `true`/`false`/`none` em qualquer caixa (`True`, `FALSE`, etc.);
apenas valores fora de `true`/`false`/`none` geram erro na inicialização citando a chave configurada.
As configurações numéricas `VOTE_SKIP_AMOUNT`, `HINT_RATE`, `DBCACHE_SIZE`, `PLAYLIST_CACHE_SIZE` e `MONGO_TIMEOUT`
possuem piso mínimo de `1`: valores informados iguais a `0` ou negativos são automaticamente elevados para `1`.
Valores de tipo incompatível em chaves inteiras geram erro explicativo na inicialização indicando a chave configurada.

### Variáveis essenciais

| Variável | Para quê |
|---|---|
| `TOKEN` | Token(s) do(s) bot(s). Vários tokens: separados por espaço. Qualquer chave cujo valor contenha um token válido também é aceita (`BotPool.setup` em `empiremuse/utils/client.py`). |
| `DEFAULT_PREFIX` | Prefixo dos comandos de texto (padrão `!!`). |
| `OWNER_IDS` | IDs de donos extras, separados por `\|\|` (`BotCore.__init__` em `empiremuse/utils/client.py`). |
| `MONGO` | URL do MongoDB. **Vazio = TinyMongo local** em `local_database` sob o diretório de dados/runtime (configurável via `EMPIREMUSE_HOME`; `./local_database` por padrão em desenvolvimento). |
| `MONGO_SSL_FIX` | Só para depuração: com `true` (também `1`, `yes`, `on`, sem diferenciar maiúsculas) desliga a validação do certificado TLS do `MONGO` (CWE-295) e registra um WARNING no startup, sem a URI. Vazio, ausente, `false` ou `0` mantêm a validação. Qualquer outro valor deixou de desligá-la. |
| `PLAYER_SESSIONS_MONGODB` | Guardar sessões de players no Mongo (exige `MONGO`). |
| `RUN_RPC_SERVER` / `PORT` | Liga a página de status e o WebSocket do RPC (tornado). O padrão é `false` (`DEFAULT_CONFIG` em `empiremuse/config_loader.py` e `.example.env`). Ligue `RUN_RPC_SERVER=true` só se for testar o RPC (com `PORT=6000`; a porta padrão é 6000); `ENABLE_RPC_AUTH` já vem `true` por padrão; mantenha-o assim e não exponha a porta publicamente (em ambientes compartilhados, mantenha a porta privada). |
| `RPC_SERVER_SECRET` | Segredo que o bot usa para se registrar no servidor RPC (cabeçalho X-RPC-Key). Vazio: o bot e o servidor RPC do mesmo processo usam um segredo gerado automaticamente a cada inicialização. Defina o MESMO valor nos dois lados quando o servidor RPC rodar em outra instância. Trate como senha e use wss://. |
| `RPC_ALLOWED_ORIGINS` | Origens de navegador autorizadas a abrir o WebSocket do RPC, separadas por vírgula (ex.: https://painel.exemplo.com). Vazio: só clientes sem cabeçalho Origin (app, bot) e a mesma origem do servidor são aceitos. |
| `WEB_BIND_ADDRESS` | Endereço de escuta do servidor web/RPC. 0.0.0.0 aceita conexões de qualquer interface (necessário em Docker e Pterodactyl); use 127.0.0.1 para aceitar apenas conexões locais ou um proxy reverso no mesmo host. Com 0.0.0.0 e ENABLE_RPC_AUTH=false, um aviso é registrado na inicialização. |
| `RUN_LOCAL_LAVALINK` | Inicia um Lavalink local (ver seção 5). |
| `AUTO_DOWNLOAD_LAVALINK_SERVERLIST` | Baixa a lista pública de Lavalink (ver seção 5). |
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | Credenciais do app Spotify (*client credentials*). Sem elas, o suporte interno ao Spotify fica desativado (`BotPool.setup` em `empiremuse/utils/client.py`). |
| `LASTFM_KEY` / `LASTFM_SECRET` | Integração com o Last.fm (opcional). |
| `TEST_GUILDS_<NOME_DO_BOT>` | IDs de servidores de teste (separados por `\|\|`) para registrar comandos só neles (`BotPool.setup` em `empiremuse/utils/client.py`). |
| `SUPPORT_SERVER` | Convite do servidor de suporte exibido no `/about` e em mensagens de erro (padrão aponta para o EmpireHUB). Em instâncias próprias, altere para o seu servidor ou deixe vazio (`SUPPORT_SERVER=''`) para não exibir o link. |
| `APP_DESCRIPTION` / `APP_TAGS` / `APP_DESCRIPTION_RESERVE` | Perfil da aplicação no Discord (descrição e até 5 tags de 20 caracteres, separadas por vírgula): cada bot grava o próprio ao iniciar, só se diferir. `APP_DESCRIPTION` vale para o bot principal (o do `TOKEN`) e `APP_DESCRIPTION_RESERVE` para todos os outros. Os dois textos aceitam `{prefix}`, `{bots}`, `{panel_url}`, `{support_url}` e `{main}` (nome do bot principal); a linha cujo marcador fica vazio sai e `\n` vale como quebra de linha; passou de 400 caracteres, é encurtada com um aviso. Vazio = não mexer no campo. |
| `ENABLE_JISHAKU` | Carrega o jishaku (execução de código e de shell pelo Discord, só para donos). Desligado por padrão (`false`); ligue só se precisar e confie em quem está em `OWNER_IDS`. O `shell` do owner roda com ambiente mínimo e devolve a saída mascarada só por DM. |
| `EMBED_COLOR` | Cor dos embeds (hex de 6 dígitos; padrão `8B5CF6`). Defina `EMBED_COLOR=none` para restaurar e utilizar a cor do cargo mais alto do bot no servidor. |
| `*_INTENT`, `MESSAGE_CONTENT_INTENT` etc. | Intents; os padrões estão em `DEFAULT_CONFIG` (`empiremuse/config_loader.py`). |

A lista completa, comentada, está em `.example.env`. O log (`LOG_LEVEL`, `LOG_COLOR`, `LOG_FILE`, `LOG_DEBUG_GUILDS`, `METRICS_INTERVAL`) é descrito no
[guia de logs](guides/logs.md): fora do Docker o arquivo `./.logs/disnake.log` (rotativo, 5 MB × 3) vem ligado (`LOG_FILE=auto`).
`COMMAND_LOG=true` registra os comandos de usuários que não são donos, só com IDs e opções resumidas. Em instâncias
próprias (self-hosting), certifique-se de configurar `SUPPORT_SERVER` (com seu convite ou vazio) e `EMBED_COLOR`
conforme a identidade desejada.

### Suporte e Códigos de Erro (`ERR-XXXXXX`)

Em caso de exceções inesperadas em comandos de barra ou prefixados, mensagens da Jukebox (`on_jukebox_message`), eventos de reprodução (`LavalinkPlayer`), painel do dono e na página de erro 500 do painel web, o bot gera um código de identificação único de 6 caracteres hexadecimais no padrão `ERR-XXXXXX` (via `new_error_code` em `empiremuse/rules/error_code.py` usando `secrets.token_hex`, com geração segura garantida por `safe_error_code`). O detalhe técnico da exceção e o traceback completo são registrados unicamente no log do servidor (e no webhook de erros, se configurado); o usuário recebe apenas uma mensagem genérica neutra com o código para informar ao suporte.

Para investigar uma falha reportada cruzando o código:
- **Log em arquivo**: O log em arquivo vem ligado fora do Docker (`LOG_FILE=auto`); `ENABLE_LOGGER=true` é só o sinônimo legado de `LOG_FILE=on`, e no Docker é preciso `LOG_FILE=on` e um volume. Fica no caminho `RUNTIME_DIR/.logs/disnake.log` (sob `EMPIREMUSE_HOME` ou diretório atual). O arquivo é gravado em append com rotação (5 MB × 3 cópias, `build_file_log_handler` em `empiremuse/utils/file_log.py`), então os códigos `ERR-XXXXXX` sobrevivem a uma reinicialização. O log redige o token das URLs de webhook (`/api/webhooks/<id>/***`) na mensagem e no traceback; a pasta `.logs` é criada com modo `0o700` e o arquivo com `0o600` (ignorado onde o SO não suporta, como no Windows). Mesmo assim, não compartilhe o arquivo sem revisá-lo.
- **Webhook de erros (recomendado para histórico)**: Para reter o histórico persistente entre reinicializações, recomenda-se configurar `AUTO_ERROR_REPORT_WEBHOOK` para o envio automático de relatórios detalhados com o código `ERR-XXXXXX` no embed e no anexo do traceback, ou `ERROR_REPORT_WEBHOOK` para exibir o botão manual "Reporte esse erro" aos usuários.

## 4. Como rodar

```bash
source .venv/bin/activate
python -m empiremuse
```

O comando canônico de execução é `python -m empiremuse` (via módulo `empiremuse.__main__`). O arquivo `main.py` na raiz do repositório funciona como atalho direto (`python main.py`).

### Diretório de execução e dados (`EMPIREMUSE_HOME`)

Por padrão, o diretório de dados em tempo de execução (`RUNTIME_DIR` em `empiremuse/paths.py`) corresponde a `.runtime/` na raiz do projeto quando essa pasta existir, ou ao diretório atual de trabalho (`Path.cwd()`) caso contrário. Para isolar ou direcionar os arquivos de estado do processo (banco de dados local `local_database/`, binários e arquivos do Lavalink local `Lavalink.jar`, `application.yml`, diretório `plugins/`, arquivos de log `.logs/`, `lavalink.ini` e `auto_lavalink.ini`) para outro caminho arbitrário, defina a variável de ambiente `EMPIREMUSE_HOME`:

```bash
export EMPIREMUSE_HOME=/caminho/para/dados
python -m empiremuse
```

### Pasta de execução local (`.runtime/`)

Para manter a raiz do repositório limpa durante o desenvolvimento local sem precisar definir variáveis de ambiente a cada sessão:

- Crie a pasta `.runtime/` na raiz do repositório e restrinja as permissões (`mkdir .runtime && chmod 700 .runtime`).
- Com `.runtime/` presente, o bot passa a gravar ali todos os dados de execução, bancos locais (`local_database/`), caches, logs (`.logs/`), sincronização de comandos e binários do Lavalink e do Java (`.java/`, `plugins/`, `Lavalink.jar`, `application.yml`). Na subida, o bot registra no log qual pasta de execução escolheu.
- Os arquivos `.env` e `config.json` também ficam em `.runtime/` (ex.: `.runtime/.env` e `.runtime/config.json`). Os composes de `deploy/` são de deploy e continuam lendo o `.env` da raiz, enquanto o bot em desenvolvimento lê `.runtime/.env`.
- No modo `.runtime`, os comandos do bot que atualizam ou exportam o código pelo Git não funcionam; use o `git` direto no terminal.
- A pasta `.runtime/` nunca vai para a Discloud nem para a imagem Docker, pois está incluída nos arquivos de ignorar (`.gitignore`, `.dockerignore` e `.discloudignore`).
- A variável `EMPIREMUSE_HOME`, se definida, tem precedência sobre `.runtime/`.
- Sem a pasta `.runtime/` (e sem `EMPIREMUSE_HOME`), o comportamento antigo continua valendo: o bot utiliza a pasta atual de trabalho.

Na primeira execução:

- os comandos de barra são sincronizados com o Discord (`AUTO_SYNC_COMMANDS`, padrão `true`) e o estado
  fica em `.app_commands_sync_data/` sob o diretório de dados — a sincronização é pulada se nada mudou (`BotCore.sync_app_commands` em `empiremuse/utils/client.py`);
- sem `MONGO`, a pasta `local_database/` é criada no diretório de dados (`empiremuse/utils/db.py`);
- com `RUN_RPC_SERVER=true`, a página de status responde em `http://localhost:<PORT>/`.

Pare com `Ctrl+C`. O `jishaku` só é carregado com `ENABLE_JISHAKU=true` (`load_jishaku_if_enabled` em `empiremuse/utils/client.py`, desligado por padrão) e serve para depuração interativa por donos. Ao carregá-lo, o bot define `JISHAKU_MASK_SECRETS=true`, `JISHAKU_DM_ONLY=true` e `JISHAKU_OWNER_IDS` com os IDs de `OWNER_IDS` (o dono do aplicativo precisa estar na lista). Com `OWNER_IDS` vazio o jishaku não é carregado (aviso no log). O operador pode sobrescrever `JISHAKU_MASK_SECRETS` e `JISHAKU_DM_ONLY` pelo ambiente. Os tracebacks voltam por DM.

### 4.1 Instalar o `uv`

Instale o `uv` pelo gerenciador de pacotes do sistema, pelo `pipx` (`pipx install uv`) ou baixando o binário da [página de releases do uv](https://github.com/astral-sh/uv/releases) e conferindo o checksum publicado ao lado dele. Evite os instaladores que baixam e executam um script de uma vez só (por pipe): eles rodam código remoto sem você conferir.

### 4.2 Rodar sem container (não recomendado)

O modo recomendado é o Docker ([`deploy/README.md`](../deploy/README.md)). Para quem não pode ou não quer usar container, os scripts de [`deploy/local/`](../deploy/local/) fazem o mesmo caminho da seção 2 num só passo:

```bash
sh deploy/local/run.sh        # Linux e macOS
deploy\local\run.bat         # Windows
```

O script confere que não está rodando como root (no Windows, como Administrador) e que o `uv` existe, instala o Python 3.14 pelo `uv`, cria o `.venv`, instala as dependências do `requirements-lock.txt` com verificação de hash e inicia o bot pela raiz do repositório. Ele não instala o `uv`, não baixa nada além do Python 3.14 (instalado pelo próprio `uv`), não pede privilégio elevado e nunca lê, mostra nem copia o `.runtime/.env`; só avisa se o arquivo não existir ou se grupo e outros puderem lê-lo (`chmod 600 .runtime/.env`). Se já existir `.env`, `config.json` ou `local_database/` na raiz, o script não cria `.runtime/` e segue usando a raiz, para não trocar os dados de uma instalação antiga. Um `.venv` com Python diferente de 3.14 faz o script parar pedindo para apagá-lo (ele nunca apaga sozinho). O Java e o Lavalink são baixados pelo próprio bot, com SHA-256 fixo (seção 5).

Com banco MongoDB, suba só o banco com `docker compose -f deploy/docker-compose.deps.yml up -d` ([`docker.md`](guides/docker.md), seção 3.2) e use o host `127.0.0.1` no `MONGO`. Sem `MONGO`, o bot usa o banco local. O `update` usa o modo `git` quando o código vem de um clone comum (a pasta `.git` é um diretório; numa git worktree ela é um arquivo e o modo cai em `manual`); fora disso, o bot só informa a versão nova.

## 5. Lavalink (servidor de áudio)

O bot **não toca áudio sozinho**: precisa de pelo menos um node Lavalink (v3 ou v4) ou NodeLink. As fontes
de nodes são somadas (`BotPool.setup` em `empiremuse/utils/client.py`):

| Opção | Como usar |
|---|---|
| **`lavalink.ini`** | Copie [`deploy/lavalink.ini.example`](../deploy/lavalink.ini.example) para `lavalink.ini` na pasta de execução (`.runtime/lavalink.ini` ou raiz, ignorado pelo Git). Cada seção é um node (`host`, `port`, `password`, `secure`, `region`, `search_providers`, …). Use `%%` para escrever `%` em valores. Só é lido quando `AUTO_DOWNLOAD_LAVALINK_SERVERLIST` é falso (`BotPool.setup` em `empiremuse/utils/client.py`). |
| **`LAVALINK_NODE_<N>`** | Variável (ou chave do `.env`) com um JSON por node, p.ex. `LAVALINK_NODE_1='{"identifier": "meu", "host": "...", "port": 443, "password": "...", "secure": true}'`. O JSON é usado como está, sem valores padrão: informe `identifier`, `host`, `port` e `password` (`BotPool.setup` e `BotPool.check_node` em `empiremuse/utils/client.py`). |
| **Lavalink local** | `RUN_LOCAL_LAVALINK=true`, ou nenhum node configurado (`BotPool.setup` em `empiremuse/utils/client.py`). Baixa `Lavalink.jar`, `application.yml` e plugins com verificação rigorosa de integridade via SHA-256 no download. Por padrão, `LAVALINK_FILE_URL` fica vazio no `.env`, o que seleciona automaticamente o espelho oficial correspondente à versão do código ([EmpireCommunity/EmpireMuse-LL-binaries](https://github.com/EmpireCommunity/EmpireMuse-LL-binaries) release `0.0.5` com Lavalink oficial 4.2.2, plugins espelhados e o plugin Soundy (SoundCloud); constantes em `empiremuse/utils/branding.py`; o `application.yml` traz o cliente TV, OAuth desligado por padrão e log de `YoutubeOauth2Handler` em WARN). Se `LAVALINK_FILE_URL` contiver uma URL oficial de versão anterior do espelho, o bot atualiza sozinho para a versão atual com aviso no log. Para usar um jar próprio ou hospedagem customizada, o endereço exige obrigatoriamente HTTPS (`https://`) e a definição de `LAVALINK_FILE_SHA256`. Preserva `application.yml` personalizado existente (substituindo apenas versões oficiais conhecidas como 0.0.1 e 0.0.2). Baixa o JDK se não houver Java 17+. Requer rede e a RAM definida em `LAVALINK_RAM_LIMIT` (MB). O node se chama `LOCAL` e escuta em `127.0.0.1:${SERVER_PORT:-8090}`. |
| **Lista pública** | `AUTO_DOWNLOAD_LAVALINK_SERVERLIST=true` baixa a lista de nodes públicos (`get_lavalink_servers`, com `LAVALINK_SERVER_LIST` como alternativa; o padrão é `data/lavalink_servers.ini` do repositório, `LAVALINK_SERVER_LIST_URL` em `empiremuse/utils/branding.py`). Nodes públicos são instáveis e não devem ser usados com dados sensíveis. |

A lista baixada é validada (status 200 e formato INI com `host`, `port` e `password` em cada seção; `empiremuse/utils/music/lavalink_server_list.py`). Se o download falhar ou a resposta for inválida, o bot usa o arquivo local `data/lavalink_servers.ini`.

Dica: em máquinas de desenvolvimento, `RUN_LOCAL_LAVALINK=true` é o caminho mais previsível; os arquivos
baixados (`Lavalink.jar`, `application.yml`, `plugins/`, `.tempjar/`, `.java/`) ficam no diretório de dados em execução (`RUNTIME_DIR`) e não devem
ser commitados.

## 6. Testes e ferramentas

Os testes automatizados ficam em `tests/` e cobrem identidade de marca (`tests/brand/test_branding.py`, `tests/brand/test_identidade_visual.py`), validação de listas de servidores Lavalink, filtros de URLs, a suíte completa do painel web (`tests/panel/`) e caracterização de canais de pedidos (`tests/cogs/test_canal_de_pedidos_*.py`). O `pyproject.toml` define a configuração do pytest, da medição de cobertura (`pytest-cov`) e do linter/formatador `ruff`.

As subpastas são organizadas por domínio: `audio/` (Lavalink, JDK, YouTube, Spotify), `brand/`, `cogs/`, `config/`, `deploy/`, `panel/`, `persistence/` (sessões, fila salva, skins, caches, db.py), `pool/`, `repo/` (layout da raiz e ignores), `rules/`, `runtime/` (paths e .runtime/), `serialization/`, `web/` (RPC/WebSocket) e `support/` (helpers compartilhados e raiz.py). O arquivo vai para a pasta do módulo de produção que ele testa e o nome de arquivo não leva fase nem etapa.

Política: lint, smoke e a suíte completa rodam no CI e não se repetem localmente; "pronto" é CI verde. Rode localmente só o que o CI/CD e o bot ECC Tools não cobrem. Os comandos abaixo servem para diagnóstico pontual.

```bash
uv pip install --require-hashes -r requirements-dev-lock.txt # pytest, pytest-asyncio, pytest-cov, ruff e mypy
.venv/bin/python -m pytest               # executa a suíte de testes (Windows: .venv\Scripts\python -m pytest)
.venv/bin/python -m pytest --cov         # executa testes medindo a cobertura de código
.venv/bin/ruff check .                   # verificação estática de linting
.venv/bin/ruff format --check .          # verificação de conformidade de formatação
.venv/bin/ruff format .                  # formatação automática de arquivos
.venv/bin/mypy                           # verificação estática de tipos
```

### Status das ferramentas no projeto:

| Ferramenta | Status | Uso |
|---|---|---|
| `ruff` | **Ativo** | Linter e formatador configurado em `pyproject.toml` (target Python 3.14, line-length 120, regras `E4`, `E7`, `E9`, `F`). |
| `pytest` + `pytest-asyncio` + `pytest-cov` | **Ativo** | Testes por risco (ver `.claude/rules/empiremuse/testing.md`); piso de cobertura no `pyproject.toml` (`fail_under`, nunca cai) e meta de 70%. |
| `compileall` | **Ativo** | Validação sintática e compilação de bytecode (`python -m compileall -q main.py empiremuse`). |
| `mypy` | **Ativo** | Checagem estática de tipos configurada em `pyproject.toml` (target Python 3.14, baseline gradual com checagem ativa em `empiremuse.rules.*`). |
| `uv` | **Ativo** | Gerenciamento de dependências e arquivos de lock universais com integridade estrita via hash (`requirements-lock.txt` e `requirements-dev-lock.txt`). |

### Regeneração e sincronia dos lockfiles

Caso dependências sejam alteradas em `requirements.txt` ou `requirements-dev.txt`, os lockfiles devem ser regerados a partir da raiz com resolução universal e corte temporal de segurança utilizando `uv==0.13.5`:

```bash
# Fixa o uv na versão da CI (0.13.5, com hashes)
python -m pip install --require-hashes -r .github/ci-uv-requirements.txt
uv --version  # deve imprimir uv 0.13.5

# Lock universal de produção
uv pip compile --generate-hashes --universal --exclude-newer 2026-10-20T00:00:00Z --exclude-newer-package yt-dlp=2026-10-26T22:30:00Z --exclude-newer-package certifi=2026-10-23T12:00:00Z --exclude-newer-package brotli=2026-10-22T15:00:00Z --exclude-newer-package brotlicffi=2026-10-22T17:00:00Z requirements.txt -o requirements-lock.txt

# Lock universal de desenvolvimento
uv pip compile --generate-hashes --universal --exclude-newer 2026-10-20T00:00:00Z requirements-dev.txt -o requirements-dev-lock.txt
```

#### Justificativa das isenções pontuais de corte temporal

A regra padrão de corte temporal (`--exclude-newer 2026-10-20T00:00:00Z`, o mesmo nos dois locks) previne ataques de supply chain exigindo que pacotes tenham pelo menos 7 dias de maturação pública no PyPI. Quatro exceções temporárias, só no lock de produção e com datas fixas no passado, foram homologadas:
- **`yt-dlp` (2026.10.26, corte `2026-10-26T22:30:00Z`)**: exceção temporária necessária para acompanhar atualizações contínuas de cifragem e contorno anti-bot do YouTube;
- **`certifi` (2026.10.23, corte `2026-10-23T12:00:00Z`)**: versão atualizada exigida como requisito estrito de dependência do `yt-dlp` (remove as raízes emSign da CA eMudhra);
- **`brotli` (1.2.1, corte `2026-10-22T15:00:00Z`)**: versões anteriores continham vulnerabilidades conhecidas em rotinas de descompressão (PYSEC-2026-4322 e PYSEC-2026-4323); a versão 1.2.1 corrige falhas de segurança prévias;
- **`brotlicffi` (1.2.1.0, corte `2026-10-22T17:00:00Z`)**: bindings CFFI correspondentes à biblioteca brotli atualizada;
- **Timestamps fixos no passado**: cada isenção é fixada exatamente no instante passado do release travado, evitando o risco de que datas futuras quebrem a sincronia da CI caso novos releases saiam antes da virada do corte;
- **Diretriz para alertas e releases**: alerta do Dependabot → regerar o lock com isenção pontual (`--exclude-newer-package`) do pacote corrigido; a cada novo release do bot → mover o `--exclude-newer` geral para ~7 dias antes da data de release;
- **Versão do `uv`**: os lockfiles são regerados compulsoriamente com `uv==0.13.5` (versão fixada na esteira de CI), garantindo estabilidade e determinação dos hashes em qualquer plataforma.

A esteira de integração contínua (CI) em `.github/workflows/ci.yml` executa automaticamente em todos os pushes e PRs para a branch `main`:
1. `Verificar integridade e sincronia dos lockfiles` (regera os locks e valida com `git diff --exit-code`);
2. `ruff check .` (linting);
3. `ruff format --check .` (formatação);
4. `python -m compileall -q main.py empiremuse` (validação de compilação de bytecode);
5. `mypy` (verificação estática de tipos, não-bloqueante via `continue-on-error`);
6. `pytest -q --cov --cov-report=term-missing:skip-covered` (execução da suíte de testes com validação de cobertura mínima configurada no `pyproject.toml`).

Convenções de código adicionais: `.claude/rules/empiremuse/python-disnake.md`.

## 7. Regenerar o pacote ECC dos agentes Antigravity

O pacote ECC (regras, workflows, skills e agentes) usado pelos agentes Antigravity fica em `./.agents/`
e é gerado pelo instalador do plugin ECC. Para atualizá-lo, a partir da **raiz do repositório**:

```bash
ls ~/.claude/plugins/cache/ecc/ecc/                  # descubra a versão instalada
node ~/.claude/plugins/cache/ecc/ecc/<versão>/scripts/install-apply.js --target antigravity python
```

- Use `--dry-run` antes para ver o plano sem copiar arquivos.
- O argumento final (`python`) escolhe a linguagem; o alvo `antigravity` grava em `./.agents/`
  (`node …/install-apply.js --help` lista os alvos).
- Regras do Claude Code ficam em `.claude/rules/ecc/` (alvo `claude-project`). Revise o diff antes de
  commitar e não misture a atualização do ECC com mudanças de código.

## 8. Workspace multipasta e repositórios irmãos

Os ecossistemas satélites do bot **não são submódulos**: são clones independentes, **irmãos** do repositório principal, na raiz do workspace:

```text
<raiz-do-workspace>/
├── EmpireMuse/              # este repositório
├── EmpireMuse-LL-binaries/  # binários e plugins do Lavalink
├── EmpireMuse-RPC-app/      # aplicativo de presença rica (RPC) para desktop
└── EmpireMuse-jishaku/      # fork do jishaku para o disnake
```

- **Clonar os irmãos** (a partir da raiz do workspace, ao lado de `EmpireMuse/`). Para abrir o `EmpireMuse.code-workspace` os três precisam estar clonados; sem o workspace, clone só os que for usar:
  ```bash
  git clone https://github.com/EmpireCommunity/EmpireMuse-LL-binaries.git
  git clone https://github.com/EmpireCommunity/EmpireMuse-RPC-app.git
  git clone https://github.com/EmpireCommunity/EmpireMuse-jishaku.git
  ```
  A CI não baixa os irmãos e os testes que dependem deles são pulados automaticamente (hoje, só os que leem o `rpc_client.py` do app desktop).
- `EmpireMuse.code-workspace`: arquivo na raiz do repositório que mapeia `.` (o EmpireMuse) e `../EmpireMuse-LL-binaries`, `../EmpireMuse-RPC-app` e `../EmpireMuse-jishaku`.
- **Isolamento e ignorados**: `worktrees/`, `.arquivo/` e `.local/` são ignorados no `.gitignore`, excluídos de builds no `.dockerignore` e omitidos das métricas de cobertura no `pyproject.toml`.
- **Desempenho da IDE**: o workspace configura exclusões de indexação e busca (`search.exclude`) para pastas pesadas ou locais como `.venv`, `.java`, `local_database`, `worktrees`, `.arquivo` e `.local`.

## 9. Padrão de Git Worktrees e Isolamento de Branches

Para permitir o trabalho paralelo e simultâneo de múltiplos agentes autônomos e desenvolvedores sem colisão de estado ou sujeira no ambiente, o projeto adota compulsoriamente o padrão de **Git Worktrees**:

### Diretrizes de Isolamento
- **Árvore principal intocável**: A árvore de trabalho principal (a pasta do clone principal) permanece **permanentemente na branch `main`**. Desenvolvedores e agentes **NUNCA** devem trocar de branch na raiz da árvore principal (`git checkout <branch>` ou `git switch <branch>`).
- **Diretório `worktrees/`**: Todas as branches de novas funcionalidades (`feat/*`), correções (`fix/*`), refatorações (`refactor/*`) ou tarefas paralelas devem ser desenvolvidas em worktrees dedicadas criadas dentro de `worktrees/` (exemplo: `worktrees/fix-nome-da-tarefa`). Esse diretório é estritamente ignorado no `.gitignore`, no `.dockerignore` e nas métricas de cobertura.

### Ciclo Operacional

1. **Criar a worktree a partir da `main` atualizada**:
   ```bash
   git fetch origin main
   git worktree add worktrees/<nome-da-branch> -b <nome-da-branch> origin/main
   ```

2. **Desenvolvimento e testes**:
   Navegue até a worktree correspondente (`cd worktrees/<nome-da-branch>`). Todo o desenvolvimento, execução de testes e commits acontecem exclusivamente dentro da pasta da worktree. O ambiente virtual `.venv` da raiz pode ser utilizado diretamente (`../../.venv/bin/python -m pytest`).

3. **Limpeza pós-merge**:
   Após o término da demanda, abertura do Pull Request e conclusão do merge na `main` pelo MAESTRO, remova a worktree e sincronize o repositório a partir da raiz:
   ```bash
   git worktree remove worktrees/<nome-da-branch>
   git branch -d <nome-da-branch>
   git worktree prune
   ```


