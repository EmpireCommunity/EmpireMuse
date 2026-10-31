<p align="center">
  <img src="assets/brand/logo-256.png" alt="Logo do EmpireMuse" width="160">
</p>

<h1 align="center">EmpireMuse</h1>

<p align="center">
  <strong>Bot de música multi-instância e player interativo da comunidade EmpireHub</strong>
</p>

<p align="center">
  <a href="https://discord.gg/KgtyNg9U6H"><img src="https://img.shields.io/badge/Discord-EmpireHUB-8B5CF6?logo=discord&logoColor=white" alt="Discord EmpireHUB"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/Licen%C3%A7a-GPL--2.0-blue.svg" alt="Licença GPL-2.0"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white" alt="Python 3.14"></a>
  <a href="https://github.com/EmpireCommunity/EmpireMuse/actions/workflows/ci.yml"><img src="https://github.com/EmpireCommunity/EmpireMuse/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://hub.docker.com/r/or4cu1o/empiremuse"><img src="https://img.shields.io/docker/v/or4cu1o/empiremuse?sort=semver&logo=docker&logoColor=white&label=Docker" alt="Docker Hub"></a>
</p>

---

## Links

- 🤖 [Convidar o EmpireMuse 1](https://discord.com/oauth2/authorize?client_id=1149485958480285756&permissions=2533601240804560&scope=bot) para o seu servidor
- 💬 [Servidor do Discord](https://discord.gg/KgtyNg9U6H)
- 🌐 [Página do projeto](https://muse.empirecommunity.org) (status; painel web em `/painel`)

O repositório é público e mantido pela equipe para a comunidade EmpireHub, sem compromisso de suporte à hospedagem própria por terceiros.

---

## Sobre o Projeto

O **EmpireMuse** é o bot de música oficial da comunidade [EmpireHUB](https://discord.gg/KgtyNg9U6H). Desenvolvido em **Python 3.14** com o framework [disnake](https://github.com/DisnakeDev/disnake), o bot conta com processamento de áudio externo de alta fidelidade baseado no ecossistema [Lavalink v4](https://github.com/lavalink-devs/Lavalink) e NodeLink, arquitetura multi-instância em pool e interface visual rica para usuários e administradores no Discord.

> O **EmpireMuse** é uma obra derivada e evolução contínua do [MuseHeart-MusicBot](https://github.com/zRitsu/MuseHeart-MusicBot), criado por **zRitsu** e distribuído sob a licença **GPL-2.0** (consulte [LICENSE](LICENSE) e [CREDITS.md](CREDITS.md)).

Participe do nosso servidor oficial [Discord EmpireHUB](https://discord.gg/KgtyNg9U6H) para interagir com a comunidade, tirar dúvidas e acompanhar as atualizações do bot.

---

## Recursos Principais

- 🎵 **Player Interativo e Rico**: Controles multimídia completos via botões, menus de seleção e modais, com 15 skins visuais intercambiáveis (10 dinâmicas e 5 estáticas).
- 📻 **Arquitetura Multi-instância (`BotPool`)**: Gerenciamento integrado de múltiplos bots no mesmo processo para balanceamento de canais de voz simultâneos.
- 🌐 **Painel Web Integrado (`panel/`)**: Interface web moderna orientada a hipertexto (Tornado + htmx + Pico CSS) com autenticação Discord OAuth2 e PKCE para gerenciamento de configurações de servidores e preferências pessoais.
- 🎧 **Múltiplas Fontes de Áudio**: Suporte a YouTube (com cliente TV, cifra remota e autenticação OAuth opcional contra bloqueios anti-bot), Spotify, Deezer e servidores Lavalink locais ou remotos.
- 💬 **Canais e Fóruns de Pedidos**: Modos dedicados de song-request em canais de texto ou fóruns com sincronização automática de status de voz e palco.
- 📊 **Integrações e Presença**: Suporte a scrobble no [Last.fm](https://www.last.fm/) e integração via WebSocket com aplicativo desktop de [Rich Presence (RPC)](https://github.com/EmpireCommunity/EmpireMuse-RPC-app).
- 🛡️ **Segurança e Resiliência**: Isolamento estrito de variáveis de ambiente do subprocesso Lavalink por allowlist, download de binários verificado por SHA-256 e políticas de cookies seguros (`__Host-`).

---

## Previews da Interface

- **Player Controller**: Modo normal com mini-player e suporte a RPC (Rich Presence):

[![](assets/docs/readme_preview_player_default.png)](assets/docs/readme_preview_player_default.png)

<details>
<summary><strong>Ver mais capturas de tela e recursos visuais:</strong></summary>
<br>

- **Comandos de Barra / Slash Commands**:

[![](assets/docs/readme_preview_slashcommands.png)](assets/docs/readme_preview_slashcommands.png)

- **Integração com o Last.fm** para scrobbles em tempo real:

[![](assets/docs/readme_preview_lastfm.png)](assets/docs/readme_preview_lastfm.png)

- **Player Controller Estendido**: Modo de canal fixo de pedidos com conversa e fila interativa (`/setup`):

[![](assets/docs/readme_preview_player_setup.png)](assets/docs/readme_preview_player_setup.png)

- **Canal de Pedidos em Fórum**: Tópicos dedicados com atualização dinâmica de status no canal de voz e palco:

[![](assets/docs/readme_preview_controller_forum.png)](assets/docs/readme_preview_controller_forum.png)

> As imagens de preview foram preservadas temporariamente do projeto original e estão hospedadas em [`assets/docs/`](assets/docs/). A substituição gradual por artes exclusivas da identidade visual Nebulosa está planejada (veja [CREDITS.md](CREDITS.md)).

</details>

---

## Painel Web Integrado

O EmpireMuse inclui uma sub-aplicação web completa sob `/painel/` servida diretamente pelo servidor HTTP Tornado, dispensando dependências de runtime adicionais:

- **Gerenciamento de Servidores**: Ajuste de volume padrão, modo DJ, alternância de skins, links rápidos fixos, setup assistido de canais de pedidos e status do canal de voz;
- **Área Pessoal do Usuário** (`/painel/conta`): Configuração de prefixo customizado, gerenciador de playlists e favoritos com importação/exportação, vinculação Last.fm e emissão de tokens de presença rica;
- **Segurança de Nível Bancário**: Fluxo *Authorization Code Grant* com par criptográfico PKCE S256, revogação instantânea de access tokens, controle otimista de concorrência com validação SHA-256 e cookies de sessão isolados.

Consulte o [Guia do Painel Web](docs/guides/painel-web.md) para detalhes de rotas, permissões e setup de OAuth2 no Discord Developer Portal.

---

## Visão Geral de Arquitetura

O sistema é estruturado em camadas modulares para desacoplar regras de negócio, interface do Discord e processamento de áudio:

- **Ponto de Entrada (`empiremuse/__main__.py` / `main.py`)**: Ponto canônico `python -m empiremuse` e atalho direto `main.py` para inicialização do loop de eventos asyncio e instanciação do `BotPool`.
- **Pool de Bots (`empiremuse/utils/client.py`)**: Classe `BotPool` que supervisiona múltiplos bots `BotCore` (disnake), nós Lavalink e clientes de RPC.
- **Servidor Web e Relé (`empiremuse/web_app.py`)**: Servidor Tornado que expõe a página de status, relé WebSocket de RPC e o roteador da aplicação web `empiremuse/panel/`.
- **Motor de Áudio (`empiremuse/wavelink/` e `empiremuse/utils/music/`)**: Modelos de reprodução (`LavalinkPlayer`, `LavalinkTrack`), cliente de nós Lavalink e gerenciamento de subprocesso Java local (`local_lavalink.py`).
- **Persistência (`empiremuse/utils/db.py`)**: Suporte híbrido transparente para MongoDB (produção) e TinyMongo local (desenvolvimento).
- **Repositórios satélites**: [`EmpireMuse-LL-binaries`](https://github.com/EmpireCommunity/EmpireMuse-LL-binaries) `0.0.5` (Lavalink 4.2.2 e plugins, incluindo Lyrics 2.7.1), [`EmpireMuse-jishaku`](https://github.com/EmpireCommunity/EmpireMuse-jishaku) `v2.6.6.post3` (opcional, `ENABLE_JISHAKU`) e [`EmpireMuse-RPC-app`](https://github.com/EmpireCommunity/EmpireMuse-RPC-app) (Rich Presence).
- **Atualizador (`empiremuse/updater/`)**: comando `update` do dono (modos `docker`, `panel`, `git` e `manual`); no Docker troca a imagem por digest através de um proxy mínimo, sem dar o `docker.sock` ao bot. Guia: [`docs/guides/auto-update.md`](docs/guides/auto-update.md).
- **Cogs e Comandos (`empiremuse/cogs/`)**: Módulos de comandos de música, configurações de servidor, tratamento de erros, ajuda e integração com Last.fm.

Para uma documentação aprofundada com fluxogramas, ciclo de vida e mapeamento completo de arquivos, consulte [`docs/architecture.md`](docs/architecture.md).

---

## Desenvolvimento Local e Contribuição

Para configurar um ambiente local, executar testes e contribuir com o desenvolvimento do bot:

1. **Guia de Desenvolvimento**: Siga o passo a passo de setup com `uv`, variáveis do `.env`, execução do bot e Lavalink local em [`docs/development.md`](docs/development.md).
2. **Padrão de Git Worktrees**: O desenvolvimento de novas funcionalidades e correções opera obrigatoriamente através de worktrees isoladas em `worktrees/` a partir da branch `main`. Veja a seção 9 de [`docs/development.md`](docs/development.md).
3. **Diretrizes de Contribuição**: Padrões de código, formatação com `ruff`, convenções de commits convencionais e fluxo de pull requests estão documentados em [`.github/CONTRIBUTING.md`](.github/CONTRIBUTING.md).
4. **Política de Segurança**: Recomendações de segurança, reporte responsável de vulnerabilidades e escopo de proteção em [`.github/SECURITY.md`](.github/SECURITY.md).

---

## Suporte e Códigos de Erro

Em caso de falha inesperada durante a execução de comandos (slash ou prefixo) ou na navegação pelo painel web (erro 500), o EmpireMuse exibe ao usuário uma mensagem amigável acompanhada de um código de erro curto no formato `ERR-XXXXXX` (gerado aleatoriamente e sem expor detalhes internos):

- **Para o usuário**: Ao solicitar suporte ou abrir um chamado no servidor [Discord EmpireHUB](https://discord.gg/KgtyNg9U6H) ou no repositório, basta informar o código `ERR-XXXXXX` exibido na mensagem ou página de erro.
- **Para o administrador / dono da instância**: Para investigar a causa raiz:
  - **Arquivo de log local**: fora do Docker o arquivo `RUNTIME_DIR/.logs/disnake.log` vem ligado (`LOG_FILE=auto`; no Docker, use `LOG_FILE=on` e um volume). Ele é rotativo (5 MB × 3), não é recriado a cada inicialização e, por padrão, não grava nomes de usuários nem texto de mensagens (só IDs; `LOG_DEBUG_GUILDS` e `DEBUG` liberam texto livre redigido); os segredos conhecidos são mascarados por um redator, mas ele tem limites, descritos em [`docs/guides/logs.md`](docs/guides/logs.md) (ex.: `grep ERR-XXXXXX .logs/disnake.log`). Níveis, `LOG_DEBUG_GUILDS`, resumo periódico e `COMMAND_LOG` estão no [guia de logs](docs/guides/logs.md).
  - **Canal de webhook (recomendado para histórico)**: Para guardar o histórico persistente de erros entre reinicializações, recomenda-se configurar `AUTO_ERROR_REPORT_WEBHOOK` (envio automático de notificações com o código `ERR-XXXXXX`, traceback completo e dados de contexto) ou `ERROR_REPORT_WEBHOOK` (habilita o botão manual "Reporte esse erro" para envio pelo usuário).

---

## Créditos e Licença

O **EmpireMuse** é um software livre distribuído sob os termos da licença **GNU General Public License v2.0 (GPL-2.0)**. Em conformidade com a licença, todos os avisos de direitos autorais originais foram preservados.

- Autor original: **zRitsu** ([MuseHeart-MusicBot](https://github.com/zRitsu/MuseHeart-MusicBot))
- Licença completa: [LICENSE](LICENSE)
- Catálogo de atribuições e componentes de terceiros: [CREDITS.md](CREDITS.md)

Agradecimentos especiais aos projetos de código aberto que tornam o EmpireMuse possível:
- [DisnakeDev](https://github.com/DisnakeDev) pelo framework `disnake` (e [Rapptz](https://github.com/Rapptz) pelo `discord.py` original);
- [Lavalink-Devs](https://github.com/lavalink-devs) pelo servidor de áudio Lavalink e Lavaplayer;
- [Pythonista Guild](https://github.com/PythonistaGuild) pela biblioteca WaveLink;
- À comunidade e equipe do servidor **EmpireHUB** pelo apoio contínuo;
- Ícones de 21 dos emojis do bot (`assets/emojis/`, lista no [CREDITS.md](CREDITS.md)): <a href="https://www.flaticon.com/free-icons/user-interface" title="user interface icons">User interface icons created by mnauliady - Flaticon</a>.
