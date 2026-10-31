# Diretrizes de Governança e Operação de Agentes — EmpireMuse

> Este documento é a **fonte única da verdade** de instruções, responsabilidades e fluxos operacionais para todos os agentes autônomos (Claude Code, Antigravity/Gemini, GPT-OSS e Codex) que atuam no repositório **EmpireMuse**.

---

## 1. O Projeto

- **Nome**: **EmpireMuse** (organização GitHub: [EmpireCommunity/EmpireMuse](https://github.com/EmpireCommunity/EmpireMuse)).
- **Descrição**: Bot de música multi-instância para Discord em **Python 3.14**, utilizando o framework **disnake** e processamento de áudio 100% externo em servidores **Lavalink v3/v4** ou NodeLink.
- **Origem e Licença**: Obra derivada do [MuseHeart-MusicBot](https://github.com/zRitsu/MuseHeart-MusicBot), criado por **zRitsu**, sob licença **GPL-2.0**.
- **Aviso Mandatório**: É obrigatório preservar o arquivo `LICENSE` e todos os créditos e avisos de direitos autorais ao autor original em todos os arquivos pertinentes.
- **Documentação de Referência**:
  - [Arquitetura do Sistema](docs/architecture.md)
  - [Guia de Desenvolvimento Local](docs/development.md)
  - [Problemas Conhecidos (Backlog Técnico)](docs/known-issues.md)
  - [Política de Segurança](.github/SECURITY.md)
  - [Registros de Decisão de Arquitetura (ADRs)](docs/adr/README.md)

---

## 2. Mapa do Repositório

Estrutura essencial do código (consulte [`docs/architecture.md`](docs/architecture.md) para detalhes de linha):

- `empiremuse/__main__.py`: Ponto de entrada canônico do pacote (`python -m empiremuse`), instancia `BotPool` e inicializa o loop do processo.
- `main.py`: Atalho de execução na raiz que chama `empiremuse.__main__.main()`.
- `empiremuse/paths.py`: Ponto central de resolução de diretórios (`PROJECT_DIR` para raiz e arquivos estáticos, `PACKAGE_DIR` para pacote e templates, e `RUNTIME_DIR` para dados de execução configuráveis via `EMPIREMUSE_HOME`).
- `empiremuse/config_loader.py`: Carregamento, tipagem e precedência de configurações (`DEFAULT_CONFIG`, `.env`, `config.json`).
- `empiremuse/web_app.py`: Servidor HTTP Tornado para página de status e relé WebSocket do RPC (Rich Presence).
- `empiremuse/utils/client.py`: Classes centrais `BotPool` (gestão multi-bot, nós de áudio) e `BotCore` (bot disnake).
- `empiremuse/utils/db.py`: Camada de persistência (`MongoDatabase` via PyMongo Async e `LocalDatabase` via TinyMongo).
- `empiremuse/utils/others.py`: Decorador `pool_command`, classes de paginação, contexto e identificadores do controlador.
- `empiremuse/utils/owner_panel.py`: Interface administrativa do dono do bot (`PanelView`).
- `empiremuse/utils/music/models/`: Pacote dos modelos centrais de reprodução: `LavalinkPlayer` (`player.py` + mixins `player_*.py`), `LavalinkTrack`/`PartialTrack` (`tracks.py`) e auxiliares (`helpers.py`); o `__init__.py` reexporta os nomes públicos.
- `empiremuse/utils/music/interactions/`: Pacote dos componentes de UI do Discord (views, botões, modais e selects), com um módulo por tema (`player_views.py`, `fav_menu.py`, `fav_modals.py`, `skin_editor.py`, …); o `__init__.py` reexporta os nomes públicos.
- `empiremuse/utils/music/checks.py`: Verificações de permissão, voz, DJ e algoritmo de roteamento entre múltiplos bots.
- `empiremuse/utils/music/local_lavalink.py`: Download e execução sob demanda de binários JDK e Lavalink local.
- `empiremuse/utils/music/lavalink_server_list.py`: Validação da lista de servidores Lavalink baixada, com fallback para `data/lavalink_servers.ini`.
- `empiremuse/rules/`: Regras de negócio puras compartilhadas por comandos e painel (limites, texto, URLs, cota e configurações; só biblioteca padrão).
- `empiremuse/panel/`: Sub-aplicação web Tornado do painel de administração (OAuth2, sessões, rotas, serviços, templates e estáticos vendorizados).
- `empiremuse/utils/music/request_channel.py`: Lógica unificada de setup e reset de canais de texto e fóruns de pedidos, compartilhada entre os cogs e o painel web.
- `empiremuse/utils/music/audio_sources/`: Provedores de metadados para plataformas externas (Spotify e Deezer).
- `empiremuse/utils/music/skins/`: Coleção de 15 skins visuais para o player (10 no modo normal e 5 no modo estático).
- `empiremuse/wavelink/`: Cliente Lavalink v3/v4 e NodeLink (nós, conexões WebSocket, players e eventos).
- `empiremuse/cogs/music.py`: Cog principal `Music` (atributos de núcleo, `__init__`, `setup()` e a lista de mixins); os comandos e listeners (reprodução, filas, controles e nós) vivem em `empiremuse/music_cog/`.
- `empiremuse/music_cog/`: Mixins da Cog `Music` (um módulo por tema) e `shared.py`. Fica fora de `cogs/` de propósito: `BotCore.load_modules` carrega todo `.py` de `cogs/` como extensão. Ver `docs/architecture.md`, seção 3.
- `empiremuse/cogs/music_settings.py`: Cog de configurações de servidor/usuário, skins e favoritos.
- `empiremuse/cogs/player_resume.py`: Salvamento e restauração de sessões de reprodução ativas.
- `empiremuse/cogs/misc.py`: Comandos gerais de utilidade (ping, estatísticas, convite, sobre).
- `empiremuse/cogs/legacy_cmds.py`: Comandos administrativos e de suporte acionados via prefixo.
- `empiremuse/cogs/lastfm.py`: Cog de integração e scrobble na plataforma Last.fm.
- `empiremuse/cogs/error_handler.py`: Tratamento centralizado de exceções e envio de mensagens de erro formatadas.
- `empiremuse/cogs/help_cog.py`: Sistema interativo de ajuda.
- `empiremuse/cogs/ll_yt_oauth.py`: Gestão de tokens OAuth do YouTube para o plugin Lavalink.
- `empiremuse/cogs/server_manager.py`: Comandos administrativos para gerenciamento de servidores (listar, remover, ver uso, banir e desbanir; só o dono).
- `empiremuse/rules/playlists.py`, `empiremuse/utils/server_playlists.py`, `empiremuse/utils/server_playlists_files.py`, `empiremuse/utils/server_playlists_fetch.py` (importar por link: allowlist de hosts, IP público fixado, sem redirecionamento, 64 KiB, 10 s) e `empiremuse/cogs/server_playlists.py`: playlists do servidor (seção `playlists` do documento único, importação só-acrescenta, `/playlists`) e as regras puras delas; os arquivos importáveis ficam em `data/playlists/`.
- `empiremuse/rules/request_panel.py`, `empiremuse/utils/server_request.py` e `empiremuse/utils/request_panel.py`: canal de pedidos único (seção `request` do documento único, eleição do bot do painel sob a trava, painel multi-player com edição agrupada) e as regras puras dele; `resolve_player` e `pick_request_bot` ficam em `empiremuse/utils/music/checks.py`.
- `empiremuse/rules/bot_roles.py`: regras puras dos papéis principal/reserva (ordem dos convites, quem avisa num servidor sem o principal, textos fixos); o papel de cada bot (`is_main`, `is_reserve`, `pool.main_bot`) fica em `empiremuse/utils/client.py`.
- `empiremuse/utils/servers.py` e `empiremuse/rules/servers.py`: configuração única por servidor (documento `servers` do banco global, trava por servidor, migração do bot principal, fachada `get_data` no formato antigo) e as regras puras dela.
- `empiremuse/cogs/usage_tracker.py`, `empiremuse/utils/usage.py`, `empiremuse/utils/guild_ban.py` e `empiremuse/rules/usage.py`: registro de uso (buffer e gravação em lote), lista global de servidores banidos e as regras puras de ambos.
- `empiremuse/utils/branding.py`: Fonte única de identidade e URLs (nome, repositório, espelhos, upstream, `asset_url`).
- `assets/`: Imagens em subpastas por função (`brand/`, `providers/`, `player/`, `skins/`, `ui/`, `docs/`, `emojis/`) e `SHA256SUMS` com caminhos relativos. O nome do arquivo é único entre as subpastas, e `asset_url(nome)` resolve o subcaminho. Os arquivos soltos na raiz são cópias de compatibilidade da 2.1.0 e saem numa versão futura.
- `assets/brand/`: Arte e identidade visual de marca (logo vetorial, avatar, banner, avatares alternativos e favicon).
- `deploy/`: Todos os tipos de deploy, com o índice em `deploy/README.md` (qual arquivo usar em cada caso): composes (`docker-compose.yml` base, override do Traefik, `docker-compose.deps.yml` só com o MongoDB, `docker-compose.hostinger.yml` e `docker-compose.hostinger-sem-traefik.yml` + `env.example.hostinger` da VPS), `local/` (`run.sh` e `run.bat` para rodar sem container), egg do Pterodactyl e exemplo do `lavalink.ini`. O CI valida tudo isso no job `deploy-check`.
- `data/`: Dados estáticos do projeto (`lavalink_servers.ini`, lista de servidores Lavalink).
- `docs/`: Documentação técnica, guias de setup e Registros de Decisão de Arquitetura (ADRs).
- `docs/guides/`: Guias técnicos oficiais do projeto (Lavalink local, YouTube OAuth, painel web e Spotify).
- `docs/identidade-visual.md`: Diretrizes da identidade visual "Nebulosa", especificações de arte e guia de configuração no Discord.
- `docs/painel/`: Inventário dos serviços e configurações do painel web (`servicos-configuracoes.md`).
- `tests/`: Testes automatizados (pytest), organizados por domínio em subpastas (ver docs/development.md, seção 6).
- Repositórios satélites (`EmpireMuse-RPC-app`, `EmpireMuse-LL-binaries`, `EmpireMuse-jishaku`): clones irmãos na raiz do workspace, ao lado desta pasta (não são submódulos); ver docs/development.md, seção 8.

---

## 3. Comandos do Ambiente

- **Setup do Ambiente Virtual e Instalação** (detalhes em [`docs/development.md`](docs/development.md)):
  ```bash
  uv venv --python 3.14
  source .venv/bin/activate  # Windows: .venv\Scripts\activate
  uv pip install --require-hashes -r requirements-lock.txt
  cp .example.env .env
  ```
- **Execução do Bot**:
  ```bash
  python -m empiremuse                   # ponto de entrada canônico (ou python main.py)
  ```
- **Lint e Testes Automatizados**:
  - Testes com `pytest` (config em `pyproject.toml`; dependências em `requirements-dev-lock.txt`):
    ```bash
    uv pip install --require-hashes -r requirements-dev-lock.txt
    .venv/bin/python -m pytest
    ```
  - Lint e formatação com `ruff` (configurado em `pyproject.toml`):
    ```bash
    .venv/bin/ruff check .                  # verificação estática de lint
    .venv/bin/ruff format --check .         # verificação de conformidade de formatação
    .venv/bin/ruff format .                 # formatação automática
    ```
  - Checagem estática de tipos com `mypy` (configurado em `pyproject.toml`; baseline gradual com checagem ativa em `empiremuse.rules.*`):
    ```bash
    .venv/bin/mypy                          # verificação estática de tipos
    ```
  - Para validação sintática estática básica:
    ```bash
    python -m compileall -q main.py empiremuse
    ```

---

## Mapa de risco

O arquivo de maior risco tocado define o nível da mudança (Baixo, Médio ou Alto).

| Nível | Áreas | Caminhos |
| :--- | :--- | :--- |
| **Alto** | Segredos e carregamento de config | `empiremuse/config_loader.py`, `.example.env`, `empiremuse/logs/redact.py` (redação de logs) |
| **Alto** | Autenticação e permissões | `empiremuse/panel/` (`oauth.py`, `sessions.py`, `authz.py`, cookies), `empiremuse/cogs/server_manager.py`, `empiremuse/utils/guild_ban.py`, `empiremuse/utils/servers.py` (permissões de DJ e modo restrito), `empiremuse/cogs/server_playlists.py` (quem gerencia as playlists), `empiremuse/utils/owner_usage.py`, `empiremuse/utils/owner_panel.py`, `empiremuse/cogs/legacy_cmds.py` (comandos do dono, shell e jishaku) |
| **Alto** | Atualizador e cadeia de suprimentos | `empiremuse/updater/`, `Dockerfile`, `deploy/`, `.github/workflows/`, `requirements*.txt` e locks com hashes, `assets/SHA256SUMS` |
| **Alto** | Dados pessoais | `empiremuse/cogs/usage_tracker.py`, `empiremuse/utils/usage.py`, `empiremuse/cogs/lastfm.py`, `empiremuse/cogs/ll_yt_oauth.py` (tokens) |
| **Alto** | Exclusão de dados | `purge_events` e `delete_data` em `empiremuse/utils/db.py` |
| **Médio** | Player e áudio | `empiremuse/utils/music/`, `empiremuse/music_cog/`, `empiremuse/wavelink/` |
| **Médio** | Comportamento do bot e do painel | `empiremuse/cogs/`, `empiremuse/rules/`, `empiremuse/panel/` (rotas e serviços fora de auth) |
| **Baixo** | Conteúdo e valores | `assets/` (exceto `SHA256SUMS`), textos e traduções, `docs/`, estilos, valores padrão de config, bumps de versão |

---

## 4. Protocolo Maestri

O desenvolvimento cooperativo de múltiplos agentes é coordenado através do canvas **Maestri**:

- **Papel do MAESTRO (orquestrador)**: Planeja sprints, decompõe demandas, delega tarefas, valida entregas e arbitra conflitos técnicos. **O MAESTRO não escreve código produtivo.**
- **Comunicação entre Agentes**:
  - Utilização de `maestri ask`, `maestri check` e notas compartilhadas do canvas.
  - **Toda tarefa delegada termina obrigatoriamente com o envio do relatório completo ao MAESTRO** via:
    ```bash
    maestri ask "MAESTRO" "$(cat <<'FIM'
    <relatório com arquivos criados/alterados, decisões e status>
    FIM
    )"
    ```
- **Roster de Agentes e Responsabilidades** (nomes exatos do canvas):

| Agente | Capacidade Principal | Responsabilidades Principais | Ferramenta · Modelo Atual (pode mudar) |
| :--- | :--- | :--- | :--- |
| **MAESTRO** | Orquestração e arbitragem | Planejamento, delegação, validação de etapas e aprovação final de commits. | Claude Code · Claude Opus 5.5 |
| **Backend & Data Specialist** | Raciocínio arquitetural e backend | Regras de negócio, cogs, modelo do player, integração Lavalink, banco de dados e desempenho. | Claude Code |
| **Frontend & UI/UX Specialist** | Design de UI e interação | Interface do Discord (embeds, botões, selects, modais, 15 skins) e página web/RPC (`empiremuse/web_app.py`). | Claude Code |
| **QA & Test Engineer** | Qualidade e testes | Testes por risco (RED antes só para bug e regra pura; ver `testing.md`); **nunca altera código de produção**. | Claude Code |
| **DevOps & Infrastructure Specialist** | Infraestrutura e dependências | Docker, CI, runtime, lint, dependências e lockfiles. | Antigravity · Claude Sonnet 5.5 |
| **Cybersecurity Specialist** | Auditoria profunda e raciocínio crítico | Auditoria de segurança, validação de dados de entrada, proteção de segredos e supply chain (modo plano/leitura). | Antigravity · Claude Opus 5.5 |
| **Second Opinion (Devil's Advocate) (fora do canvas atual)** | Pensamento divergente e revisão crítica | Revisão crítica de coerência, formulação de hipóteses divergentes e desempate de impasses (modo plano/leitura). | Antigravity · Gemini 3.8 Flash high |
| **Assistente Operacional** | Execução mecânica rápida | Tarefas mecânicas repetitivas, geração de esqueletos, mocks, formatações em lote e configs. **Nunca recebe arquivos críticos de segurança/infra.** | Antigravity · Gemini 3.8 Flash |
| **Git & Documentation Keeper** | Governança de documentação | Governança de documentação, `CHANGELOG.md`, ADRs, padronização de commits e branches. | Antigravity · Claude Sonnet 5.5 |
| **DeepSearch** | Pesquisa técnica (somente leitura) | Pesquisa técnica, levantamento de documentação externa e referências (somente leitura). | Antigravity |

- **Isolamento de Escopo e Padrão de Git Worktrees**:
  - Cada agente deve modificar **estritamente os arquivos atribuídos à sua tarefa**. Alterações em arquivos de outros agentes devem ser solicitadas no relatório.
  - **Árvore principal intocável**: Agentes autônomos e desenvolvedores **NUNCA** trocam de branch na árvore principal (a pasta do clone principal), que permanece permanentemente em `main`.
  - **Uso obrigatório de Worktrees**: Demandas que exijam branches de funcionalidades (`feat/*`), correções (`fix/*`), refatorações (`refactor/*`) ou trabalho paralelo devem operar exclusivamente em uma worktree dedicada sob `worktrees/` (criada a partir de `main`: `git worktree add worktrees/<nome-da-branch> -b <nome-da-branch> origin/main`). O diretório `worktrees/` é estritamente ignorado pelo git.
  - **Limpeza pós-merge**: Após a conclusão e merge do PR correspondente pelo MAESTRO, a worktree deve ser limpa a partir da raiz com `git worktree remove worktrees/<nome-da-branch>`, a branch local excluída com `git branch -d <nome-da-branch>` e as referências limpas com `git worktree prune`.
- **Mediação e Prevenção de Loops**: Caso qualquer agente tente a mesma solução mais de duas vezes sem sucesso ou entre em loop, sua execução é interrompida imediatamente e o impasse é encaminhado para avaliação do **Devil's Advocate**.

---

## 5. Fluxo de Trabalho com o ECC

A metodologia padrão do projeto utiliza o framework **Everything Claude Code (ECC)**:

1. **Planejamento**: `/ecc:plan` para decomposição de épicos e análise de impacto.
2. **Testes**: TDD (RED antes, conduzido pelo **QA & Test Engineer**) só para correção de bug e regra pura; no resto, teste depois e só se o item merecer (ver [`.claude/rules/empiremuse/testing.md`](.claude/rules/empiremuse/testing.md)). O QA nunca altera código de produção.
3. **Revisão de Código**: Auto-revisão e submissão para revisão de pares (`code-reviewer`).
4. **Auditoria de Segurança Proporcional (AppSec)**: Obrigatória em **toda demanda** (diretriz mandatória do dono do projeto), com profundidade proporcional:
   - **Auditoria Completa**: Aplicada obrigatoriamente quando a alteração tocar em:
     - Entrada de dados de usuário ou modais;
     - Comunicação de rede, endpoints HTTP ou WebSockets do Tornado (`empiremuse/web_app.py`);
     - Persistência e consultas a bancos de dados (`empiremuse/utils/db.py`) ou arquivos locais;
     - Manipulação de tokens, chaves de API, credenciais ou segredos;
     - Dependências do projeto (`requirements.txt`, arquivos de lock);
     - Scripts de instalação, atualização e deploy (`source_*`, Docker, YAMLs);
     - Downloads de binários ou scripts externos (Lavalink, JDK, plugins);
     - Comandos administrativos de owner (`empiremuse/cogs/legacy_cmds.py`, `empiremuse/utils/owner_panel.py`) ou execução de subprocessos.
   - **Checagem Rápida**: Verificação de ausência de segredos expostos e aderência estrita de escopo nas demais demandas (ex.: documentação pura, formatação ou textos estáticos).
5. **Documentação e Changelog**: Atualização dos documentos técnicos correlatos e inclusão das mudanças na seção `[Unreleased]` de [`CHANGELOG.md`](CHANGELOG.md).
6. **Padronização de Commits e Branches**:
   - Commits, pushes e merges são **terminantemente proibidos** sem a autorização prévia e explícita do MAESTRO.
   - Padrão **Conventional Commits** (`feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `perf`, `ci`).
   - Branches nomeadas no formato `tipo/descricao-curta` criadas a partir de `main` e isoladas em `worktrees/<nome-da-branch>`, preservando a árvore principal sempre em `main`.

---

## 6. Definição de Pronto (Definition of Done)

Uma funcionalidade, correção ou tarefa só é considerada **concluída** após o cumprimento cumulativo de quatro etapas:
1. ✅ **Implementação**: Código completo, funcional e aderente às convenções do projeto;
2. 🛡️ **Auditoria de Segurança**: Validado pelo AppSec em toda demanda (auditoria completa para superfícies sensíveis ou checagem rápida de segredos/escopo nas demais), sem novas vulnerabilidades;
3. 🧪 **Testes**: CI verde (lint, smoke e suíte rodam no CI e não se repetem localmente);
4. 📝 **Documentação & Changelog**: Documentação técnica atualizada e registro formal em `CHANGELOG.md`.

---

## 7. Regras e Precedência de Configurações

- **Claude Code**: Carrega automaticamente `.claude/rules/ecc/{common,python,java}` e `.claude/rules/empiremuse/*`.
- **Antigravity CLI**: Descobre e carrega `AGENTS.md` na raiz do repositório e utiliza o pacote local `.agents/` (não versionado; regenerável via script indicado em `docs/development.md`).
- **Ordem de Precedência**: As regras específicas do EmpireMuse ([`.claude/rules/empiremuse/*`](.claude/rules/empiremuse/) e este `AGENTS.md`) possuem **precedência absoluta** sobre as diretrizes genéricas do ECC caso haja divergência.
- **Repositórios Irmãos (`../EmpireMuse-*`)**:
  - Para agentes ou sessões abertas dentro de um repositório irmão (clone ao lado deste, na raiz do workspace), as regras e convenções do repositório filho prevalecem sobre as do repositório pai (`EmpireMuse`).
  - Commits, branches e PRs de alterações feitas nos ecossistemas satélites devem ser realizados **estritamente dentro do próprio repositório filho** correspondente.

---

## 8. Segurança e Confidencialidade

- **Segredos**: Nunca versione arquivos `.env`, `config.json`, bancos de dados locais (`local_database/`) ou credenciais reais. Variáveis novas devem ser adicionadas vazias e documentadas em `.example.env`.
- **Backlog Privado de Vulnerabilidades**: O catálogo detalhado de vulnerabilidades conhecidas é mantido **estritamente em ambiente privado** (nota do canvas do Maestri: *"Segurança — backlog privado"*). Detalhes técnicos exploráveis **nunca** devem ser inseridos em arquivos públicos do repositório, issues ou discussões públicas.
- **Relato Responsável**: Siga as diretrizes públicas estabelecidas em [`.github/SECURITY.md`](.github/SECURITY.md).

---

## 9. Convenções Globais

- **Idioma**: Toda documentação, mensagens de erro voltadas a usuários, botões, modais e relatórios operacionais entre agentes devem ser redigidos em **português do Brasil com acentuação correta**.
- **Identificadores**: Identificadores de código (funções, classes, variáveis), nomes de parâmetros internos e comandos do sistema devem manter sua forma em inglês ou padrão já estabelecido no código legado.
- **Disciplina Operacional**: Não altere dependências, arquivos ou código fora do escopo estrito da sua demanda.
