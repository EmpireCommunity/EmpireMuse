# Backend J3: interface para integração

T-473, contrato Jukebox rev. 3.2. Backend e telas integrados na mesma branch, com revisões finais de Alicerce, Sentinela e Crivo. Não publicar
J3 antes do inventário e da Política atualizados, e do pré-requisito E1 de exclusão.

## Pessoal: `pool.radio.user_favorites`

`UserRadioFavorites(pool, hub)` usa o diretório e a árvore atuais do hub.
O argumento `user` deve ser `int` da interação Discord ou da sessão autenticada;
outro tipo levanta `TypeError`. Nunca aceitar identidade do corpo, custom_id ou select.

| Operação assíncrona | Retorno |
| --- | --- |
| `stations(user)` | `list[FavoriteStation]`, tuplas nomeadas com `uuid`, `name` |
| `toggle_station(user, uuid)` | `Toggled.ADDED`, `.REMOVED`, `.FULL` (valores `added`, `removed`, `full`) |
| `remove_station(user, uuid)` / `clear_stations(user)` | `bool`: houve remoção |
| `filters(user)` | `list[str]`: ids dos nós; rótulos/caminhos pela `hub.locations` |
| `toggle_filter(user, node_id)` | os mesmos três resultados `Toggled` |
| `remove_filter(user, node_id)` / `clear_filters(user)` | `bool`: houve remoção |

Limites: `USER_LIVE_FAVORITES_MAX = 24`, `USER_FILTERS_MAX = 10`, em
`utils/radio/user_favorites.py`. `Toggled` é reusado de `custom_favorites.py`.
Estações guardam só uuid e nome limpo; o nome é relido por `directory.by_uuid_strict`.
A retirada de estação já salva não depende do diretório. Filtros guardam somente
`{node}`; desconhecidos somem da leitura, árvore desligada oculta a lista, mas
ambos continuam removíveis. Remover e limpar são idempotentes, inclusive ausentes.

Erros de `favorites.py`: `FavoriteNotFound` (sem payload) indica estação/local
inválido ou ausente no toggle; não há quarto resultado. `RadioUnavailable`, de
`premium.py`, indica banco, diretório indisponível ou tentativas CAS esgotadas.
A consulta estrita preserva `SEARCH_RESERVE`: falhas de rede e balde sem ficha levantam
`DirectoryError`, convertido em `RadioUnavailable` pelo store. `by_uuid` continua
tolerante para os chamadores existentes; novas gravações devem usar a porta estrita.
A UI deve oferecer refazer para não encontrado e tentar novamente para indisponível;
no painel, usar respectivamente 404 e 503, conforme contrato. Remoções não levantam
`FavoriteNotFound`. As recusas não escrevem nem tentam CAS.

## Servidor: `pool.radio.favorites`

| Operação assíncrona | Retorno |
| --- | --- |
| `list(guild_id)` | `list[FavoriteStation]`, até 24; leitura pública permitida |
| `add(guild_id, actor_id, station)` | `AddResult.ADDED`, `.ALREADY`, `.FULL` |
| `remove(guild_id, actor_id, uuid)` / `clear(guild_id, actor_id)` | `bool`: houve remoção |

`station` é a `Station` relida no servidor; nunca confiar em nome enviado pelo
componente. O store chama `perms.can_manage_guild(pool, guild_id, actor_id)`:
cache de membro, depois fetch com prazo; dono do servidor, administrador ou
Gerenciar Servidor. Falha/ausência nega. `FavoriteForbidden`, de `favorites.py`,
sem payload, mapeia para `radio.refused.no_manage_guild` (403 no painel).
A tela mantém sua checagem e o store reconfere o ator em toda operação de escrita.
`RadioUnavailable` continua sendo indisponibilidade do banco ou CAS esgotado.

## Superfícies integradas

`cogs/radio.py::_favorite` atende a estrela pessoal da 24 h e `_favorite_server`
atende o menu administrativo; ambos respondem pelo fluxo efêmero existente.
A resposta pessoal reutiliza `radio.panel.custom.favorite_{added,removed,full}`;
a do servidor usa `radio.panel.favorite_{added,already,full}`. Estação não encontrada
usa `radio.live.not_found`; indisponibilidade usa `radio.owner.unavailable`.
O visor público continua usando somente as favoritas do servidor.

Telas entregues pela UI (Vitral, T-474): `/fav_manager` modo "Rádios favoritas" (`fav_menu_radio.py`, Remover/Limpar de estações e filtros, sempre efêmero: num menu público abre uma mensagem efêmera nova), a gerência `/radio favoritas` (`fav_menu_server.py`, `open_server_favorites(inter, tr, pool)`, Remover e Limpar com confirmação), Minha conta › Rádios favoritas (só Remover) e Rádios › Favoritas do servidor no painel (Remover com confirmação, Adicionar pelo seletor de local, estados 200/403/404/409/422/503). `cogs/radio.py` registra `/radio favoritas`, aplica o defer efêmero e o cooldown do grupo e chama `open_server_favorites`; o helper reconfere a autorização real antes de ler a lista do servidor. O gerenciamento de filtros pessoais está no mesmo modo do `/fav_manager`.
Listas pessoais e filtros só em efêmero ou página da própria conta: nunca em
visão administrativa, voto, log ou mensagem pública. Painel exige sessão e XSRF;
ignore `user_id` enviado no corpo. Não adicionar Tocar nem estrela no player nesta
J3: pertencem à J2a/J2b. Não há porta nem stub de reprodução nesta unidade.

## Persistência e exclusão futura

Um documento por usuário, `_id` decimal em `global.radio_user`:
`{_id, stations: [{uuid, name}], filters: [{node}], rev}`. Todas as mutações usam
`save_doc_cas`, preservando a outra lista. Não há cache pessoal, cópia em outra
coleção nem migração das favoritas do servidor. O apagamento futuro pode remover
pelo `_id`, sem importar código J3. Esta unidade não implementa exclusão de conta.
