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

## Retenção de logs (183 dias)

Na instância hospedada (VPS com Docker Manager e Traefik compartilhado), o **`logrotate` é pré-requisito padrão do host** (instalação, o arquivo `host/logrotate-traefik-access` e a verificação estão no runbook). Quem hospeda por conta própria sem essa pilha **não precisa** dele.

Arquivos para aplicar **à mão, numa janela e com o OK do dono**; o passo a passo, o que cada mudança derruba e as condições C1 a C7 estão em [`docs/runbook/log-retention.md`](../docs/runbook/log-retention.md).

| Arquivo | Para quê |
| :--- | :--- |
| [`traefik/accesslog-flags.txt`](traefik/accesslog-flags.txt) | Lista de flags e volume para o Traefik compartilhado: registro de acesso em arquivo, só com os campos necessários |
| [`host/logrotate-traefik-access`](host/logrotate-traefik-access) | `/etc/logrotate.d/` (padrão do host da instância hospedada): rotação diária e `maxage 183` do registro de acesso |
| [`host/lavalink-log-prune.sh`](host/lavalink-log-prune.sh), `host/empiremuse-lavalink-log-prune.{service,timer}` | Poda diária das cópias antigas do `spring.log` do Lavalink |

O `docker-compose.traefik.yml` é só um complemento do `docker-compose.yml`: nunca o suba sozinho.

## Antes de usar

- **Segredos só no `.env`.** Nenhum arquivo deste diretório leva token, senha ou chave: eles chegam por variáveis de ambiente. Copie `.example.env` (ou `env.example.hostinger`) para `.env`, preencha e nunca versione o resultado.
- **Digest da imagem.** Nos dois compose da Hostinger, as linhas `image:` trazem um marcador de zeros no lugar do digest. Troque pelo `image-digest.txt` da release ([`release.md`](../docs/guides/release.md)).
- **Baixe da release, não da `main`.** Para copiar um arquivo daqui, use a tag da versão que vai instalar: `https://raw.githubusercontent.com/EmpireCommunity/EmpireMuse/vX.Y.Z/deploy/<arquivo>`.
- **Pedindo ajuda.** Não cole o `.env` nem a saída de `docker compose config` em issue ou chat: ela mostra os valores já interpolados.
- **Sem suporte a terceiros.** Os arquivos são mantidos para a comunidade EmpireHub; usar em outro ambiente é por sua conta.
