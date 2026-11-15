# Política de Segurança

O EmpireMuse é um bot de música para Discord, derivado do
[MuseHeart-MusicBot](https://github.com/zRitsu/MuseHeart-MusicBot), de zRitsu, e distribuído sob a
licença GPL-2.0. Este documento explica quais versões recebem correções, como relatar uma
vulnerabilidade de forma privada e quais cuidados quem hospeda o bot deve tomar.

## Versões suportadas

O projeto ainda não publica versões numeradas. Só a branch `main` recebe correções de segurança.

| Versão                                   | Suporte de segurança |
| ---------------------------------------- | -------------------- |
| `main` (último commit)                   | ✅ Sim               |
| Commits antigos, forks e outras branches | ❌ Não               |

Quando houver versões numeradas, esta tabela passará a listar as que recebem suporte.

## Como relatar uma vulnerabilidade

**Não abra issue, pull request ou discussão pública com detalhes de uma vulnerabilidade.**
O repositório é público e os detalhes ficariam visíveis para qualquer pessoa antes da correção.

Use o relato privado de vulnerabilidades do GitHub:

1. Acesse <https://github.com/EmpireCommunity/EmpireMuse>.
2. Abra a aba **Security**.
3. Clique em **Report a vulnerability** e preencha o formulário (ou acesse diretamente <https://github.com/EmpireCommunity/EmpireMuse/security/advisories/new>).

O relato fica visível apenas para você e para os mantenedores.

Caso você não consiga utilizar o formulário do GitHub por qualquer motivo, abra uma issue pelo modelo
**Solicitação de contato de segurança**
(<https://github.com/EmpireCommunity/EmpireMuse/issues/new?template=security_contact.yml>).
Esse modelo só pede um canal privado. **Não inclua nenhum detalhe técnico** da vulnerabilidade na
issue. Um mantenedor responderá com um canal privado para o relato.

### O que incluir no relato

- Descrição do problema e do impacto (o que alguém mal-intencionado conseguiria fazer).
- Componente afetado (arquivo, comando, módulo ou endpoint) e commit testado.
- Passos para reproduzir, de preferência em uma instância própria e isolada.
- Configuração relevante (variáveis de ambiente, banco usado, servidor de áudio), **sem incluir
  tokens, senhas ou outros segredos reais**.
- Sugestão de correção, se tiver.

## Prazos de resposta pretendidos

O EmpireMuse é mantido por voluntários, então os prazos abaixo são metas, não garantias:

| Etapa                                          | Prazo pretendido           |
| ---------------------------------------------- | -------------------------- |
| Confirmação de recebimento                     | até 3 dias úteis           |
| Triagem inicial e classificação de severidade  | até 7 dias corridos        |
| Correção de severidade crítica ou alta         | até 30 dias após a triagem |
| Correção de severidade média ou baixa          | até 90 dias após a triagem |

Durante o processo, os mantenedores vão:

- manter quem relatou informado sobre o andamento;
- combinar a data de divulgação pública, que por padrão acontece depois da correção na `main` ou
  em até 90 dias após o relato, o que vier primeiro;
- dar crédito a quem relatou no aviso de segurança, a menos que a pessoa prefira ficar anônima;
- avisar o projeto original ([zRitsu/MuseHeart-MusicBot](https://github.com/zRitsu/MuseHeart-MusicBot))
  de forma responsável quando o problema também existir lá.

## Escopo

**Dentro do escopo** (código mantido neste repositório):

- **Bot:** inicialização e configuração (`main.py`, `empiremuse/__main__.py`,
  `empiremuse/config_loader.py`), módulos (cogs) em `empiremuse/cogs/`, utilitários e player em
  `empiremuse/utils/`, cliente de áudio em `empiremuse/wavelink/`, painel web em
  `empiremuse/panel/` e camada de banco de dados (MongoDB ou banco local).
- **Servidor web e RPC:** `empiremuse/web_app.py`, ou seja, a página de status e o WebSocket de Rich Presence.
- **Instalação, conteinerização e deploy:** Dockerfile oficial, docker-compose, scripts de
  automação e a instalação automática do Lavalink local.

**Fora do escopo:**

- Servidores de áudio de terceiros, como [Lavalink](https://github.com/lavalink-devs/Lavalink),
  [NodeLink](https://github.com/PerformanC/NodeLink), seus plugins e nós públicos. Relate
  diretamente aos respectivos projetos.
- APIs e plataformas externas: Discord, Spotify, Last.fm, YouTube e demais serviços consultados
  pelo bot.
- O aplicativo de Rich Presence para desktop, que é um projeto separado.
- Vulnerabilidades em dependências de terceiros que não sejam exploráveis pelo uso que o
  EmpireMuse faz delas. Relate ao projeto da dependência. Se o EmpireMuse usar a dependência de um
  jeito que a torne explorável, o problema está no escopo.
- Ataques de negação de serviço por volume, engenharia social e ataques físicos.
- Problemas que exigem acesso prévio de dono do bot (`OWNER_IDS`) ou acesso ao servidor onde o
  bot roda, já que esses acessos concedem controle total por definição.
- Configurações que contrariam as recomendações da seção abaixo.

## Pesquisa de boa-fé

Pesquisas de segurança feitas de boa-fé e dentro destas regras são bem-vindas:

- teste apenas em instâncias suas, nunca em bots ou servidores de outras pessoas;
- não acesse, altere nem guarde dados de outros usuários;
- não degrade o serviço de terceiros;
- dê tempo razoável para a correção antes de divulgar.

## Boas práticas para quem hospeda o EmpireMuse

### Segredos

- Guarde tokens e credenciais (`TOKEN`, `MONGO`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`,
  `LASTFM_KEY`, `LASTFM_SECRET` e afins) **somente** no arquivo `.env` ou em variáveis de ambiente
  ou de segredos da plataforma de hospedagem. Use `.example.env` como modelo.
- Nunca versione `.env` nem um `config.json` com segredos, e nunca envie esses arquivos (nem
  capturas de tela do conteúdo deles) para ninguém.
- Se um token vazar, gere um novo imediatamente no
  [Discord Developer Portal](https://discord.com/developers/applications) e troque as demais
  credenciais expostas.
- Ative a autenticação em dois fatores na conta do Discord que é dona da aplicação.

### Donos do bot

- Quem está em `OWNER_IDS`, o dono da aplicação no Discord e os membros do time da aplicação têm
  acesso a comandos administrativos que controlam o processo e o servidor onde o bot roda.
  Conceda esse acesso só a pessoas de total confiança.

### Banco de dados (MongoDB)

- Use sempre autenticação, com um usuário exclusivo do bot e permissão apenas no banco que ele usa.
- Não exponha o MongoDB publicamente na internet. Mantenha-o em `localhost` ou em rede privada; em
  serviços gerenciados, restrinja os IPs de acesso.
- Use conexão com TLS quando o banco estiver fora da máquina do bot.
- Ao usar o banco local, proteja também a pasta `local_database/`, porque ela guarda dados do bot.

### Servidor RPC e página web

- Se não for usar Rich Presence ou painel web, desative o servidor com `RUN_RPC_SERVER=false`.
- Ao expor o servidor à rede ou à internet, ative a autenticação com `ENABLE_RPC_AUTH=true` e utilize um proxy reverso com TLS (`https`/`wss`), evitando publicar portas desnecessárias.
- Prefira o relé embutido no próprio bot (`RUN_RPC_SERVER=true`): ele confere o token no registro do app de Rich Presence.
- Quando o `RPC_SERVER` for remoto, configure o mesmo `RPC_SERVER_SECRET` nos dois lados e utilize conexão segura via `wss://`.
- Utilize `WEB_BIND_ADDRESS=127.0.0.1` quando não precisar de acesso externo, mantendo `0.0.0.0` em contêineres Docker e painéis como Pterodactyl.

### Servidor de áudio (Lavalink/NodeLink)

- Use uma senha forte e não deixe a porta do Lavalink aberta para a internet sem necessidade.
- Prefira nós próprios ou de origem confiável.

### Atualizações

- Mantenha a instância atualizada com a `main` deste repositório e acompanhe os avisos de
  segurança publicados na aba **Security**.
- Atualize o código e as dependências apenas a partir de fontes oficiais.

## Melhorias de segurança planejadas

O EmpireMuse está passando por um saneamento do código herdado. Entre as frentes de endurecimento
planejadas estão:

- autenticação e validação de entradas no servidor RPC/WebSocket;
- formatos seguros e validados para os dados persistidos;
- verificação de integridade dos componentes baixados automaticamente;
- dependências com versões fixas, verificação de hashes e auditoria automatizada;
- redução de privilégios dos comandos administrativos;
- ativação dos recursos de segurança do GitHub (alertas de dependências e varredura de segredos).

Os detalhes técnicos de cada item só serão publicados depois da correção, nos avisos de segurança
e no `CHANGELOG.md`.

## Créditos

Agradecemos a zRitsu e aos contribuidores do
[MuseHeart-MusicBot](https://github.com/zRitsu/MuseHeart-MusicBot), base deste projeto, e a todas
as pessoas que relatam vulnerabilidades de forma responsável.
