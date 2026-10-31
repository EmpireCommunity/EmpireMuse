# Implantação com Docker

Guia prático para execução do **EmpireMuse** em contêineres Docker utilizando a imagem oficial multi-stage com o bot e o servidor de áudio Lavalink integrados.

---

## 1. O que a Imagem Traz

A imagem Docker do EmpireMuse foi projetada para oferecer uma implantação simples, segura e auto-contida em uma única instância:

- **Bot e Lavalink no mesmo contêiner**: O bot em Python 3.14 e o servidor de áudio Lavalink 4.2.2 rodam coordenados no mesmo contêiner, eliminando a complexidade de orquestrar múltiplos serviços.
- **Runtime Java 25**: Inclui o JRE oficial do **Eclipse Temurin 25**, atendendo aos requisitos do Lavalink e seus plugins.
- **Isolamento de rede local**: O Lavalink é vinculado estritamente ao endereço de loopback (`127.0.0.1` via `SERVER_ADDRESS`), comunicando-se exclusivamente com o bot via socket interno e nunca expondo portas de áudio para fora do contêiner.
- **Segurança e privilégios mínimos**:
  - Usuário não-root dedicado com UID/GID fixo `10001:10001` (`empiremuse`) e shell restrito (`/usr/sbin/nologin`).
  - Código-fonte da aplicação em `/app` de propriedade de `root` e estritamente somente leitura para o usuário da aplicação.
  - Binários e configurações pré-instaladas do Lavalink em `/opt/lavalink` em modo somente leitura.
- **Gerenciamento com `tini`**: O contêiner utiliza o inicializador `tini` como PID 1, garantindo o correto repasse de sinais do sistema operacional (`SIGTERM`, `SIGINT`) e a eliminação de processos zumbis.
- **Healthcheck por heartbeat**: o bot grava um carimbo de tempo em `EMPIREMUSE_HEALTH_FILE` (`/tmp/empiremuse.health`) a cada 15 s, apagado na subida, só enquanto todos os bots estão prontos e, havendo nós de áudio configurados, ao menos um está disponível. O `HEALTHCHECK` (`python -m empiremuse.health`) falha se o carimbo tiver mais de 60 s, está ausente ou a variável não existe; sem tokens válidos o container nunca fica `healthy`. Não depende do `RUN_RPC_SERVER` nem de porta HTTP, e tem 180 s de carência (`start-period`) na subida.
- **Versão na imagem**: `EMPIREMUSE_VERSION` traz a versão da tag (`dev` em builds locais).

---

## 2. Como Construir e Executar

### 2.1 Construção Local da Imagem

Para construir a imagem localmente a partir da raiz do repositório:

```bash
docker build -t empiremuse:local .
```

> [!IMPORTANT]
> O Docker Hub publica a versão exata, nunca republicada (`or4cu1o/empiremuse:X.Y.Z`, ex.: `2.0.2`) e as tags flutuantes `X.Y`, `X` (`2`) e `latest`. Em produção, prefira a versão exata ou a `:2`.

### 2.2 Execução com `docker run`

Antes de iniciar, prepare o arquivo de variáveis de ambiente `.env` na máquina hospedeira com as configurações do seu bot (utilize `.example.env` como referência):

```bash
docker run -d \
  --name empiremuse \
  --restart unless-stopped \
  --env-file .env \
  -v empiremuse-data:/data \
  -p 127.0.0.1:6000:6000 \
  empiremuse:local
```

**Parâmetros fundamentais:**
- `--restart unless-stopped`: Política de reinício recomendada. Caso o contêiner pare inesperadamente ou após uma falha de subprocesso, o Docker reinicia o bot automaticamente.
- `--env-file .env`: Injeta suas credenciais e configurações (tokens do Discord, chaves de API, etc.).
- `-v empiremuse-data:/data`: Monta um volume persistente do Docker no caminho `/data`.
- `-p 127.0.0.1:6000:6000`: Mapeia a porta HTTP do contêiner (definida por `PORT=6000`) para acesso à página de status, WebSocket de Rich Presence e ao Painel Web.

---

## 3. Docker Compose

O [índice do `deploy/`](../../deploy/README.md) diz qual arquivo usar em cada tipo de implantação. Os arquivos de compose ficam em [`deploy/`](../../deploy/): o base [`deploy/docker-compose.yml`](../../deploy/docker-compose.yml), o override do Traefik (seção 3.5) e o de produção da VPS na Hostinger (seção 3.6). O base está pronto para uso, oferecendo orquestração declarativa, isolamento seguro e suporte opcional a banco de dados MongoDB em contêiner dedicado.

> [!WARNING]
> A orquestração via Docker Compose foi validada estruturalmente com `docker compose config` e testes estáticos, mas **ainda não foi testada com o bot conectado (teste real pendente)** (validação com instância real agendada para a etapa D-16).

### 3.1 Executando Apenas o Bot

Para executar o bot com o servidor Lavalink local integrado e persistência em banco local (TinyMongo):

1. Certifique-se de que o arquivo `.env` na raiz do projeto está preenchido com suas credenciais;
2. Execute, a partir da raiz do repositório (o compose lê o `.env` e o `.env.mongo` da raiz):
   ```bash
   docker compose -f deploy/docker-compose.yml --env-file .env up -d
   ```

### 3.2 Executando com MongoDB Dedicado (`deploy/docker-compose.deps.yml`)

O MongoDB opcional (`mongo:8.0.34`, fixado por digest imutável) tem o próprio arquivo, [`deploy/docker-compose.deps.yml`](../../deploy/docker-compose.deps.yml). Ele sobe sempre com autenticação e publica a porta só em `127.0.0.1`.

1. Crie o arquivo `.env.mongo` na raiz do projeto (**nunca versione este arquivo**; ele já está incluído no `.gitignore`). Ele guarda só o usuário root, usado na criação do banco:
   ```env
   MONGO_INITDB_ROOT_USERNAME=empiremuse_admin
   MONGO_INITDB_ROOT_PASSWORD=sua_senha_segura_aqui
   ```
2. Suba o banco junto com o bot, sempre o par de arquivos (subir o `docker-compose.yml` sozinho no mesmo projeto trata o `mongo` como órfão, e `--remove-orphans` o derrubaria):
   ```bash
   docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.deps.yml --env-file .env up -d
   ```
   Se você usa o Traefik (seção 3.5), inclua também o override: `-f deploy/docker-compose.yml -f deploy/docker-compose.traefik.yml -f deploy/docker-compose.deps.yml`. Sem ele, o bot perde os rótulos e a rede `proxy`.
3. Crie um usuário próprio do bot, com a função `readWriteAnyDatabase` (Mongo dedicado ao bot; em instância compartilhada, veja o `.example.env`), e use esse usuário no `MONGO` do `.env` (o usuário root serve só para criar o banco). O host é o serviço `mongo`:
   ```env
   MONGO='mongodb://usuario_do_bot:senha_do_bot@mongo:27017/?authSource=admin'
   ```
   > [!NOTE]
   > O parâmetro `?authSource=admin` é necessário na URL de conexão porque os usuários são criados no banco de autenticação `admin`. Codifique com percent-encoding o usuário e a senha antes de colocá-los na URL (só eles, não a URL inteira): todo caractere reservado, como `@ : / ? # [ ] $ %`, vira `%` e o código hexadecimal (por exemplo, `@` vira `%40`).
4. Depois de salvar o `MONGO` no `.env`, recrie o bot com o mesmo comando do passo 2 (editar o `.env` não altera o contêiner em execução).

Os dados ficam no volume `empiremuse_empiremuse-mongo`, o mesmo do antigo perfil `mongo`. Nunca use `down -v` nele. Para rodar só o banco (por exemplo, para o bot sem container em [`deploy/local/`](../../deploy/local/)), suba `docker compose -f deploy/docker-compose.deps.yml --env-file .env up -d` (o `--env-file` faz valer o `MONGO_LOCAL_PORT` do `.env`) e use no `MONGO` o host `127.0.0.1` com a porta publicada (`27017` por padrão, ou o valor de `MONGO_LOCAL_PORT`): `MONGO='mongodb://usuario_do_bot:senha_do_bot@127.0.0.1:27017/?authSource=admin'`.

### 3.3 Variáveis de Configuração do Compose

Você pode customizar o comportamento do Compose através do seu arquivo `.env` ou de variáveis de ambiente no host:

| Variável | Valor Padrão | Descrição |
| :--- | :--- | :--- |
| `EMPIREMUSE_IMAGE` | `or4cu1o/empiremuse:latest` | Nome da imagem Docker a ser utilizada (em produção recomenda-se fixar a versão exata `or4cu1o/empiremuse:2.0.2` (ou o digest) ou a tag flutuante `:2` que acompanha a série 2.x). |
| `EMPIREMUSE_MEM_LIMIT` | `1024m` | Limite de memória do contêiner do bot (cobre processo Python e heap do Java). |
| `EMPIREMUSE_PORT` | `127.0.0.1:6000` | Mapeamento da porta HTTP exposta no host para o bot (padrão em loopback). |

> [!TIP]
> Por padrão, a porta é publicada apenas na interface de loopback (`127.0.0.1:6000`), ideal para ambientes com proxy reverso (como Nginx, Caddy ou Traefik) rodando no mesmo host. Se desejar expor publicamente ou alterar a porta, ajuste `EMPIREMUSE_PORT` no `.env`.

### 3.4 Garantias de Segurança do Compose

O arquivo `deploy/docker-compose.yml` aplica uma política rigorosa de segurança defensiva:

- **Exposição Mínima de Portas**: Apenas a porta HTTP 6000 do bot é publicada no host (vinculada a `127.0.0.1` por padrão). O Lavalink (vinculado estritamente em `127.0.0.1` dentro do contêiner) e o serviço `mongo` (porta 27017 acessível exclusivamente na rede interna do Compose) **nunca** têm portas publicadas para fora.
- **Sistema de Arquivos Somente Leitura**: O contêiner do bot roda com `read_only: true`. Diretórios temporários de escrita utilizam `/tmp` montado em memória (`tmpfs`).
- **Isolamento de Privilégios**: `security_opt: [no-new-privileges:true]` previne escalada de privilégios e `cap_drop: [ALL]` descarta todas as capacidades Linux (Linux Capabilities).
- **Limites e Resiliência**: `mem_limit` e `memswap_limit` fixados em `1024m` (sem swap), `pids_limit: 256`, `stop_grace_period: 30s` e rotação de logs (`json-file` 10m x 3) para bot e mongo.
- **Volumes Nomeados**: Os dados são isolados nos volumes `empiremuse-data` (para `/data` do bot) e `empiremuse-mongo` (para `/data/db` do MongoDB, no compose de deps).
- **Identificação e Nomes Determinísticos**: O compose declara `name: empiremuse` e nomes de contêineres fixos (`container_name: empiremuse` para o bot e `container_name: empiremuse-mongo` para o MongoDB), padronizando a inspeção e comandos operacionais (`docker logs empiremuse`, `docker top empiremuse`) independentemente do diretório hospedeiro.

### 3.5 Produção atrás do Traefik e da Cloudflare

Para implantar o EmpireMuse em um servidor de produção com proxy reverso gerenciado pelo **Traefik v3** e proteção da **Cloudflare**, utilize o arquivo de override [`deploy/docker-compose.traefik.yml`](../../deploy/docker-compose.traefik.yml).

Esta configuração segue as práticas de segurança do [Traefik com Cloudflare (docker-compose-cookbook)](https://github.com/Or4cu1o/docker-compose-cookbook/tree/main/composes/traefik):
- A VPS mantém apenas a porta **443** aberta, aceitando conexões exclusivamente da Cloudflare via mTLS (**Authenticated Origin Pulls**), certificado **Cloudflare Origin CA** e lista de IPs permitidos (`cloudflare-only`).
- Nenhuma porta do bot é exposta no host (`ports: !reset []`). Toda a comunicação passa pela rede interna do Docker (`proxy`).
- Atrás do Traefik do cookbook, `PANEL_TRUST_PROXY=true` faz o painel web utilizar com segurança o endereço IP real enviado pela Cloudflare (`CF-Connecting-IP`).

#### Passo a Passo de Implantação:

1. **Rede Externa `proxy`**:
   Certifique-se de que a rede gerenciada pelo Traefik já existe no host (se ainda não existir, crie-a):
   ```bash
   docker network create proxy
   ```

2. **Configuração das Variáveis de Ambiente no `.env`**:
   No arquivo `.env` na raiz do projeto, defina as variáveis operacionais para o ambiente atrás do proxy:
   ```env
   # Domínio público FQDN (usado na regra Host(...) do Traefik; obrigatório com o override):
   EMPIREMUSE_DOMAIN=muse.seudominio.com

   # Ativação do servidor web/RPC interno do bot:
   RUN_RPC_SERVER=true
   WEB_BIND_ADDRESS=0.0.0.0

   # Configurações do Painel Web:
   PANEL_ENABLED=true
   PANEL_BASE_URL=https://muse.seudominio.com
   # Atrás do Traefik do cookbook, faz o painel usar o IP enviado pela Cloudflare (CF-Connecting-IP):
   PANEL_TRUST_PROXY=true

   # Configurações do RPC (Rich Presence):
   # O bot fala com o relé local internamente no contêiner, sem circular pela Cloudflare:
   # (utilize aspas simples para impedir que o Docker Compose interpole a variável $PORT)
   RPC_SERVER='ws://localhost:$PORT/ws'
   # Endereço público entregue aos clientes e exibido no comando /rich_presence:
   RPC_PUBLIC_URL=wss://muse.seudominio.com/ws
   ```

3. **Cadastro do Callback OAuth no Discord**:
   No [Discord Developer Portal](https://discord.com/developers/applications), na página da sua aplicação em **OAuth2 > Redirects**, cadastre a URL de callback:
   ```
   https://<domínio>/painel/callback
   ```

4. **Inicialização com Override do Compose**:
   Inicie a stack combinando o `deploy/docker-compose.yml` base com o arquivo de override:
   ```bash
   docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.traefik.yml --env-file .env up -d
   ```

> [!NOTE]
> O Traefik já define os entrypoints, certificados e middlewares (`cloudflare-only` e `secure-headers`) por padrão na entrada 443; o override do EmpireMuse declara o roteamento de domínio (`Host`), a porta interna do serviço (`6000`), a rede `proxy` (com a label `traefik.docker.network=proxy` para desambiguação de interfaces) e a rede interna `default` para alcançar o MongoDB quando o perfil `mongo` for utilizado.

### 3.6 Gerenciadores de Contêineres em Arquivo Único (Hostinger e Painéis Sem Suporte a `-f`)

Em gerenciadores de Docker integrados a painéis de VPS (como o **Docker Manager da Hostinger**), existem restrições operacionais específicas:
- **Arquivo Único**: Não é possível encadear múltiplos arquivos compose com `-f` (`docker compose -f docker-compose.yml -f ...`); toda a configuração do serviço precisa estar consolidada em um único arquivo `docker-compose.yml`.
- **Sem `env_file`**: Nesses gerenciadores, a diretiva `env_file` não é suportada. O arquivo `.env` serve apenas para que o painel web interpole variáveis declaradas no compose sob a sintaxe `${VARIAVEL}`.
- **Limite de 256 Caracteres por Variável**: No formulário de `.env` do painel web, cada valor aceita no máximo 256 caracteres, e o formulário recusa comentários em linha nos campos de valores.

Para esse cenário de implantação em arquivo único atrás do Traefik, o repositório traz o compose usado na produção do EmpireMuse (VPS na Hostinger, atrás do Traefik + Cloudflare):

- [`deploy/docker-compose.hostinger.yml`](../../deploy/docker-compose.hostinger.yml): copie para o diretório do projeto no gerenciador (ex.: `/docker/empiremuse`) como `docker-compose.yml`. A cada release, atualize a tag **e o digest** das duas linhas `image:` (bot e `updater-proxy`), com o digest do `image-digest.txt` da release (passo 2 do [`release.md`](release.md), seção 3, Passo 5): trocar só a tag mantém a imagem antiga. Ou use o `update` com o auto-update ([`auto-update.md`](auto-update.md)).
- [`deploy/env.example.hostinger`](../../deploy/env.example.hostinger): renomeie para `.env` e preencha só os segredos.

- [`deploy/docker-compose.hostinger-sem-traefik.yml`](../../deploy/docker-compose.hostinger-sem-traefik.yml): o mesmo compose sem Traefik, para quem usa outro proxy com TLS. O painel fica só em `127.0.0.1:6000`; para expor, ponha o proxy na frente e nunca troque a porta por `"6000:6000"`. Preencha `PANEL_BASE_URL` e `RPC_PUBLIC_URL` com o endereço público do proxy. As duas linhas `image:` seguem a mesma regra do compose com Traefik.

O compose com Traefik segue a receita genérica mantida no repositório [**docker-compose-cookbook (`composes/empiremuse`)**](https://github.com/Or4cu1o/docker-compose-cookbook/tree/main/composes/empiremuse):
- O compose consolida todas as variáveis e padrões recomendados de forma *inline*, sem necessidade de overrides.
- O `.env` gerencia apenas os segredos, domínio e tokens de bot.
- **Múltiplos Bots (Um Token por Variável)**: Para operar múltiplos bots no mesmo pool sem estourar o limite de 256 caracteres de um único campo (evitando concatenar múltiplos tokens em uma única linha), declare variáveis independentes: `TOKEN=...`, `TOKEN_2=...`, `TOKEN_3=...` (até `TOKEN_5` ou mais). O EmpireMuse varre automaticamente todas as variáveis de ambiente em busca de tokens de bot do Discord válidos.

---

## 4. Variáveis de Ambiente e Recursos

### 4.1 Variáveis Padrão da Imagem

A imagem Docker já traz pré-configuradas as seguintes variáveis de ambiente operacionais:

| Variável | Valor Padrão | Descrição |
| :--- | :--- | :--- |
| `EMPIREMUSE_HOME` | `/data` | Diretório raiz de execução e persistência de dados. |
| `RUN_LOCAL_LAVALINK` | `true` | Ativa a inicialização automática do Lavalink no contêiner. |
| `LAVALINK_OFFLINE` | `true` | Modo offline: impede downloads de binários e valida os arquivos existentes por SHA-256. |
| `LAVALINK_HOME` | `/opt/lavalink` | Diretório dos binários validados do Lavalink (`Lavalink.jar`, `application.yml`, `plugins/`). |
| `LAVALINK_JAVA_PATH` | `/opt/java/openjdk/bin/java` | Caminho do executável do runtime Java 25 JRE pré-instalado. |
| `PORT` | `6000` | Porta do servidor HTTP Tornado (status, RPC e painel). |

> [!IMPORTANT]
> No arquivo `.example.env`, as variáveis operacionais do Lavalink (`RUN_LOCAL_LAVALINK`, `LAVALINK_OFFLINE`, `LAVALINK_JAVA_PATH` e `LAVALINK_HOME`) são mantidas comentadas propositalmente. No Docker, as variáveis definidas via `ENV` na imagem já ativam o Lavalink local embutido (`RUN_LOCAL_LAVALINK=true`). Caso você descomente essas chaves com valores vazios ou `false` em seu arquivo `.env`, o Docker Compose sobrescreverá o ambiente do contêiner e desligará o Lavalink local. Fora do Docker, o comportamento padrão é mantido pelo `config_loader` com o Lavalink local desativado (`RUN_LOCAL_LAVALINK=False`).

### 4.2 Dimensionamento de Memória (RAM) e Recursos

O uso de memória do contêiner é a soma de dois componentes principais:
1. **Processo do Bot (Python)**: Consumo estimado entre ~200 MB e ~300 MB dependendo da quantidade de servidores conectados, filas ativas e uso do painel web.
2. **Processo do Lavalink (Java)**: A JVM tem seu heap de memória configurado pelas variáveis:
   - `LAVALINK_INITIAL_RAM` (heap inicial `-Xms`, padrão `30` MB): define a memória inicial reservada pela JVM (repassada quando `0 < LAVALINK_INITIAL_RAM < LAVALINK_RAM_LIMIT`).
   - `LAVALINK_RAM_LIMIT` (heap máximo `-Xmx`, padrão `120` MB): define o teto máximo de memória que o Lavalink pode alocar (repassada quando `LAVALINK_RAM_LIMIT > 10`).
   - Além da memória, a alocação de núcleos de CPU da JVM é definida por `LAVALINK_CPU_CORES` (padrão `1`, repassada via `-XX:ActiveProcessorCount=`).

Ao configurar um limite de memória para o contêiner (como a flag `--memory` do `docker run` ou `mem_limit` / `EMPIREMUSE_MEM_LIMIT` no Docker Compose):
- O limite padrão recomendado do contêiner é de **768 MB** (`768m`), suficiente para cobrir com folga o processo Python, a JVM e as bibliotecas nativas de áudio.
- Para instâncias com grande volume de servidores ou dezenas de conexões de voz simultâneas, recomenda-se alocar **1 GB** (`1024m`) ou mais para o contêiner e expandir `LAVALINK_RAM_LIMIT` (por exemplo, `256` a `512` MB) conforme a demanda de faixas em fila.

> [!NOTE]
> Valores oficiais medidos em testes de carga e homologação real (etapa D-16) serão incluídos em revisões posteriores desta documentação.

---

## 5. O que Fica Persistido em `/data`

O contêiner executa a partir do diretório `/data` (`WORKDIR /data`), de modo que todos os dados que precisam sobreviver a reinicializações e atualizações de imagem são armazenados no volume persistente:

- **Banco de Dados Local**: Diretório `/data/local_database/` (quando utilizado o banco TinyMongo em vez do MongoDB remoto).
- **Configurações Locais**: Arquivo `config.json` ou sobrescritas locais, caso aplicáveis.
- **Caches de Sessão**: Caches do YouTube, Last.fm e sessões ativas do painel web.
- **Logs do Servidor de Áudio**: O Lavalink grava seus arquivos de log em `/data/.logs/lavalink/` (configurado via `-Dlogging.file.path`).
- **Overrides de Inicialização**: O arquivo gerado em `/data/.lavalink/plugins-override.yml`, que permite ao Lavalink iniciar sem consultas desnecessárias a repositórios externos.

---

## 6. Comportamento de Reinício e Resiliência

O EmpireMuse possui um mecanismo integrado de supervisão do processo de áudio:

1. **Vigia do Processo Java (`watch_lavalink_process`)**: Durante a execução, uma thread dedicada monitora o processo do Lavalink em tempo real.
2. **Tratamento de Quedas Inesperadas**: Se o processo do Java for finalizado de forma anômala (queda por falta de memória, encerramento externo, etc.), o bot detecta o término e aciona um encerramento gracioso do pool (`shutdown(1)`), saindo com código de status de erro `1`.
3. **Recuperação pelo Docker**: Com a política de reinício configurada (`--restart unless-stopped`), o daemon do Docker detecta a saída do processo e reinicia o contêiner de forma limpa, restabelecendo o bot e o Lavalink.

---

## 7. Como Atualizar

No modelo de contêineres, o ciclo de vida da aplicação é declarativo e imutável:

1. O comando `update` no Docker **não faz `git pull`**: com o proxy de atualização configurado (`UPDATE_PROXY_SOCKET`), o bot troca a imagem pelo auxiliar, por digest, e sem o proxy só mostra a versão nova. Como ligar, os códigos de saída e as limitações estão em [`auto-update.md`](auto-update.md).
2. Para atualizar o bot à mão:
   - Baixe ou construa a nova imagem (`docker pull` ou `docker build`);
   - Pare e remova o contêiner em execução (`docker stop empiremuse && docker rm empiremuse` ou `docker compose down`);
   - Inicie o novo contêiner mantendo o mesmo volume persistente (`-v empiremuse-data:/data` ou via compose) e o mesmo arquivo `.env`.

Seus dados, favoritos, configurações de servidores e histórico permanecerão intactos no volume `/data`.

---

## 8. Licença e Créditos

A imagem oficial contém cópias completas dos arquivos [`LICENSE`](file:///app/LICENSE) e [`CREDITS.md`](file:///app/CREDITS.md) sob o diretório `/app`. O **EmpireMuse** é disponibilizado sob a licença **GPL-2.0**, sendo uma obra derivada do projeto original [MuseHeart-MusicBot](https://github.com/zRitsu/MuseHeart-MusicBot) desenvolvido por **zRitsu**.
