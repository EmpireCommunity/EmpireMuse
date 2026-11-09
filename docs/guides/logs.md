# Logs e Telemetria Local

Guia do operador sobre o log do EmpireMuse: o que aparece no console e no arquivo, como ajustar o detalhe, o que nunca é gravado e como investigar um erro. A decisão de projeto está no [ADR-0005](../adr/0005-logs-stdlib.md); os módulos, em [`architecture.md`](../architecture.md).

A telemetria é **local**: console e arquivo, sem serviço externo de métricas ou de log. A única saída para fora do host é opcional e escolha sua: com `AUTO_ERROR_REPORT_WEBHOOK` (ou `ERROR_REPORT_WEBHOOK`, no botão manual) um relatório **mínimo** do erro é enviado a um canal do Discord: o tipo da exceção e o código `ERR-XXXXXX`, sem traceback, sem ID de usuário, de servidor ou de canal (seção 4). Use um canal privado.

---

## 1. Chaves de Configuração

Todas ficam no `.env` (modelo comentado em `.example.env`). `NO_COLOR` é lida só do ambiente.

| Chave | Valores | Padrão | O que faz |
| :--- | :--- | :--- | :--- |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` | `INFO` | Nível do console e do arquivo. Valor inválido vira `INFO`, com um aviso na subida. |
| `LOG_COLOR` | `on`, `off` | `on` | Cor por severidade **só no console** (nunca no arquivo). `LOG_COLOR=off` ou `NO_COLOR` não vazia desligam. |
| `LOG_FILE` | `auto`, `on`, `off` | `auto` | Arquivo de log. `auto` liga fora da imagem Docker e desliga dentro dela. |
| `LOG_RETENTION_DAYS` | dias, de 30 a 730 | `183` | Idade máxima dos arquivos de log (seção 1.2). Texto inválido usa 183; número fora da faixa vai para o limite mais próximo, com aviso. |
| `LOG_RETENTION_HOLD` | `true`, `false` | `false` | Suspensão legal: com `true` a poda não apaga nada (seção 1.3). |
| `LOG_DEBUG_GUILDS` | IDs separados por vírgula | vazio | Servidores que registram em `DEBUG` mesmo com `LOG_LEVEL=INFO`. No máximo 50 IDs. |
| `METRICS_INTERVAL` | segundos | `300` | Intervalo do resumo periódico. Mínimo 60 (de 1 a 59 vira 60); `0` desliga o resumo. |
| `COMMAND_LOG` | `true`, `false` | `false` | Registra os comandos de quem não é dono (seção 5). |
| `USAGE_RETENTION_DAYS` | dias, de 1 a 365 | `30` | Retenção do registro de uso e dos eventos de reprodução (seção 7). Inválido ou menor que 1 usa 30. |
| `ENABLE_LOGGER` | `true`, `false` | `false` | Sinônimo antigo de `LOG_FILE=on`; nunca desliga nada. |
| `PANEL_TRUSTED_PROXY_CIDRS` | redes (CIDR) separadas por vírgula | vazio (loopback e redes privadas) | Quem pode mandar o `CF-Connecting-IP` com `PANEL_TRUST_PROXY=true` (seção 8). |

### 1.1 Nível e `LOG_DEBUG_GUILDS`

Em `INFO`, o console só mostra **problemas e o resumo periódico**: início e fim de faixa, comandos e eventos de rotina ficam em `DEBUG`. Isso mantém o console legível com centenas de servidores tocando.

Para investigar **um** servidor, ponha o ID dele em `LOG_DEBUG_GUILDS` e reinicie o bot: só os registros do EmpireMuse nesse servidor sobem para `DEBUG`; o restante continua em `INFO`. O `LOG_LEVEL=DEBUG` global é temporário: com muitos servidores ele enche o teto diário do arquivo (64 MiB) em minutos.

As bibliotecas externas (disnake, aiohttp, pymongo, etc.) ficam sempre em `WARNING`.

### 1.2 Arquivo e rotação

O arquivo do dia é `RUNTIME_DIR/.logs/disnake-AAAA-MM-DD.log` (data em **UTC**), com permissão 0600 (pasta 0700). Ele troca à meia-noite UTC e fica em append, então os códigos `ERR-XXXXXX` sobrevivem a um reinício. Cada dia tem um **teto de 64 MiB**: passado o teto, o bot escreve uma linha `log do dia truncado` e descarta o resto do dia (o dia seguinte é normal); o teto nunca apaga arquivo. O nível do arquivo é o `LOG_LEVEL`, o mesmo do console, e o conteúdo é o mesmo, só sem cor, sem emoji e com a data completa (dá para usar `grep`).

No Docker o arquivo vem desligado por padrão, e os composes de produção ligam com `LOG_FILE=on` (`/data` é volume). O driver `json-file` do Docker (10 MB × 3) continua como conveniência de depuração e **não é o registro**: ele gira por tamanho (cobre poucos dias) e some quando o contêiner é recriado.

### 1.3 Retenção e suspensão legal

A poda apaga **somente** arquivo cujo nome casa exatamente com `disnake-AAAA-MM-DD.log` (ou `access-AAAA-MM-DD.log`), que é arquivo comum (link simbólico, pasta e qualquer outro nome ficam) e cuja data **do nome** é mais velha que `LOG_RETENTION_DAYS` (o padrão é 183 dias, cerca de 6 meses). A data vem do nome, nunca da data de modificação, então copiar ou restaurar um backup não adia a poda. A poda roda na subida, na troca de dia e a cada hora no ciclo de manutenção do log (`tick_logging`).

`LOG_RETENTION_HOLD=true` **pausa a poda por inteiro** (ordem de guarda de registros, Marco Civil art. 15 §2). Procedimento: com a ordem em mãos, ligue o `LOG_RETENTION_HOLD`, reinicie, copie para um local restrito os arquivos pedidos, registre a ordem e a cópia no runbook de operação e só desligue o `LOG_RETENTION_HOLD` quando a ordem acabar. Com a suspensão ligada o bot avisa na subida e os arquivos se acumulam (até 64 MiB por dia). **Atenção:** os arquivos do formato antigo (`disnake.log` e `disnake.log.1` a `.3`) **não são podados** (o nome não segue o padrão, e a poda só apaga o que reconhece); apague-os à mão depois de atualizar. Esta é a retenção só do **bot**; o registro de acesso do proxy (Traefik) e os demais armazenamentos têm a retenção própria descrita no contrato de apagar dados.

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
2. Procure o código: `grep ERR-XXXXXX .logs/disnake-*.log` (ou `docker logs <container> | grep ERR-XXXXXX`).
3. A linha traz o traceback e o contexto do comando: nome, `user_id`, `guild_id`, `channel_id` e os parâmetros **resumidos por tipo**. Modelos do Discord aparecem como `Tipo(id=…)`, anexos só como tipo e tamanho, e **texto livre só como tamanho** (`str(len=N)`). O valor, redigido e truncado, só aparece em `DEBUG` ou com o servidor em `LOG_DEBUG_GUILDS`. Parâmetro com nome de segredo sai como `***`.

Erros esperados do usuário (cooldown, permissão, argumento inválido) ficam em `DEBUG`.

### Webhook de erro

O `AUTO_ERROR_REPORT_WEBHOOK` copia o aviso de cada erro inesperado para um canal do Discord, onde ele **não tem prazo**. Por isso o relatório é mínimo:

- **todo remetente** ao webhook (comandos, jukebox, reprodução e o aviso de falha de faixa) segue a mesma regra; abaixo, o que cada parte leva;
- o **anexo** leva só o tipo da exceção e o código `ERR-XXXXXX`; o texto da exceção e o traceback completo **não saem do host** e ficam no arquivo de log, que tem retenção (procure pelo código, seção 4);
- o **embed** leva só um título fixo e o código do erro: **nunca** nome ou ID de usuário, servidor ou canal, comando, opções, texto da mensagem, ícone ou avatar;
- o **modal** "Reportar erro" (botão manual) leva só o texto livre que o próprio membro digitou (e o print opcional), sem autor, sem ícone e sem servidor.

**Expurgo manual:** apague o conteúdo do canal do webhook a cada **183 dias** (rotina do runbook de operação), já que o Discord não aplica prazo. Se não quiser manter essa rotina, deixe o `AUTO_ERROR_REPORT_WEBHOOK` e o `ERROR_REPORT_WEBHOOK` vazios.

Exceções não tratadas (do loop, de threads e do `sys.excepthook`) também saem no log de erros, com redação, desde o começo da subida.

---

## 5. `COMMAND_LOG`

Com `COMMAND_LOG=true`, cada comando de quem não é dono gera uma linha `INFO` (logger `empiremuse.commands`) no console **e** no arquivo. Ela contém só:

- `user_id` e `guild_id` (nunca nome de usuário, apelido nem nome do servidor);
- o nome qualificado do comando;
- as opções resumidas como na seção 4. Em comandos de prefixo, o resto da mensagem sai só como `str(len=N)`, **salvo nos servidores de `LOG_DEBUG_GUILDS`**: ali o texto livre (opções e resto da mensagem de prefixo, até 100 caracteres por parâmetro e 500 no conjunto, redigido e saneado) sai também em `INFO`, no console e no arquivo.

IDs são dado pessoal (LGPD). Mantenha o `COMMAND_LOG` desligado quando não precisar, e lembre que a retenção é a do arquivo diário (`LOG_RETENTION_DAYS`, seções 1.2 e 1.3) e a do driver de log do Docker.

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

---

## 8. IP do visitante atrás do proxy (`PANEL_TRUSTED_PROXY_CIDRS`)

Com `PANEL_TRUST_PROXY=true` o painel, a página de status e o relé RPC usam o IP do cabeçalho `CF-Connecting-IP` (para o limite de tentativas do login e para os registros). O cabeçalho só vale se quem abriu a conexão (o **par TCP**, medido antes de qualquer cabeçalho) estiver em `PANEL_TRUSTED_PROXY_CIDRS`. O padrão, com a variável vazia, é loopback e redes privadas, o que cobre o Traefik na rede interna do compose. Conexão de fora da lista usa o próprio IP do par e ignora o `CF-Connecting-IP`, o `X-Forwarded-For` e o `X-Real-IP`; assim um cliente que alcance a porta sem passar pelo proxy não escolhe o IP que fica registrado. Entrada inválida é ignorada com um aviso e, **se todas forem inválidas, nenhum proxy é confiável** (o IP vira o do par; nunca o padrão). Há avisos também para entrada com bits de host (`10.0.0.5/8` vale `10.0.0.0/8`) e para `0.0.0.0/0` ou `::/0` (confia em qualquer um). Se o IP do par não puder ser lido, o IP do visitante fica como `unknown` e sai um aviso.

Isto **não substitui** a regra de rede: o bot não deve publicar a porta, e só o proxy (aceitando apenas as faixas da Cloudflare) deve alcançá-la. Ela fica no deploy.
