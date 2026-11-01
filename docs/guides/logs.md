# Logs e Telemetria Local

Guia do operador sobre o log do EmpireMuse: o que aparece no console e no arquivo, como ajustar o detalhe, o que nunca é gravado e como investigar um erro. A decisão de projeto está no [ADR-0005](../adr/0005-logs-stdlib.md); os módulos, em [`architecture.md`](../architecture.md).

A telemetria é **local**: console e arquivo, sem serviço externo de métricas ou de log. A única saída para fora do host é opcional e escolha sua: com `AUTO_ERROR_REPORT_WEBHOOK` (ou `ERROR_REPORT_WEBHOOK`, no botão manual) o relatório de um erro, com traceback, contexto do comando e dados do servidor, é enviado a um canal do Discord. Ele passa pelo mesmo redator (seção 4), mas use um canal privado.

---

## 1. Chaves de Configuração

Todas ficam no `.env` (modelo comentado em `.example.env`). `NO_COLOR` é lida só do ambiente.

| Chave | Valores | Padrão | O que faz |
| :--- | :--- | :--- | :--- |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` | `INFO` | Nível do console e do arquivo. Valor inválido vira `INFO`, com um aviso na subida. |
| `LOG_COLOR` | `on`, `off` | `on` | Cor por severidade **só no console** (nunca no arquivo). `LOG_COLOR=off` ou `NO_COLOR` não vazia desligam. |
| `LOG_FILE` | `auto`, `on`, `off` | `auto` | Arquivo de log. `auto` liga fora da imagem Docker e desliga dentro dela. |
| `LOG_DEBUG_GUILDS` | IDs separados por vírgula | vazio | Servidores que registram em `DEBUG` mesmo com `LOG_LEVEL=INFO`. No máximo 50 IDs. |
| `METRICS_INTERVAL` | segundos | `300` | Intervalo do resumo periódico. Mínimo 60 (de 1 a 59 vira 60); `0` desliga o resumo. |
| `COMMAND_LOG` | `true`, `false` | `false` | Registra os comandos de quem não é dono (seção 5). |
| `USAGE_RETENTION_DAYS` | dias, de 1 a 365 | `30` | Retenção do registro de uso e dos eventos de reprodução (seção 7). Inválido ou menor que 1 usa 30. |
| `ENABLE_LOGGER` | `true`, `false` | `false` | Sinônimo antigo de `LOG_FILE=on`; nunca desliga nada. |

### 1.1 Nível e `LOG_DEBUG_GUILDS`

Em `INFO`, o console só mostra **problemas e o resumo periódico**: início e fim de faixa, comandos e eventos de rotina ficam em `DEBUG`. Isso mantém o console legível com centenas de servidores tocando.

Para investigar **um** servidor, ponha o ID dele em `LOG_DEBUG_GUILDS` e reinicie o bot: só os registros do EmpireMuse nesse servidor sobem para `DEBUG`; o restante continua em `INFO`. O `LOG_LEVEL=DEBUG` global é temporário: com muitos servidores ele enche o arquivo rotativo em minutos.

As bibliotecas externas (disnake, aiohttp, pymongo, etc.) ficam sempre em `WARNING`.

### 1.2 Arquivo e rotação

O arquivo é `RUNTIME_DIR/.logs/disnake.log`, com permissão 0600 (pasta 0700). Ele gira a **5 MB com 3 cópias**, então ocupa no máximo cerca de 20 MB. O nível do arquivo é o `LOG_LEVEL`, o mesmo do console, e o conteúdo é o mesmo, só sem cor, sem emoji e com a data completa (dá para usar `grep`).

No Docker o arquivo vem desligado: o log sai pelo console e o driver `json-file` do compose já limita (10 MB × 3). Para ter o arquivo na imagem, use `LOG_FILE=on` e monte um volume em `RUNTIME_DIR`.

---

## 2. O que Aparece

Formato do console: `HH:MM:SS NÍVEL emoji [bot/servidor] logger: mensagem`. O `[bot/servidor]` só aparece quando há contexto (o servidor é sempre o ID).

- **Subida:** um relatório por etapas (pasta, Python, versão, cogs carregados e os que falharam, MongoDB, Java, nós do Lavalink e se há versão estável nova).
- **Problemas:** faixa travada, exceção de faixa, nó caído, reconexão de voz. Cada um leva servidor, canal, duração e origem; o **título da música só aparece em `DEBUG`**.
- **Nós de áudio:** conectado, caído e todos indisponíveis, uma linha por transição.
- **Resumo periódico** (a cada `METRICS_INTERVAL`): servidores, players ativos e pico, faixas iniciadas, erros por tipo, latência do gateway e da voz, CPU e RAM do processo Python e do Java local, e uma linha por nó do Lavalink.
- **Erros inesperados:** uma linha `ERROR` com traceback, o contexto do comando (seção 4) e o código `ERR-XXXXXX`.

### Erros repetidos

Avisos e erros iguais (mesmo logger, nível, modelo da mensagem, tipo de exceção e ponto de origem) saem **inteiros uma vez por janela de 60 s**. As repetições são contadas e viram uma linha `↻ … repetida N× em 60 s`. Erros com código `ERR-…` repetidos mantêm cada código no log (uma linha curta apontando o traceback igual), para o código que o usuário informou poder ser encontrado. Isso vale para **no máximo 50 ocorrências com código por chave e janela** (a primeira inteira e as demais em linha curta). Da 51ª em diante, a ocorrência não ganha linha nem código: só entra na contagem do resumo (`repetida N× …, códigos omitidos`), que não repete as 50 primeiras.

---

## 3. O que Nunca é Gravado

- **Segredos:** tokens do Discord, URI do MongoDB, senhas dos nós do Lavalink, refresh token do YouTube, segredos do Spotify e do Last.fm, tokens de webhook e cookies do painel. Um **redator único** troca esses valores (e os formatos conhecidos: credencial em URL, parâmetros `api_key`/`token`/`sk`, cabeçalho `Authorization`) por `***` antes de o registro chegar ao console, ao arquivo ou ao webhook de erro.
- **Conteúdo de usuários:** texto de mensagens, nomes de usuário, apelidos, nomes de servidor, URLs assinadas de anexos. Só IDs.
- **Caracteres de controle:** quebras de linha e sequências ANSI vindas de títulos, nomes ou mensagens de exceção são neutralizadas, então um texto de terceiros não forja uma linha de log nem mexe no terminal. Cada registro também tem tamanho máximo.

Valores de segredo com menos de 6 caracteres não são mascarados por valor (os formatos conhecidos continuam cobertos). Se um segredo vazar mesmo assim, troque-o.

---

## 4. Investigar um Erro (`ERR-XXXXXX`)

1. O usuário informa o código `ERR-XXXXXX` que o bot mostrou.
2. Procure o código: `grep ERR-XXXXXX .logs/disnake.log` (ou `docker logs <container> | grep ERR-XXXXXX`).
3. A linha traz o traceback e o contexto do comando: nome, `user_id`, `guild_id`, `channel_id` e os parâmetros **resumidos por tipo**. Modelos do Discord aparecem como `Tipo(id=…)`, anexos só como tipo e tamanho, e **texto livre só como tamanho** (`str(len=N)`). O valor, redigido e truncado, só aparece em `DEBUG` ou com o servidor em `LOG_DEBUG_GUILDS`. Parâmetro com nome de segredo sai como `***`.

Erros esperados do usuário (cooldown, permissão, argumento inválido) ficam em `DEBUG`.

### Webhook de erro

O `AUTO_ERROR_REPORT_WEBHOOK` guarda o histórico de erros fora do disco. O traceback enviado passa pelo mesmo saneamento e pelo mesmo redator do log: antes só o token do bot era mascarado, agora vale tudo o que está na seção 3.

Exceções não tratadas (do loop, de threads e do `sys.excepthook`) também saem no log de erros, com redação, desde o começo da subida.

---

## 5. `COMMAND_LOG`

Com `COMMAND_LOG=true`, cada comando de quem não é dono gera uma linha `INFO` (logger `empiremuse.commands`) no console **e** no arquivo. Ela contém só:

- `user_id` e `guild_id` (nunca nome de usuário, apelido nem nome do servidor);
- o nome qualificado do comando;
- as opções resumidas como na seção 4. Em comandos de prefixo, o resto da mensagem sai só como `str(len=N)`, **salvo nos servidores de `LOG_DEBUG_GUILDS`**: ali o texto livre (opções e resto da mensagem de prefixo, até 100 caracteres por parâmetro e 500 no conjunto, redigido e saneado) sai também em `INFO`, no console e no arquivo.

IDs são dado pessoal (LGPD). Mantenha o `COMMAND_LOG` desligado quando não precisar, e lembre que a retenção é a da rotação do arquivo (seção 1.2) e a do driver de log do Docker.

---

## 6. Menções

Por padrão o bot não marca `@everyone` nem cargos em nenhuma mensagem, mesmo que o texto (por exemplo, um nome de música ou de playlist) contenha esse trecho. Menções a usuários continuam funcionando quando o código as pede.

---

## 7. Registro de uso e banimento de servidores

O dono consulta **onde, como e quando** o bot foi usado, sem o log de arquivo e sem mensagem nova no Discord.

**O que é gravado** (coleção `usage_events` do banco global, no MongoDB ou no banco local): hora UTC, bot, servidor (ID e nome), canal de texto e de voz (ID e nome), tipo (`slash`, `prefix`, `button`, `jukebox`, `panel`, `playback`, `guild`), nome do comando ou botão, `ok` ou um rótulo fixo de erro (nunca o texto da exceção) e a latência. Na reprodução entram também título, autor, origem, duração, o comando que enfileirou, os segundos ouvidos e o motivo do fim (`finished`, `skipped`, `stopped`, `error`). **Nunca** entram ID, nome, avatar ou contagem de usuários, nem o texto digitado na busca.

**De onde vem:** comandos de barra, de menu e de prefixo abrem o evento no mesmo gancho do `COMMAND_LOG` (seção 5), mas o registro de uso fica sempre ligado e grava só o nome do comando e onde foi usado: nunca o usuário nem os valores das opções. O `COMMAND_LOG` continua opt-in e não muda.

**Como grava:** os eventos vão para um buffer em memória e são gravados em lote a cada 10 segundos, fora do caminho do comando. Banco fora do ar não atrasa nem quebra comando: sai uma única linha de aviso por queda e o erro `usage_write` aparece no resumo de métricas (o lote é descartado). Nome de servidor, canal e faixa é texto de terceiros: o ANSI e os controles saem na gravação, o tamanho é limitado e o markdown é escapado na exibição, sempre sem menções.

**Retenção:** `USAGE_RETENTION_DAYS` (30 por padrão). No MongoDB é um índice TTL (vale para os eventos gravados depois da mudança); no banco local, uma poda a cada hora.

**Painel do dono (`servers`):** "Ver uso" responde só a você (efêmero) com o resumo dos últimos 7 dias, os canais mais usados e os 15 últimos eventos. "Banir" pede o motivo (opcional) e uma confirmação, grava o banimento e faz **todos** os bots do pool saírem do servidor. "Banidos" lista os servidores banidos e desbane. Os botões só respondem a quem abriu o painel e continua sendo dono do bot.

**Lista de banidos:** uma só para todo o pool (coleção `banned_guilds` do banco global: ID, nome no momento do ban, data e motivo). Todo bot a consulta ao entrar em um servidor e ao ficar pronto; um ban ou desban feito no painel de qualquer bot vale para todos. Um servidor banido que readiciona o bot é abandonado na hora, sem boas-vindas, sem aviso no webhook de entrada/saída e com uma única linha de log.
