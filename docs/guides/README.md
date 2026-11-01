# Guias Técnicos do EmpireMuse

Documentação técnica e tutoriais de configuração para administradores e desenvolvedores do **EmpireMuse**.

---

## Índice de Guias

| Guia | Descrição |
| :--- | :--- |
| **[Lavalink Local e Atualização](atualizar-lavalink.md)** | Configuração do servidor de áudio local, verificação de integridade via SHA-256 (`Lavalink.jar`, `application.yml`, plugins e JDK portátil), cifras remotas e fontes de áudio. |
| **[YouTube OAuth](youtube-oauth.md)** | Configuração de autenticação OAuth com conta Google descartável no cliente TV para contornar bloqueios anti-bot do YouTube. |
| **[Painel Web Integrado](painel-web.md)** | Configuração do painel de administração web, credenciais OAuth2 no Discord Developer Portal, permissões e variáveis de ambiente. |
| **[Chaves de API do Spotify](spotify-keys.md)** | Obtenção de `SPOTIFY_CLIENT_ID` e `SPOTIFY_CLIENT_SECRET` no Spotify for Developers para resolução de metadados de faixas e playlists. |
| **[Implantação com Docker](docker.md)** | Implantação e execução com a imagem oficial multi-stage, variáveis operacionais, volumes persistentes e supervisão de processos. |
| **[Atualização Automática](auto-update.md)** | Comando `update`: modos `docker`, `panel`, `git` e `manual`, implantação do proxy no compose da Hostinger, códigos de saída do auxiliar e limitações. |
| **[Logs e Telemetria Local](logs.md)** | Níveis, cores, arquivo rotativo, `LOG_DEBUG_GUILDS`, resumo periódico de métricas, `COMMAND_LOG` só com IDs, redação de segredos e como investigar um `ERR-XXXXXX`. |
| **[Idiomas (i18n)](i18n.md)** | Catálogos em `empiremuse/locales/`, regras de chave e plural, o que o validador descarta, como ligar um idioma novo e a checagem `python -m empiremuse.i18n.check` do CI. |
| **[Emojis do Bot](emojis.md)** | Emojis de aplicação enviados à própria aplicação do bot, reserva em Unicode, troca de ícones por `emojis.json` e `APP_EMOJIS_GUILD_BOTS`. |
| **[Guia de Release e Publicação](release.md)** | Versionamento SemVer, publicação de tags no Git, esteira automatizada multi-arquitetura no Docker Hub com SBOM, provenance, assinatura cosign e GitHub Release com o digest. |
| **[Implantação no Pterodactyl](pterodactyl.md)** | Instalação via egg oficial no painel Pterodactyl, imagens `0.1` e `latest`, alocação de porta, persistência e recursos. |
| **[Implantação na Discloud](discloud.md)** | Hospedagem no plano pago da Discloud (1 GB / 1 vCPU), integração com GitHub, variáveis de ambiente, Lavalink e MongoDB. |

---

## Outras Documentações do Projeto

- **[Guia de Desenvolvimento Local](../development.md)**: Preparação do ambiente Python 3.14 com `uv`, execução do bot, suíte de testes e padrão de Git Worktrees.
- **[Arquitetura do Sistema](../architecture.md)**: Mapeamento aprofundado dos módulos, ciclo de vida do `BotPool`, fluxo de áudio com Lavalink v4 e persistência de dados.
- **[Guia de Contribuição](../../.github/CONTRIBUTING.md)**: Convenções de código, formatação com `ruff` e fluxo de trabalho com Pull Requests.
- **[Política de Segurança](../../.github/SECURITY.md)**: Práticas de proteção de credenciais e canal para relato privado de vulnerabilidades.
