# Painel Web — serviços de configuração e conta (Etapas 4 e 5)

As regras e os efeitos das configurações editáveis pelo painel ficam em `panel/services/`, sem dependência do Tornado.
Os handlers (`panel/handlers/*.py`) só fazem HTTP; os helpers comuns (`_is_htmx`, `_reject_plain`, `_write_allowed`, `decode_args`) ficam em `AuthenticatedHandler` (`panel/handlers/servers.py`).

## Mapa

| Arquivo | Conteúdo |
| :--- | :--- |
| `panel/services/base.py` | `version_of`, `FormContext`, `Formulario`, classe base `SettingsForm`, `clean_title`, `load_json_object` |
| `panel/services/service.py` | `SettingsService`: leitura, gravação com `versao`, lock por escopo, auditoria |
| `panel/services/player.py` | Player (volume, autoplay, outros bots no canal, modo restrito) e DJ (cargos) |
| `panel/services/appearance.py` | Skins por bot e skins globais |
| `panel/services/general.py` | Prefixo e status automático do canal de voz |
| `panel/services/links.py` | Links fixos do servidor (adicionar, editar, remover, importar; exportar no handler) |
| `panel/services/listen_along.py` | Convites de "ouvir junto" (listar e remover) |
| `panel/services/jukebox.py` | Jukebox (E4c): validação síncrona, `JukeboxService`, jobs de setup/off e estruturas dos templates |
| `panel/services/channel_tokens.py` | `ResetTokens`: token HMAC do reset (2 min, uso único) |
| `panel/services/tasks.py` | `TaskRunner`: tarefas assíncronas em memória (timeout, limites, expiração, `shutdown`) |
| `panel/services/account*.py` | Conta do usuário (E5): `account_base`, `account` (serviço), `account_misc`, `account_links` — ver a seção "Conta do usuário (E5)" |

## Regras espelhadas dos comandos

As regras abaixo **espelham** o código dos comandos; os testes de caracterização (`tests/panel/test_settings_rules.py`)
detectam se um comando mudar e o painel ficar para trás.

- Volume: `range(5, 101, 5)` + `range(110, 151, 10)` (`modules/music_settings.py`, `PlayerSettings`).
- Prefixo: até 5 caracteres (`modules/legacy_cmds.py`, `setprefix`); o painel é mais estrito e recusa espaços e
  caracteres de controle. Vazio volta ao padrão e grava `""` no `guild_prefix_cache` (como `resetprefix`), com a chave
  inteira do servidor editado.
- Status de voz: ao menos 1 dos 9 placeholders de `SetStageTitle.placeholders`, até 496 caracteres, quebras de linha
  viram espaço (`utils/music/interactions/`).
- Skins: nomes de `bot.player_skins` / `bot.player_static_skins` menos `IGNORE_SKINS` / `IGNORE_STATIC_SKINS`, lidos de
  `bot.config` a cada requisição. Skins personalizadas do servidor não são lidas nem gravadas pelo painel.

## Dados e concorrência

- Acesso a dados somente por `bot.get_data/update_data` e `bot.get_global_data/update_global_data` (ids `int`,
  `db_name=DBModel.guilds`). O painel nunca altera `ver`.
- Semântica do cache do `BaseDB` (verificada em `utils/db.py`): `get_data` devolve o **mesmo dict** do cache e
  `update_data` coloca o dict recebido no cache. Por isso o serviço lê, faz `copy.deepcopy`, altera só os campos do
  formulário na cópia e grava a cópia; nunca muta o objeto do cache nem regrava o próprio.
- Controle otimista: cada formulário tem `versao` (16 primeiros hex do SHA-256 do JSON canônico dos campos dele).
  A escrita acontece sob um `asyncio.Lock` por escopo (`(id do bot, servidor)` ou `("global", servidor)`), relê o
  documento, compara a versão (diferente → 409) e só então grava e aplica os efeitos.
- Efeitos em memória (player, skin, status de voz, cache de prefixo) rodam depois da gravação, com falhas registradas
  apenas pelo tipo da exceção.
- Auditoria: `logger.info` com usuário, servidor, escopo, categoria e **nomes** dos campos alterados (nunca valores).

## Pendência para a fase de saneamento

Os cogs (`modules/music_settings.py`, `modules/legacy_cmds.py`, `utils/music/interactions/`) ainda têm as próprias
cópias dessas regras. **Refatorá-los para chamarem `panel/services/` fica para o saneamento**; nesta etapa os serviços
espelham os comandos e os testes de caracterização garantem a equivalência.

## Limite conhecido

Se a skin atual de um bot estiver em `IGNORE_*` (configuração alterada depois), ela não aparece na lista do formulário;
o navegador mostra a primeira opção e salvar sem reescolher troca a skin. Caso raro, aceito na v1.

## Comportamentos conhecidos (E4a/E4b)

- **Skin personalizada não recarrega pelo painel** (BAIXO-1): um valor `> custom_skin: X` é exibido e mantido se o
  formulário o reenviar idêntico, mas o painel não recarrega o player quando a skin de destino é personalizada; para
  aplicá-la ao player ativo use o Discord.
- **Salvar sem mudança não grava** (BAIXO-2): se os valores enviados são iguais aos gravados, o painel responde
  "Configurações salvas." sem escrever no banco, sem auditar e sem reaplicar efeitos (por exemplo, o cache de prefixo
  não é reposto).
- **Links fixos** (`player_controller.fav_links`): versão única da lista inteira do bot; adicionar usa os limites do modal
  do servidor (nome 2–25, URL 10–250 extraída com `URL_REG`, descrição vazia ou 3–100, máximo 25); importar usa os do
  `FavModalImport` (nome ≤ 25, URL ≤ `USER_FAV_MAX_URL_LENGTH`, descrição obrigatória ≤ 50) e valida todos os itens.
  Adicionar, editar e importar exigem a Jukebox configurada e visível ao bot; remover e exportar não. O efeito é
  `process_idle_embed`, como no comando. Reenviar a descrição já gravada não é troca (aceita mesmo abaixo do mínimo).
- **Ouvir junto** (`listen_along_invites`, global): listar e remover (adicionar fica fora da v1). Remover exige
  `manage_channels` (dono e administrador implicam), reavaliado na escrita: sem ela, mas ainda gerenciando o servidor,
  é 403. Os players ativos no canal de voz removido recebem `listen_along_invite=""`, `process_rpc()` e
  `process_save_queue()` depois da gravação.
- **Limite de corpo**: o `HTTPServer` da porta inteira recusa corpos acima de 64 KiB (`web_app.MAX_BODY_SIZE`), com ou
  sem painel; o painel ainda limita as escritas a 16 KiB (413).

## Jukebox (E4c)

- A lógica do `/jukebox setup` e do `/jukebox off` vive em `utils/music/jukebox_channel.py` e é compartilhada entre os
  comandos e o painel (`apply_jukebox_channel`, `reset_jukebox_channel`, `validate_jukebox_target`, `check_channel_perm`). O cog
  mantém só a interação (selects, botões, modais, timeouts) e passa `ask_forum_title`/`notify` como callbacks; o painel
  passa `ask_forum_title=None`.
- O painel valida **antes** de criar a tarefa (422 síncrono, sem efeito no Discord nem no banco) e executa o setup/off
  como tarefa assíncrona (`panel/services/tasks.py`): uma por servidor (409 se ocupado), estados
  pendente → executando → ok | erro, timeout de 60 s, expiração 15 min depois de terminar, limites de 5 por usuário e 500
  no total, `shutdown()` ligado ao encerramento do servidor web. O acompanhamento é por polling htmx (`every 2s`) até o
  estado final.
- O reset tem dois passos: confirmação com token assinado (usuário + servidor + bot + canal, 2 min, uso único) e, para
  apagar o canal, digitar o nome exato. Os jobs rodam sob o lock `(bot, servidor)` do `SettingsService` e entregam cópia
  profunda dos dados ao módulo; só `channel`/`message_id` mudam.
- Correções no módulo compartilhado: `check_channel_perm` usa os nomes canônicos (`view_channel`, `send_messages`) e o
  reset de fórum passa a limpar `channel`/`message_id` (vale para o comando e para o painel). A ordem banco × Discord do
  reset continua como no comando (banco primeiro) e fica no backlog de saneamento.

## Conta do usuário (E5)

`GET/POST /painel/conta` e `GET /painel/conta/favoritos/exportar` (só com sessão). Os dados são **sempre os do usuário da
sessão** (`bot.get_global_data/update_global_data(user_id, db_name=DBModel.users)`, `user_id` `int`); um `user_id` vindo do
formulário ou da URL é ignorado. O bot de entrada é o primeiro bot pronto do pool; sem bot pronto, GET e POST respondem
503 ("Painel indisponível no momento") sem ler nem gravar.

| Arquivo | Conteúdo |
| :--- | :--- |
| `panel/services/account_base.py` | `AccountContext`, `AccountSection` (contrato de cada seção), limite efetivo (0 = ilimitado; dono isento) |
| `panel/services/account.py` | `AccountService`: versão otimista, lock `("users", user_id)`, gravação por cópia profunda, auditoria |
| `panel/services/account_misc.py` | Prefixo pessoal, scrobble do Last.fm e token do app de RPC |
| `panel/services/account_links.py` | Favoritos (`fav_links`) e integrações (`integration_links`) |
| `panel/handlers/account.py` | `AccountHandler` (página e `POST` por `acao`) e `FavoritesExportHandler` |

- **Ações** (campo oculto `acao`): `prefixo`, `favorito_adicionar`, `favorito_editar`, `favorito_remover`,
  `favoritos_importar`, `integracao_remover`, `integracoes_importar`, `lastfm_scrobble`, `rpc_token_gerar`. Qualquer outra
  (inclusive adicionar integração por URL, vincular/desvincular Last.fm, importar/remover token) é 422.
- **Versão por seção** (16 hex do SHA-256 do JSON canônico): prefixo `{"custom_prefix"}`, favoritos `{"fav_links"}` (uma só
  para todos os formulários de favoritos), integrações `{"integration_links"}`, Last.fm `{"username","scrobble"}`, RPC
  `{"configurado"}`. Seções diferentes não conflitam entre si. Os segredos nunca entram na versão.
- **Prefixo**: 1–5 caracteres, sem espaço nem controle (mais estrito que o comando); vazio remove; efeito em
  `pool.user_prefix_cache[<id int>]`.
- **Favoritos**: adicionar segue o modal do comando (nome 2–25, URL 10–200, extraída com `URL_REG.findall`); importar usa
  `USER_FAV_MAX_NAME_LENGTH`/`USER_FAV_MAX_URL_LENGTH`, grava só o trecho que casa com `URL_REG.match` e valida todos os
  itens. `MAX_USER_FAVS` e `MAX_USER_INTEGRATIONS` são lidos de `bot.config` a cada requisição (0 = ilimitado; o dono do bot
  é isento; sobrescrever um nome existente não ocupa vaga). Editar nunca renomeia.
- **Integrações**: listar, remover e importar (texto-URL ou `{url, avatar}`; URL ≤ 150, só o trecho que casa; título ≤ 90).
  O painel nunca faz requisição externa; sempre grava objeto `{url[, avatar]}`.
- **Last.fm**: "vinculada" = `sessionkey` não vazia (o valor nunca é copiado para a tela, para a versão, para o log nem para
  a auditoria). Sem vínculo não há formulário e o POST é 422. O POST só altera `lastfm.scrobble`.
- **Token do app de RPC**: disponível quando `ENABLE_RPC_AUTH` e (`RPC_PUBLIC_URL` ou `RPC_SERVER`) e (`ENABLE_RPC_COMMAND`
  ou dono de algum bot pronto do pool); indisponível = sem formulário e POST 403. O token (50 caracteres `[A-Za-z0-9]`, gerado
  com `secrets`) aparece **só na resposta do POST que o gerou**; a página mostra apenas "Token configurado"/"Nenhum token".
  Trocar um token existente primeiro fecha a presença RPC do usuário nos players em que ele está na voz (falha de um
  player não impede o novo token).
- **Auditoria**: `logger.info("Conta alterada: usuario=… acao=… campos=…")` com **nomes** de campos (`custom_prefix`,
  `fav_links`, `integration_links`, `scrobble`, `token`); nunca valores, URLs, nomes de favoritos, token ou sessionkey.
- Limite conhecido: o modal "Adicionar" do comando não tem a exceção de `MAX_USER_FAVS=0`/dono (e o "adicionar integração"
  usa `MAX_USER_FAVS`), e o importador de integrações do comando quebra com o próprio export; o painel segue as regras
  coerentes (backlog de saneamento dos cogs).
