# Guia de Implantação na Discloud

Guia técnico para hospedagem e execução do **EmpireMuse** na plataforma **Discloud** utilizando os arquivos de configuração padronizados do projeto.

---

> [!NOTE]
> **Testado na Discloud em 2026-10-24 (plano Gold 1 GB): 335 MB parado, 383 MB tocando, Lavalink local v4 ok.**
> 
> Não foi testado de novo desde 2026-10-24: versões mais novas podem usar mais memória. Confira o consumo depois de atualizar.
> 
> **Requisito de Plano Pago**: O plano gratuito da Discloud (100 MB de RAM) **NÃO roda o bot**. O EmpireMuse requer **plano pago com no mínimo 1 GB (1024 MB) de RAM e 1 vCPU** (plano Gold ou superior), dimensão definida no parâmetro `RAM=1024` do `discloud.config`. Planos inferiores a 1 GB não possuem memória suficiente para acomodar o bot e o servidor de áudio integrado.

---

## 1. Visão Geral e Arquivo `discloud.config`

O arquivo [`discloud.config`](../../discloud.config) na raiz do repositório define os parâmetros da aplicação para a Discloud:

```ini
NAME=EmpireMuse
AVATAR=https://raw.githubusercontent.com/EmpireCommunity/EmpireMuse/main/assets/brand/avatar.png
TYPE=bot
MAIN=main.py
RAM=1024
# AUTORESTART=true (disponivel apenas a partir do plano Platinum; descomente se assinar Platinum+)
VERSION=latest
APT=tools
```

### Explicação dos Parâmetros:
- **`TYPE=bot`**: Declara a aplicação como um bot do Discord (sem roteamento de porta HTTP pública na Discloud).
- **`MAIN=main.py`**: Ponto de entrada que inicializa o loop do `BotPool` via `empiremuse.__main__.main()`.
- **`RAM=1024`**: Aloca 1024 MB (1 GB) de memória RAM, necessária para suportar o processo Python somado ao processo Java do servidor de áudio Lavalink.
- **`AUTORESTART=true`** *(Opcional / Platinum+)*: O reinício automático supervisionado pela Discloud está disponível exclusivamente a partir do plano **Platinum**. No plano Gold ou inferiores, este parâmetro deve permanecer comentado ou omitido no arquivo de configuração para evitar recusa no upload.
- **`VERSION=latest`**: Utiliza a imagem mais recente disponibilizada pela Discloud.
  > [!NOTE]
  > No teste realizado em **24 de outubro de 2026**, a Discloud executou **Python 3.15** sob a opção `VERSION=latest` (a versão 3.13 não é oferecida individualmente na plataforma, que disponibiliza apenas 3.11, versões legadas ou `latest`). Como `latest` seleciona a versão mais recente disponibilizada pela plataforma, recomenda-se revalidar o runtime nos logs da aplicação a cada deploy. A suíte e os módulos do EmpireMuse foram validados e são compatíveis com esse ambiente.
- **`APT=tools`**: Instala ferramentas essenciais do sistema operacional (como `git`); o `requirements.txt` não declara mais dependências via Git.

---

## 2. Formas de Envio (Deploy)

A Discloud oferece duas modalidades para publicação da aplicação. Priorize sempre a integração com o GitHub.

### 2.1 Método Preferido: Integração com o GitHub
A integração direta entre a Discloud e o seu repositório GitHub é a forma **mais segura e recomendada**:
1. Conecte seu repositório GitHub ao painel da Discloud;
2. Configure as variáveis de ambiente necessárias diretamente na interface web da Discloud (aba de variáveis do aplicativo);
3. **Vantagem de segurança**: As credenciais nunca trafegam em arquivos em disco nem correm risco de exposição em commits acidentais.

### 2.2 Método Alternativo: Upload via Arquivo ZIP ou Discloud CLI
Caso utilize o envio manual por arquivo `.zip` ou através da ferramenta de linha de comando (`discloud upload`):
1. Crie o arquivo `.env` na raiz do projeto com suas credenciais;
2. **Boas Práticas Mandatórias de Segurança**:
   - **NUNCA versione o `.env`**: Certifique-se de que o `.env` está presente no `.gitignore` e que `git status` não indica o arquivo como rastreado;
   - **Permissões locais restritas**: No Linux/macOS, restrinja a leitura do arquivo:
     ```bash
     chmod 600 .env
     ```
   - **Exclua o arquivo `.zip` local imediatamente**: Logo após o envio bem-sucedido para a Discloud, delete o arquivo compactado da sua máquina para evitar vazamento de credenciais locais.
3. O arquivo [`.discloudignore`](../../.discloudignore) já vem configurado para **bloquear** compulsoriamente o envio de:
   - Arquivos auxiliares de ambiente (`.env.*`, como `.env.mongo` e backups);
   - Segredos e chaves locais (`config.json`, `*.key`, `*.pem`, `.panel-cookie-secret`, cookies do YouTube);
   - Bancos de dados locais (`local_database/`, `.local_dbs/`) e caches temporários (`.logs/`, `__pycache__/`, `.ytdl_cache/`, etc.).

> [!NOTE]
> A pasta `assets/` **não** deve ser ignorada no deploy, pois `branding.asset_url` monta, uma vez, um índice dos arquivos das subpastas de `assets/` (`brand/`, `providers/`, `player/`, `skins/`, `ui/`, `docs/`) e confere a existência física de cada arquivo em tempo de execução antes de gerar URLs públicas para o player, provedores e tutoriais.

---

## 3. Configurações e Variáveis de Ambiente no `.env`

Como a Discloud configura instâncias do tipo `TYPE=bot` sem portas HTTP públicas roteadas para a Web:

### 3.1 Desativação de Serviços HTTP
Para economizar memória RAM e ciclos de CPU, desative explicitamente o servidor web e o painel no `.env` da Discloud:
```env
RUN_RPC_SERVER=false
PANEL_ENABLED=false
```

> [!NOTE]
> O decodificador de imagens roda em sandbox e exige suporte do kernel do host (Landlock, kernel 6.12 ou mais novo). Sem ele, logos e capas ficam no ícone padrão e o log mostra `SANDBOX-OFF` uma vez.

### 3.2 Variáveis Essenciais
```env
TOKEN=seu_token_do_discord_aqui
MONGO=mongodb+srv://usuario:senha@cluster.mongodb.net/?retryWrites=true&w=majority
```

---

## 4. Servidor de Áudio Lavalink na Discloud

O EmpireMuse oferece dois modos de operação para a reprodução de áudio:

### 4.1 Modo Lavalink Local Embutido (`RUN_LOCAL_LAVALINK=true`)
- Fora do contêiner Docker oficial, o bot opera em modo de download dinâmico com integridade verificada (`LAVALINK_OFFLINE=false`, o padrão);
- Na primeira partida na Discloud, o bot baixa automaticamente o runtime portátil JDK da BellSoft e o binário oficial `Lavalink.jar`, conferindo os hashes **SHA-256 fixados em código** antes da execução;
- Certifique-se de ajustar a variável `LAVALINK_RAM_LIMIT` (padrão de `120` MB) para que o heap da JVM somado à memória do processo Python permaneçam confortavelmente dentro do limite de 1 GB do plano contratado. Em teste real na Discloud (2026-10-24, plano Gold), o bot consumiu **335 MB parado e 383 MB tocando**, com o Lavalink local v4 executando de forma estável.

> [!WARNING]
> **Autenticação OAuth do YouTube em IPs de Datacenter**:
> Como os servidores da Discloud operam em blocos de IPs de datacenter, o YouTube frequentemente bloqueia requisições anônimas ou exige desafios de bot. Para garantir a reprodução estável de faixas do YouTube sem erros de rate-limit ou bloqueio HTTP 429, **é fortemente recomendado configurar a autenticação OAuth do YouTube**.
> Consulte o guia detalhado [Autenticação OAuth do YouTube](youtube-oauth.md) para vincular sua conta ao plugin Lavalink.

### 4.2 Modo Lavalink Remoto (Mais Leve)
- Para uma inicialização instantânea e menor consumo de recursos, defina `RUN_LOCAL_LAVALINK=false` e configure nós Lavalink remotos e externos no arquivo `lavalink.ini` na raiz do projeto (arquivo que é incluído no pacote enviado à Discloud).

---

## 5. Persistência de Dados e Banco de Dados

> [!IMPORTANT]
> Na infraestrutura da Discloud, o sistema de arquivos local pode ser recriado ou sobrescrito a cada novo deploy, atualização de código ou reinício da aplicação.
> 
> - **Banco Local (TinyMongo)**: O armazenamento em `local_database/` deve ser considerado puramente efêmero e transitório.
> - **MongoDB Externo Obrigatório para Produção**: Configure a variável `MONGO` apontando para um cluster externo gerenciado (por exemplo, uma instância gratuita ou dedicada no MongoDB Atlas). Com isso, favoritos, configurações de servidores, prefixos e histórico permanecem totalmente seguros e intactos entre deploys.

---

## 6. Resiliência e Reinício Automático (Plano Platinum+)

A diretiva `AUTORESTART=true` no `discloud.config` funciona a partir do plano **Platinum** da Discloud:
- O EmpireMuse possui uma thread sentinela que monitora o processo do Java (`watch_lavalink_process`);
- Se o servidor de áudio cair de forma anômala, o bot encerra o processo ordenadamente com código de saída de erro (`shutdown(1)`);
- No plano Platinum ou superior (com `AUTORESTART=true` descomentado no `discloud.config`), o supervisor da Discloud detecta o encerramento com código de falha e executa o reinício automático da aplicação, restabelecendo a operação;
- No plano Gold ou inferiores, a funcionalidade de auto-restart não é liberada pela Discloud, exigindo reinício manual pelo painel web ou bot da plataforma caso a aplicação pare.

---

## 7. Licença e Créditos

O **EmpireMuse** é disponibilizado sob a licença **GPL-2.0**, sendo uma obra derivada do projeto [MuseHeart-MusicBot](https://github.com/zRitsu/MuseHeart-MusicBot) desenvolvido por **zRitsu**.

Os arquivos [`LICENSE`](../../LICENSE) e [`CREDITS.md`](../../CREDITS.md) acompanham compulsoriamente todos os pacotes de deploy para a Discloud, preservando a íntegra dos avisos de direitos autorais e as fontes originais de código.
