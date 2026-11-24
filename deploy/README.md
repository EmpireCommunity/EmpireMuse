# Deploy do EmpireMuse

Qual arquivo usar para cada tipo de implantação. Os guias em [`docs/guides/`](../docs/guides/) trazem o passo a passo.

## Qual arquivo usar

| Quero rodar… | Arquivo | Comando ou uso | Auto-update (`update`) | Guia |
| :--- | :--- | :--- | :--- | :--- |
| Em qualquer máquina com Docker | [`docker-compose.yml`](docker-compose.yml) | `docker compose -f deploy/docker-compose.yml --env-file .env up -d` | Não (modo `panel`) | [`docker.md`](../docs/guides/docker.md) |
| Em servidor com Traefik, via `-f` | `docker-compose.yml` + [`docker-compose.traefik.yml`](docker-compose.traefik.yml) | `docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.traefik.yml --env-file .env up -d` | Não (modo `panel`) | [`docker.md`](../docs/guides/docker.md) (seção 3.5) |
| Em gerenciador de VPS de arquivo único, com Traefik | [`docker-compose.hostinger.yml`](docker-compose.hostinger.yml) + [`env.example.hostinger`](env.example.hostinger) | Copie como `docker-compose.yml`; o exemplo vira o `.env` | Sim | [`docker.md`](../docs/guides/docker.md) (seção 3.6), [`auto-update.md`](../docs/guides/auto-update.md) |
| Em gerenciador de VPS de arquivo único, sem Traefik | [`docker-compose.hostinger-sem-traefik.yml`](docker-compose.hostinger-sem-traefik.yml) + `env.example.hostinger` | Igual ao anterior; o painel fica em `127.0.0.1:6000` | Sim | [`docker.md`](../docs/guides/docker.md) (seção 3.6), [`auto-update.md`](../docs/guides/auto-update.md) |
| No Pterodactyl ou Pelican | [`pterodactyl/egg-empiremuse.json`](pterodactyl/egg-empiremuse.json) | Importe o egg no painel | Não (modo `panel`) | [`pterodactyl.md`](../docs/guides/pterodactyl.md) |
| Só o MongoDB (opcional), junto do bot em container | `docker-compose.yml` + [`docker-compose.deps.yml`](docker-compose.deps.yml) | `docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.deps.yml --env-file .env up -d` (sempre o par) | Não (modo `panel`) | [`docker.md`](../docs/guides/docker.md) (seção 3.2) |
| Só o MongoDB, para o bot sem container | [`docker-compose.deps.yml`](docker-compose.deps.yml) | `docker compose -f deploy/docker-compose.deps.yml --env-file .runtime/.env up -d` (troque `.runtime/.env` pelo `.env` que o `run.sh` usa: `$EMPIREMUSE_HOME/.env` se a variável existir, ou o `.env` da raiz; seção 3.2) | — | [`docker.md`](../docs/guides/docker.md) (seção 3.2) |
| Sem container (não recomendado) | [`local/run.sh`](local/run.sh) ou [`local/run.bat`](local/run.bat) | `sh deploy/local/run.sh` (Linux e macOS) ou `deploy\local\run.bat` (Windows) | Só no modo `git`, se rodar de um clone | [`development.md`](../docs/development.md) |
| Na Discloud | [`discloud.config`](../discloud.config) (na raiz) | Envie o projeto pelo painel da Discloud | Não (modo `manual`) | [`discloud.md`](../docs/guides/discloud.md) |
| Com nós Lavalink externos | [`lavalink.ini.example`](lavalink.ini.example) | Renomeie para `lavalink.ini` | — | [`development.md`](../docs/development.md) |

Quem hospeda por conta própria com o compose base e quer o `/update` copia o bloco `updater-proxy` (e os volumes `updater-sock` e `updater-proxy-data`) de [`docker-compose.hostinger.yml`](docker-compose.hostinger.yml), liga o bot ao socket (`UPDATE_PROXY_SOCKET` e o volume `updater-sock`) e define `DOCKER_GID` e `UPDATER_PROJECT_DIR` no `.env`. Veja [`auto-update.md`](../docs/guides/auto-update.md).

## Retenção de logs (183 dias)

Na instância hospedada (VPS com Docker Manager e Traefik compartilhado), o **`logrotate` é pré-requisito padrão do host** (instalação, o arquivo `host/logrotate-traefik-access` e a verificação estão no runbook). Quem hospeda por conta própria sem essa pilha **não precisa** dele.

Arquivos para aplicar **à mão, numa janela e com o OK do dono**; o passo a passo, o que cada mudança derruba e as condições C1 a C7 estão em [`docs/runbook/log-retention.md`](../docs/runbook/log-retention.md).

| Arquivo | Para quê |
| :--- | :--- |
| [`traefik/accesslog-flags.txt`](traefik/accesslog-flags.txt) | Lista de flags e volume para o Traefik compartilhado: registro de acesso em arquivo, só com os campos necessários |
| [`host/logrotate-traefik-access`](host/logrotate-traefik-access) | `/etc/logrotate.d/` (padrão do host da instância hospedada): rotação diária e `maxage 183` do registro de acesso |
| [`host/lavalink-log-prune.sh`](host/lavalink-log-prune.sh), `host/empiremuse-lavalink-log-prune.{service,timer,sysusers}` | Poda diária das cópias antigas do `spring.log` do Lavalink |

O `docker-compose.traefik.yml` é só um complemento do `docker-compose.yml`: nunca o suba sozinho.

## Firewall da porta 443 (só a Cloudflare)

Para quem hospeda atrás da Cloudflare com o Docker publicando a 443: o host só deve aceitar conexões **novas** de fora vindas das faixas da Cloudflare. Sem isso, quem descobre o IP do host fala direto com a origem, sem passar pela Cloudflare. O Docker publica a porta por DNAT, então o filtro vai na cadeia `DOCKER-USER` (a `INPUT` não veria esse tráfego). O SSH não é tocado.

| Arquivo | Para quê |
| :--- | :--- |
| [`host/cf-firewall.sh`](host/cf-firewall.sh) | `on`, `off`, `status`, `check`, `print`, `arm-undo` e `cancel-undo` (o cabeçalho do script explica cada um) |
| [`host/empire-cf-firewall.service`](host/empire-cf-firewall.service) | Liga o firewall quando o Docker sobe (boot, start ou restart) e o desliga/refaz com ele |
| [`host/cf-ips-v4.txt`](host/cf-ips-v4.txt), [`host/cf-ips-v6.txt`](host/cf-ips-v6.txt) | Faixas da Cloudflare, **estáticas e revisadas** (copiadas de `https://www.cloudflare.com/ips-v4` e `ips-v6`) |

Como funciona, em resumo:

- O gancho casa só `-m conntrack --ctstate NEW --ctdir ORIGINAL --ctorigdstport 443` pela interface pública. O que poupa as **respostas** das conexões HTTPS que os próprios containers abrem para fora é o `--ctstate NEW` (uma resposta nunca é `NEW`); o `--ctdir ORIGINAL` fica como defesa em profundidade, e os dois devem ficar.
- O script **não baixa nada**: lê as listas de `/etc/empire/`. Cada lista é validada antes de qualquer regra (CIDR válido, só endereços globais, prefixo no mínimo `/12` em IPv4 e `/29` em IPv6, que recusa `0.0.0.0/0` e `::/0`, e uma quantidade mínima de faixas). Qualquer falha sai com erro e **não toca em nada**.
- A troca é atômica: o `iptables-restore --noflush` carrega a cadeia inteira numa transação só (a cadeia nunca fica vazia nem só com o `DROP`) e o gancho entra depois, uma vez só. Rodar `on` de novo é seguro.
- IPv4 e IPv6. **Atenção ao IPv6:** se a rede do Traefik não tem IPv6 (ou o `ip6tables` do Docker está desligado), o Docker publica `[::]:443` pelo `docker-proxy`, que entra pela `INPUT` e **nunca** pela `DOCKER-USER`. Por isso o `on` **sempre** põe o mesmo gancho também na `INPUT` do `ip6tables` (só `tcp` 443, `NEW`, `ORIGINAL`, interface pública; SSH e outras portas não são tocados), haja ou não listener na hora: o Traefik pode subir depois do `on`. Se a `INPUT` do `ip6tables` não é filtrável (por exemplo `ipv6.disable=1`), o `on` falha antes de aplicar qualquer coisa, **e portanto o IPv4 também não é ligado**. O `status` falha se há listener em `[::]:443` (`ss`; sem `ss`, assume que há) e o gancho da `INPUT` não é exatamente 1.
- `on`, `off` e `cancel-undo` rodam sob `flock` exclusivo (`/var/lock/cf-firewall.lock`; precisa de `flock`, do util-linux): um `on` manual e o da unit não se atropelam. O `off` só diz "desligado" se tudo foi removido e sai com erro (dizendo o que restou) se não. O `cancel-undo` não confirma o cancelamento se o desfazer já começou.
- Se uma falha vier depois de começar a aplicar (por exemplo o IPv6 depois do IPv4), o `on` desfaz tudo e sai com erro: nunca fica metade ligado.
- Pré-condição QUIC: o filtro é só TCP. Confira `ss -lun 'sport = :443'` vazio (sem HTTP/3 publicado); se houver UDP 443, ele **não** é filtrado e a decisão é do operador.

Instalar (à mão, com o OK de quem opera o host, e com um **segundo acesso** ao host aberto, por exemplo o console do provedor):

1. Copiar as listas para `/etc/empire/` (0644), o script para `/usr/local/sbin/cf-firewall.sh` (root, 0755) e a unit para `/etc/systemd/system/`. Do mesmo commit/tag da release, não da `main`.
2. `cf-firewall.sh check` (só valida) e `cf-firewall.sh print` (mostra o que seria aplicado, sem aplicar).
3. Rede de segurança: `cf-firewall.sh arm-undo 10` agenda o `off` para daqui a 10 minutos. Depois `cf-firewall.sh on`.
4. Dentro dos 10 minutos, confira: o site abre pela Cloudflare; **um container ainda alcança a internet** (por exemplo `docker exec <container> ... https://example.com`, esperando 200); de fora da Cloudflare, `curl --max-time 8 --resolve <host>:443:<ip-do-host> https://<host>/` estoura o tempo, **e o mesmo por IPv6** (`curl -6 --max-time 8 --resolve <host>:443:[<ipv6-do-host>] https://<host>/`, se o host tem IPv6 público); o SSH segue vivo. O contador do `DROP` (`cf-firewall.sh status`) pode ser maior que zero: são conexões novas de fora, que é o efeito desejado.
5. Se estiver tudo certo: `cf-firewall.sh cancel-undo` (avisa se o desfazer já rodou) e `systemctl enable --now empire-cf-firewall.service`. Sem o `enable`, um reinício apaga as regras. Depois de todo reboot **e de toda parada ou reinício do Docker**, confira `systemctl is-active empire-cf-firewall.service` = `active` **e** `cf-firewall.sh status` com exit 0 e ganchos presentes (`is-failed` sozinho não pega uma unit inativa).

Voltar: `cf-firewall.sh off` (ou `systemctl disable --now empire-cf-firewall.service`).

Manutenção: as faixas da Cloudflare mudam raramente. Quando mudarem, atualize estas duas listas por revisão: uma faixa nova que não esteja nelas tem a conexão **cortada pelo firewall**. **Pré-condição: `docker info -f '{{.LiveRestoreEnabled}}'` tem de ser `false`; se for `true`, PARE e decida antes de instalar a unit.** Com `live-restore`, parar o Docker mantém os containers, o `docker-proxy` e o DNAT, mas o `PartOf` roda o `off`: a 443 ficaria aberta a todos, sem alarme, até o Docker voltar. **Atualização das faixas:** se uma troca de listas falhar (lista inválida), o `on` recusa sem tocar em nada e as regras anteriores continuam; mas o `systemctl restart`, o `PartOf` ou o `off` que o precede já desligaram o firewall, então um refresh que falha em cima de um `on` que funcionava **deixa o firewall desligado** (unit `failed`): confira `is-active` e `status`. Ciclo de vida: o Docker iniciado puxa a unit (`WantedBy=docker.service`), e o Docker parado ou reiniciado desliga/refaz o firewall (`PartOf`); com o Docker parado nada escuta a 443. Confira o comportamento da `DOCKER-USER` num reinício do Docker no seu ambiente.

**Verificado** (num network namespace vazio e descartável, sem tocar nenhuma regra do host; o CI roda isso como root a cada PR, e o passo falha se o teste for pulado): o `on` cria a cadeia e o gancho em IPv4 e IPv6 (regras e ganchos contados nas duas famílias); repetir o `on` não duplica o gancho; um `on` com lista inválida recusa e deixa cadeia e gancho intactos; o gancho da `INPUT` do IPv6 entra mesmo sem listener (e o `status` o exige quando o listener aparece depois); sem `INPUT` v6 filtrável o `on` falha sem ligar nem o IPv4; uma falha do `ip6tables-restore` depois do IPv4 desfaz tudo; o `off` remove tudo e é idempotente. Medido à mão antes: durante 120 trocas seguidas, leituras repetidas da cadeia só viram o conteúdo antigo ou o novo inteiro. **Não verificado:** a unit rodando no systemd de um host real (inclusive o endurecimento da unit), o comportamento com o Docker reiniciando e o tráfego real (conntrack com DNAT, `docker-proxy`) de ponta a ponta.

## Antes de usar

- **Segredos só no `.env`.** Nenhum arquivo deste diretório leva token, senha ou chave: eles chegam por variáveis de ambiente. Copie `.example.env` (ou `env.example.hostinger`) para `.env`, preencha e nunca versione o resultado.
- **Digest da imagem.** Nos dois compose da Hostinger, as linhas `image:` trazem um marcador de zeros no lugar do digest. Troque pelo `image-digest.txt` da release ([`release.md`](../docs/guides/release.md)).
- **Baixe da release, não da `main`.** Para copiar um arquivo daqui, use a tag da versão que vai instalar: `https://raw.githubusercontent.com/EmpireCommunity/EmpireMuse/vX.Y.Z/deploy/<arquivo>`.
- **Pedindo ajuda.** Não cole o `.env` nem a saída de `docker compose config` em issue ou chat: ela mostra os valores já interpolados.
- **Sem suporte a terceiros.** Os arquivos são mantidos para a comunidade EmpireHub; usar em outro ambiente é por sua conta.
