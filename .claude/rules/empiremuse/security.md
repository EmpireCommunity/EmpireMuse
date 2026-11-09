---
paths:
  - "**/*.py"
  - "**/*.sh"
  - "**/*.bat"
  - "**/*.yml"
  - "**/*.yaml"
  - ".example.env"
---
# Segurança — regras específicas do EmpireMuse

> Complementa [ecc/common/security.md](../ecc/common/security.md) e
> [ecc/python/security.md](../ecc/python/security.md). As regras gerais de lá (segredos fora do
> código, validação de entrada, consultas parametrizadas, `bandit`) continuam valendo e não são
> repetidas aqui. A política pública está em [`SECURITY.md`](../../../.github/SECURITY.md).

## 1. Segredos do projeto

- São segredos: `TOKEN`, `TOKEN_RESERVE_*`, `TOKEN_RADIO_*`, `MONGO`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`,
  `LASTFM_KEY`, `LASTFM_SECRET`, senhas de nós Lavalink, URLs de webhook (`*_WEBHOOK`) e
  tokens OAuth (por exemplo, o `refreshToken` do YouTube).
- Exceção única: `PANEL_OAUTH_CLIENT_ID` é público (vai em toda URL de autorização) e fica fora da
  máscara só com valor snowflake (17 a 20 dígitos ASCII); com qualquer outro formato continua segredo.
- Nunca coloque segredos em código, logs (`print`/`logging`), embeds, mensagens, anexos,
  relatórios de erro por webhook nem em mensagens de exceção. Para diagnóstico, registre só se o
  valor está presente (`configurado`/`ausente`). Se registrar URLs de conexão, mascare as
  credenciais (`mongodb://***@host`).
- Não envie segredos pelo Discord, nem por DM ou anexo. Funções que exportam configuração devem
  substituir os segredos por placeholders.
- Toda variável sensível nova entra em `.example.env` **vazia** e documentada, e é lida pelo
  `empiremuse/config_loader.py`. `.example.env` nunca recebe valores reais.
- `.env`, `config.json`, `local_database/` e caches locais não são versionados nem copiados para
  imagens Docker.

## 2. Persistência e desserialização

- **Proibido** usar `pickle`, `marshal`, `shelve`, `dill`, `jsonpickle` ou equivalentes em
  persistência nova (banco, arquivos, cache, sessões, skins, filas). Use JSON com validação
  explícita de tipo, campos permitidos, tamanho e limites antes de usar os dados.
- Não crie novos pontos de leitura de dados legados serializados em formato binário. A migração do
  legado para JSON é feita em tarefa própria, com leitura isolada e conversão única.
- Trate como entrada não confiável os dados vindos do MongoDB/TinyMongo, de arquivos locais, de
  nós Lavalink/NodeLink e de APIs externas, e não só a entrada do usuário do Discord.
- `ruamel.yaml`: use apenas o carregador padrão seguro (`YAML()` / `typ="rt"` ou `"safe"`), nunca
  `typ="unsafe"`.

## 3. Servidor RPC/WebSocket e HTTP (`empiremuse/web_app.py`)

- Toda mensagem recebida é decodificada dentro de `try` (`json.JSONDecodeError`/`ValueError`),
  precisa ser um `dict` e passa por validação de esquema: `op` em lista permitida, IDs como
  snowflakes inteiros, listas e strings com tamanho máximo. Mensagens inválidas são descartadas ou
  encerram a conexão com código de erro, sem derrubar o handler.
- Quem decide se a autenticação está ligada é a **configuração do servidor**, nunca um campo enviado
  pelo cliente. Conexões de bot e de usuário se autenticam com segredo verificado no servidor,
  comparado com `hmac.compare_digest`.
- `check_origin` usa allowlist configurável. Retornar `True` incondicionalmente é proibido.
- Defina `websocket_max_message_size`, limite de conexões e de IDs por conexão e limite de taxa.
- Repasse a outros clientes apenas campos de uma allowlist, nunca o payload recebido inteiro.
- O endereço de escuta é configurável. Exponha a porta só quando o RPC estiver habilitado e,
  em produção, atrás de proxy com TLS (`wss`).
- Respostas de erro ao cliente são genéricas. Detalhes ficam no log do servidor.

## 4. Entradas do Discord

- Conteúdo de modais e importações em JSON (skins, favoritos, configurações) tem tamanho máximo e
  esquema validados antes de ser salvo.
- Mensagens de erro para usuários não incluem `repr(e)`, tracebacks, caminhos de arquivo nem
  configuração interna.
- Texto controlado pelo usuário que o bot reenvia (nomes de playlist, skins, títulos) usa
  `disnake.AllowedMentions.none()` ou escape de menções, para evitar `@everyone`/`@here` injetados.
- O bot não faz requisições HTTP diretas para URLs fornecidas por usuários sem allowlist de esquema
  e domínio (risco de SSRF). Buscas de mídia passam pelo Lavalink/NodeLink.

## 5. Downloads de binários e scripts externos

- Lavalink.jar, JDK, plugins e qualquer outro binário baixado em tempo de execução ou de instalação
  usam **URL com versão fixa** (tag ou release imutável, nunca `master`, `main` ou `latest`) e
  **SHA-256 fixo** no código ou na configuração.
- Fluxo obrigatório: baixar para arquivo temporário → verificar SHA-256 → mover de forma atômica.
  Se o hash não bater, apague o arquivo e aborte com erro claro. Arquivo existente também é
  verificado antes do uso.
- Somente HTTPS, com `timeout` explícito em toda requisição.
- Proibido executar scripts baixados da internet (`curl | bash`, `bash install.sh` baixado na
  hora). Use o gerenciador de pacotes do sistema ou um artefato versionado e verificado.

## 6. Subprocessos, comandos de owner e `jishaku`

- Passe os argumentos como lista. Em código async, use `asyncio.create_subprocess_exec`.
  `subprocess.run` só é aceito fora do loop de eventos ou via `run_in_executor` (ver
  `python-disnake.md`, seção asyncio). `shell=True` e `create_subprocess_shell` só são aceitos no
  comando `shell` de owner, que existe para isso.
- Comandos de owner (`shell`, `eval`, `jishaku`, atualização via git, exportação) rodam com
  **ambiente mínimo**: uma allowlist explícita como `PATH`, `HOME`, `LANG` e `GIT_DIR`. Repassar
  `os.environ` inteiro é proibido.
- A saída desses comandos passa por mascaramento dos valores de segredo conhecidos antes de ir
  para o Discord e é enviada só por DM ou em resposta efêmera, nunca em canal público.
- `jishaku` e qualquer recurso de `eval` são opcionais e ficam **desligados por padrão**, ativados
  por configuração. Dependência de fork exige justificativa registrada em ADR.
- `OWNER_IDS` aceita apenas snowflakes inteiros. Toda execução de comando de owner é registrada em
  log (quem, qual comando e quando), sem segredos.
- Atualização automática de código busca apenas no repositório oficial do EmpireMuse, por tag ou
  commit verificado, e nunca descarta alterações locais em silêncio (`reset --hard`) sem
  confirmação explícita.

## 7. Reinício e encerramento do processo

- **Proibido `kill 1`** e o envio de sinais para PIDs que não sejam o do próprio processo.
- Para reiniciar, encerre de forma ordenada (`await bot.close()` e fechamento de sessões e banco),
  depois saia com código diferente de zero (`sys.exit(1)`) e deixe o supervisor (política de
  restart do Docker, systemd ou plataforma) subir o processo de novo.

## 8. Dependências (supply chain)

- Toda dependência tem **versão exata** (`==`) e **hash** no arquivo de lock (por exemplo,
  `uv pip compile --generate-hashes`), e a instalação usa `--require-hashes`.
- Prefira release publicada no PyPI. Dependência que não está no PyPI entra como wheel de release por
  **URL com `--hash`** no lock (nunca por nome nem VCS); VCS só em último caso, fixado por SHA completo
  de commit, nunca por branch. Hoje não há dependência VCS.
  *Problema que resolve:* dependência VCS não aceita `--require-hashes` (KI-30), então o build não era verificável por
  hash. *Custo:* uma etapa a mais para publicar o wheel do fork. *Revisão desta regra:* 2027-01-24.
- Antes de adicionar ou atualizar uma dependência: justifique no PR (uso real, manutenção ativa,
  licença compatível com GPL-2.0) e rode `pip-audit -r requirements.txt` sem vulnerabilidades
  altas ou críticas pendentes. Dependências sem uso são removidas.

## 9. Scripts (`.sh`/`.bat`), Docker e YAML de deploy

- Scripts bash novos começam com `set -euo pipefail`, colocam variáveis entre aspas e não imprimem
  o ambiente (`env`, `printenv`, `set`).
- Imagens Docker: imagem base com versão fixa (de preferência por digest), usuário não root e
  `.dockerignore` excluindo `.env`, `config.json`, `.git`, `local_database/` e caches.
- Arquivos YAML de automação e implantação (Docker Compose, GitHub Actions e similares) nunca contêm valores de
  segredo. Use variáveis de ambiente e arquivos `.env` locais (estritamente ignorados pelo Git).
- Workflows do GitHub Actions (quando existirem): actions fixadas por SHA, `permissions:` mínimas
  e nada de `pull_request_target` executando código do PR.
- Opções novas de segurança entram com o **valor seguro como padrão** em `empiremuse/config_loader.py` e
  `.example.env`.

## 10. Revisão obrigatória

Mudanças em `empiremuse/web_app.py`, `empiremuse/utils/db.py`, persistência, `empiremuse/cogs/legacy_cmds.py`,
`empiremuse/cogs/player_resume.py`, `empiremuse/utils/music/local_lavalink.py`, `empiremuse/config_loader.py`, scripts de
instalação/deploy e arquivos de dependências passam por revisão do agente AppSec (agente
`security-reviewer` do ECC) antes do merge. Vulnerabilidades ainda não corrigidas **não** são
descritas em issues, PRs, commits ou documentos públicos. Siga o fluxo de [`SECURITY.md`](../../../.github/SECURITY.md).
