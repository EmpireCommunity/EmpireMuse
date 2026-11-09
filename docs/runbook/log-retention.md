# Retenção de logs de 183 dias (6 meses): passo a passo da janela

Para quem opera a VPS de produção (Hostinger, Docker Manager, Traefik compartilhado). **Nada aqui é aplicado pelo CI nem pelo `/update`**: cada passo roda numa janela combinada, com o OK explícito do dono. Os arquivos versionados estão em [`deploy/traefik/`](../../deploy/traefik/) e [`deploy/host/`](../../deploy/host/).

## Padrão do deploy da instância hospedada

Na instância hospedada (VPS com Docker Manager e Traefik compartilhado), o **`logrotate` é pré-requisito padrão do host**: ele aplica a retenção de 183 dias do registro de acesso. O passo a passo é a seção 4: instalar o pacote, copiar [`deploy/host/logrotate-traefik-access`](../../deploy/host/logrotate-traefik-access) para `/etc/logrotate.d/traefik-access` e verificar (`logrotate -d`, o timer e, no dia seguinte, a rotação). Numa VPS existente isso só ocorre dentro da janela da seção 4, com o OK datado do dono e a C6 pronta.

**Quem hospeda por conta própria sem essa pilha não precisa dele.** Sem o Traefik compartilhado e o registro em arquivo, nada aqui é obrigatório: use a rotação do seu ambiente ou fique só com o `json-file` do Docker.

## 1. O que cada peça faz

| Peça | Arquivo | Efeito | Depende de |
| :--- | :--- | :--- | :--- |
| Registro de acesso (IP, data e hora, método, status) | [`deploy/traefik/accesslog-flags.txt`](../../deploy/traefik/accesslog-flags.txt) | O Traefik grava `/var/log/traefik/access.log` (JSON) no host, só com os campos listados | Nada |
| Retenção do registro | [`deploy/host/logrotate-traefik-access`](../../deploy/host/logrotate-traefik-access) | Rotação diária e `maxage 183`: a cópia com mais de 183 dias é apagada | `logrotate` no host (padrão da instância hospedada, ver acima) |
| Poda do `spring.log` do Lavalink | [`deploy/host/lavalink-log-prune.sh`](../../deploy/host/lavalink-log-prune.sh), `.service` e `.timer` | Apaga as cópias `spring.log.AAAA-MM-DD.N.gz` com mais de 30 dias | Nada |
| Log do bot em arquivo | `LOG_FILE` e `LOG_RETENTION_DAYS` (comentadas) em [`docker-compose.hostinger.yml`](../../deploy/docker-compose.hostinger.yml) | Arquivo em `/data/.logs/`, podado aos 183 dias | **E4a** (código do bot, PR à parte) |

O `json-file` do Docker (10 MB x 3) continua como conveniência de depuração e **não é o registro**.

### Campos do registro de acesso e qual IP vale

Só ficam `StartUTC`, `ClientHost`, `RequestHost`, `RequestMethod`, `DownstreamStatus`, `RouterName` e o cabeçalho `Cf-Connecting-Ip` (no JSON, `request_Cf-Connecting-Ip`), mais os campos de sistema do JSON (`time`, `level`, `msg`). Tudo o mais é descartado, inclusive `ClientAddr` e `ClientPort` (endereço e porta da **Cloudflare**, não do visitante), `RequestPath` (leva a query) e todos os outros cabeçalhos.

**Qual IP vale para um pedido legal:** o de **`request_Cf-Connecting-Ip`**, que a Cloudflare define na borda. `ClientHost` vem do cabeçalho `X-Forwarded-For`, e a Cloudflare **acrescenta** ao valor que o cliente mandou, então o primeiro item pode ter sido forjado pelo visitante; use-o só como apoio. Se os dois divergirem numa linha, **registre a diferença e não use `ClientHost` como o IP**: a divergência não prova forja (proxies corporativos também mandam `X-Forwarded-For`). O IP é o do `Cf-Connecting-Ip`, e só vale com a opção "Pseudo IPv4" (Overwrite Headers) da Cloudflare **desligada**, porque ela troca o valor do cabeçalho por um IPv4 fictício para visitantes IPv6: confirme isso no painel da Cloudflare em cada janela. O valor do `Cf-Connecting-Ip` só é confiável enquanto a origem recebe tráfego apenas da Cloudflare (condição C1, abaixo).

## 2. Pré-requisitos da janela e dependências

**Antes de ligar o registro (janela da seção 4), todos estes precisam estar prontos:**

- **OK do dono para guardar IPs de visitantes de TODOS os projetos** atrás do Traefik compartilhado por 183 dias, num arquivo no host. O registro é global ao Traefik, não só do EmpireMuse.
- **Alerta de disco ativo (C6, seção 7).** O arquivo não tem teto de tamanho; sem o alerta, uma inundação de requisições pode encher o disco do host.
- **"Pseudo IPv4" (Overwrite Headers) desligado na Cloudflare**: item de conferência de **toda** janela, feito pelo dono no painel; ligado, o `Cf-Connecting-Ip` não é o IP real de visitantes IPv6.
- **OK datado do dono** para a janela, incluindo a derrubada de 15 a 60 s da porta 443 de todos os projetos (seção 4). A instalação do `logrotate` nesta VPS acontece **só dentro** dessa janela, nunca antes.

**Ordem e dependências:**

- Traefik, `logrotate` e Lavalink **não dependem de código novo** e podem entrar antes da E4a.
- **Bot (`LOG_FILE=on` e `LOG_RETENTION_DAYS=183`) depende da E4a.** Hoje o código só tem o arquivo `disnake.log` de 5 MB x 3 (cerca de 20 MB) e ignora `LOG_RETENTION_DAYS`. Subir as linhas antes da E4a começa a gravar IDs em arquivo, mas **não cumpre os 183 dias**. Por isso as duas linhas estão **comentadas** nos compose da Hostinger: descomente-as no mesmo commit que fixar a imagem com a E4a.
- A E4a também traz o webhook de erro mínimo (C7) e a confiança no IP do painel (`PANEL_TRUSTED_PROXY_CIDRS`).

## 3. Condições C1 a C7

| # | Condição | Situação | Onde |
| :--- | :--- | :--- | :--- |
| C1 | Confiança no IP de origem (m3 do T-439) | Ver o registro privado do contrato; sem detalhe neste repositório | Contrato e Decisions privados |
| C2 | Cópias do provedor e janela do Atlas | **Precisa do dono** | Seção 8 |
| C3 | Traefik em arquivo + `logrotate`, verificado no dia seguinte | **Em arquivos**; exige a C6 pronta | `deploy/traefik/accesslog-flags.txt`, `deploy/host/logrotate-traefik-access`; seção 4 |
| C4 | `LOG_FILE=on` e `LOG_RETENTION_DAYS=183` no compose | **Em arquivos, comentadas** até a imagem com a E4a | Os dois compose da Hostinger; seção 6 |
| C5 | Retenção do `spring.log` e conferência do conteúdo | **Em arquivos** (30 dias) + **conferência no runbook**; se trouxer ID, 183 dias (decisão do dono) | Seção 5 |
| C6 | Alertas do host (disco, `logrotate`, escrita do registro) | **Runbook; pré-requisito da janela do C3**; o canal do alerta **precisa do dono** | Seção 7 |
| C7 | Expurgo do canal do webhook de erro a cada 183 dias | **Runbook** | Seção 9 |

## 4. Janela: Traefik e `logrotate` (C3)

**Pré-requisitos: os da seção 2.** O Traefik é **compartilhado por todos os projetos** da VPS.

### O que a mudança do Traefik derruba

Recriar o contêiner `traefik` (necessário para aplicar flags e volume) corta **toda a entrada HTTPS (porta 443) de todos os projetos atrás dele** por alguns segundos (recriação e `start_period` do healthcheck: planeje 15 a 60 s). Efeitos:

- Painel web, relé RPC (as conexões `/ws` caem e reconectam sozinhas), página de status, página legal e qualquer outro projeto atrás do Traefik respondem erro 52x da Cloudflare nesse intervalo.
- O **bot em si não cai**: o Discord e o áudio são conexões de saída, não passam pelo Traefik.
- O `json-file` do `traefik` (cerca de 12 dias de acessos) **é apagado na recriação**, como em qualquer deploy. **Não exporte esse histórico** (`docker logs traefik`): as linhas antigas trazem a query completa, que pode ter `code` e `state` de OAuth.
- Depois da mudança, `docker logs traefik` **deixa de mostrar acessos** (só o log do próprio Traefik); eles ficam em `/var/log/traefik/access.log`.
- Faça pelo Docker Manager (editando o compose do projeto do Traefik), como no restante da VPS, não à mão no servidor.

### Passos

1. **Host, pasta e arquivo do registro** (antes de recriar): `install -d -m 0750 -o root -g root /var/log/traefik` e `install -m 0640 -o root -g root /dev/null /var/log/traefik/access.log`. Sem isso o Docker cria a pasta 0755 e o Traefik cria o arquivo com modo mais aberto.
2. **Host, `logrotate`** (pré-requisito padrão, ver a seção "Padrão do deploy" acima): confira `command -v logrotate` e `systemctl list-timers logrotate.timer`. Se faltar, `apt install logrotate` (Debian, GPL-2+, mantido pelo Debian). O pacote ativa o `logrotate.timer` e passa a rotacionar também os outros logs do host que já têm configuração em `/etc/logrotate.d/`, conforme as regras dele.
3. **Host, configuração**: copie `deploy/host/logrotate-traefik-access` para `/etc/logrotate.d/traefik-access` (root, 0644). Confira: `logrotate -d /etc/logrotate.d/traefik-access` (só simula; deve citar "old logs are removed after 183 days").
4. **Traefik**: no compose do Traefik, acrescente as flags de `deploy/traefik/accesslog-flags.txt` ao `command:` e a linha de volume ao serviço `traefik`. Antes de recriar, rode `docker compose config -q` com o compose editado e revise a diferença. Depois, no Docker Manager: atualizar/recriar só o serviço `traefik`.
5. **Verifique na hora** (sem imprimir IPs):
   - `docker inspect --format '{{.State.Health.Status}}' traefik` deve dar `healthy`.
   - `curl -s -o /dev/null -w '%{http_code}\n' https://muse.empirecommunity.org/` deve dar 2xx ou 3xx.
   - `ls -l /var/log/traefik/access.log` existe, modo 0640, e cresce.
   - Campos e query: `tail -n 200 /var/log/traefik/access.log | python3 -I -c 'import sys,json; rows=[json.loads(l) for l in sys.stdin]; print(sorted({k for r in rows for k in r})); print("linhas com ?:", sum("?" in json.dumps(r) for r in rows)); print("sem Cf-Connecting-Ip:", sum("request_Cf-Connecting-Ip" not in r for r in rows)); print("ClientHost diferente:", sum(r.get("ClientHost")!=r.get("request_Cf-Connecting-Ip") for r in rows))'`. Esperado: só as chaves da lista da seção 1 (mais `time`, `level`, `msg`), **0 linhas com `?`**, 0 sem `Cf-Connecting-Ip`. Se aparecer `ClientAddr`, `ClientPort`, `RequestPath` ou qualquer `request_*` diferente de `request_Cf-Connecting-Ip`, **pare a janela**. Se `request_Cf-Connecting-Ip` não aparecer, a grafia do cabeçalho na flag não foi aceita (as flags foram conferidas só na documentação oficial, não executadas): reverta ou corrija a grafia e registre, não siga sem o IP da Cloudflare gravado.
6. **Verifique no dia seguinte** (C3): existe `/var/log/traefik/access.log-AAAAMMDD` do dia anterior (**sem `.gz`**: com `delaycompress` ele só é comprimido na rotação seguinte), o `access.log` novo tem linhas de hoje (o USR1 funcionou), `systemctl list-timers logrotate.timer` mostra a próxima execução, `systemctl is-failed logrotate.service` não dá `failed` e `journalctl -t traefik-logrotate` está vazio.

### Reversão

Retire do compose do Traefik as flags e o volume e recrie o serviço: os acessos voltam ao `json-file`. O que já foi gravado em `/var/log/traefik/` **continua lá** e continua sob o `maxage` (a configuração do `logrotate` só sai se o dono mandar apagar os arquivos). O compose anterior fica no `.bak-*` que o Docker Manager mantém.

## 5. Janela: Lavalink (C5)

1. **Conferir o conteúdo uma vez** (o contrato pede; ainda não foi lido). Só **contagens**, nunca imprima linhas. Na pasta `.../_data/.logs/lavalink` do volume `empiremuse_empiremuse-data` (em `/var/lib/docker/volumes/`), no `spring.log` e, com `zgrep -cE`, num `.gz` recente:
   - IDs do Discord: `grep -cE '[0-9]{17,20}' spring.log`.
   - Tokens do Google (OAuth do YouTube): `grep -cE 'ya29\.[0-9A-Za-z_-]{20,}|1//[0-9A-Za-z_-]{20,}|AIza[0-9A-Za-z_-]{35}' spring.log`.
   - Zero nos dois: segue 30 dias. IDs acima de zero: o dono decide entre 30 e 183 dias e o `spring.log` passa a ser tratado como log de segurança. **Token acima de zero: pare e avise o Regente** (a credencial está em texto no disco; ver `youtube-oauth.md`).
2. Instale (sem ligar o timer): `lavalink-log-prune.sh` em `/usr/local/sbin/` (root, 0755); `.service` e `.timer` em `/etc/systemd/system/`. Para 183 dias, edite o `ExecStart` do `.service` para `/usr/local/sbin/lavalink-log-prune.sh <pasta> 183`.
3. **Confirme o usuário do serviço**: `stat -c '%u:%g' <pasta>` deve dar o dono do volume (`10001:10001` no `.service`); se for outro, ajuste `User=` e `Group=`. O serviço roda como esse usuário, não como root. A capability `CAP_DAC_READ_SEARCH` serve para atravessar a pasta do Docker, mas **também permite ler qualquer arquivo do host**; por isso o serviço fica com `ProtectSystem=strict`, `ProtectHome=true` e o script só faz `find` nessa pasta. Confirme também que o usuário e o grupo resolvem no host (`getent passwd 10001; getent group 10001`; o systemd aceita o número, e o teste abaixo usa o mesmo `User=`): se o teste falhar por usuário ou grupo, **não ligue** o timer. Teste antes de ligar o timer, repetindo as restrições da unit (a lista vai para o terminal): `systemd-run --pipe --wait -p User=10001 -p Group=10001 -p AmbientCapabilities=CAP_DAC_READ_SEARCH -p CapabilityBoundingSet=CAP_DAC_READ_SEARCH -p NoNewPrivileges=true -p PrivateTmp=true -p ProtectHome=true -p ProtectSystem=strict -p ReadWritePaths=<pasta> -E DRY_RUN=1 /usr/local/sbin/lavalink-log-prune.sh`. Depois de instalar a unit (passo 4), o primeiro disparo real roda com o `.service` de verdade: leia o `journalctl` dele antes de confiar no timer. Se não alcançar a pasta, **não ligue** o timer e avise o Regente.
4. Leia a lista do teste do passo 3 (só as cópias com mais de 30 dias entram), depois `systemctl daemon-reload && systemctl enable --now empiremuse-lavalink-log-prune.timer`.
5. Verifique no dia seguinte: `systemctl list-timers empiremuse-lavalink-log-prune.timer` e `journalctl -u empiremuse-lavalink-log-prune.service` (lista o que apagou).

O script recusa uma pasta que não seja o caminho canônico (qualquer link simbólico no caminho) e só apaga cópias já comprimidas com o nome exato, nunca o `spring.log` em uso. Reversão: `systemctl disable --now` do timer (o que foi apagado não volta).

## 6. Janela: bot (C4, com a E4a)

Com a imagem que traga a E4a (digest conforme [`release.md`](../guides/release.md)), as linhas `LOG_FILE: "on"` e `LOG_RETENTION_DAYS: "183"` do compose da Hostinger passam a valer; elas estão comentadas no arquivo: **descomente-as no mesmo commit que fixar essa imagem** (sem a E4a, ligá-las grava IDs sem poda por idade). Recriar o `empiremuse` derruba o bot e o painel por instantes (deploy normal). Confira: `docker exec empiremuse ls -la /data/.logs` mostra `disnake-AAAA-MM-DD.log` (modo 0600) e a pasta 0700. A suspensão legal do bot (`LOG_RETENTION_HOLD`) é da E4a.

## 7. Alertas do host (C6): pré-requisito da janela do Traefik

O arquivo do registro de acesso **não tem teto de tamanho**; o crescimento normal é pequeno, mas uma inundação de requisições pode crescer rápido. Os alertas abaixo precisam existir e ter sido **testados** (dispare um de propósito) antes da seção 4:

1. **Disco:** uso de `/` acima de 80%, checado ao menos uma vez por dia.
2. **`logrotate`:** `systemctl is-failed logrotate.service` igual a `failed` (inclui a falha do USR1 ao Traefik) ou qualquer linha de `journalctl -t traefik-logrotate`. **Resolva uma falha do USR1 em até 24 h**: a cópia rotacionada só fica protegida até a rotação seguinte (ver o comentário do `logrotate`).
3. **Poda do Lavalink:** `systemctl is-failed empiremuse-lavalink-log-prune.service` igual a `failed`.
4. **Registro parado:** `/var/log/traefik/access.log` sem escrita há mais de 24 h (`find /var/log/traefik -name access.log -mmin +1440`).

O **canal** do alerta (e-mail, monitoramento da Hostinger ou um webhook, que é segredo) **precisa da decisão do dono**. Até haver canal, a janela do Traefik não começa; a conferência manual (`df -h /`, `du -sh /var/log/traefik`) não substitui o alerta.

## 8. Cópias do provedor e Atlas (C2, precisa do dono)

As imagens semanais do disco da VPS (Hostinger) incluem o volume de dados e `/var/log/traefik/`. O dono confere no painel da Hostinger quantas imagens ficam (decidido: cerca de 4) e, no console do Atlas, o backup e a janela do oplog. Dado apagado ou log podado permanece na imagem até ela ser sobrescrita; não dá para apagar item de uma imagem. A Política já diz "até 4 semanas".

## 9. Expurgo do canal do webhook de erro (C7)

O `AUTO_ERROR_REPORT_WEBHOOK` copia erros para um canal do Discord sem prazo. Depois da E4a ele leva só o tipo da exceção e o código `ERR-XXXXXX`, mas as mensagens antigas e o texto livre do modal de reporte continuam lá até o expurgo.

**A cada 183 dias.** O primeiro prazo conta da data em que a janela do Traefik (seção 4) entrar em produção; registre a data na tabela abaixo.

1. O dono (precisa do OK dele) apaga o canal de destino e cria outro de mesmo nome. A exclusão em massa por API não alcança mensagens com mais de 14 dias, por isso o caminho confiável é recriar o canal.
2. Cria um webhook novo no canal novo, atualiza `AUTO_ERROR_REPORT_WEBHOOK` no `.env` (é segredo: nunca no repositório nem no chat) e recria o contêiner do bot.
3. Registre aqui a data, quem executou e a próxima data.

| Data do expurgo | Executor | Próximo prazo |
| :--- | :--- | :--- |
| _(nenhum ainda)_ | | |

## 10. Suspensão legal (ordem de guarda)

Se houver ordem legal para guardar registros além de 183 dias, o dono decide e documenta: copie o `access.log*` e os arquivos pedidos para um local protegido (modo 0600, fora das pastas podadas) **antes** da poda, e suspenda a poda: **mova** a configuração do `logrotate` para fora de `/etc/logrotate.d/` (por exemplo para uma pasta de custódia; renomear no lugar não basta, o `logrotate` pode continuar lendo o arquivo) e desabilite o timer do Lavalink. Retome e apague a cópia quando a ordem acabar.
