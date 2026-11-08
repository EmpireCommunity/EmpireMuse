> Adaptado da wiki/gist do [MuseHeart-MusicBot](https://github.com/zRitsu/MuseHeart-MusicBot) (zRitsu), GPL-2.0.

# Guia do Lavalink Local: Atualização, Plugins e Fontes de Áudio

Este guia é voltado para quem utiliza o servidor de áudio Lavalink em modo **LOCAL** no EmpireMuse (`RUN_LOCAL_LAVALINK=true` ou sem nós externos configurados).

Quando configurado para execução local, o próprio bot gerencia o download de binários, verificação de integridade, configuração e inicialização do processo do Lavalink em segundo plano.

---

## 1. Stack e Versões Homologadas (Release 0.0.5)

O EmpireMuse utiliza uma stack moderna baseada em releases oficiais e plugins espelhados com hashes fixados. A release **0.0.5** atualiza o plugin Lyrics para a 2.7.1; a **0.0.4** manteve o Lavalink 4.2.2 e os plugins da 0.0.3 e acrescentou o plugin **Soundy** (SoundCloud), com um `application.yml` que desliga a fonte nativa do SoundCloud e fixa `logging.request.includePayload: false`. Desde a 0.0.3, o `application.yml` traz suporte ao cliente `TV` (essencial para contornar bloqueios anti-bot), OAuth desativado por padrão e nível de log de `dev.lavalink.youtube.http.YoutubeOauth2Handler` fixado em `WARN` (evitando vazamento de tokens nos logs):

| Componente | Versão / Tag | Origem e Observações |
| :--- | :--- | :--- |
| **Lavalink** | **4.2.2 oficial** | Versão estável com suporte a Java 17+ e protocolo de voz **DAVE (E2EE)**. Substitui builds de desenvolvimento (snapshots). |
| **Java / JDK** | **25 recomendado, 17 mínimo** | Requisito do Lavalink 4.2.2 (**Java 25 recomendado**, 17 é o mínimo). Caso Java ≥ 17 não seja detectado no sistema, o bot baixa um JDK sob demanda. |
| **youtube-plugin** | Snapshot `2be8e542` | Build fixado pós-1.18.2 com correções críticas para clientes mobile/web e suporte a cifra remota. |
| **LavaSrc** | **4.8.3** | Provedor de metadados e suporte a plataformas externas (Spotify e Deezer). |
| **Lyrics** | **2.7.1** (commit `d0dfbb13`) | Plugin oficial de letras de músicas (`dev.schlaubi.lyrics`), compilado do commit do mantenedor porque a tag 2.7.1 não compilava. |
| **Soundy** | **1.1.0** (`lowkwrs/soundy@e2c5da8`, MIT) | Fonte do SoundCloud (`scsearch:` e URLs `soundcloud.com`) com suporte a HLS fMP4, compilada do commit fixado; receita de recompilação no README do espelho. |

Os artefatos homologados são distribuídos no espelho oficial da organização:
- **Repositório de Binários**: [EmpireCommunity/EmpireMuse-LL-binaries](https://github.com/EmpireCommunity/EmpireMuse-LL-binaries/releases/tag/0.0.5)

---

## 2. Segurança de Supply Chain e Verificação de Integridade

Para garantir a confiabilidade dos binários executados localmente, o EmpireMuse aplica validações rigorosas em `utils/music/local_lavalink.py`:

### Verificação Obrigatória por SHA-256
- Cada arquivo oficial (`Lavalink.jar`, `application.yml`, plugins e pacotes portáteis do JDK) possui seu hash SHA-256 pré-computado e fixado no código (`utils/branding.py`).
- O download é realizado em arquivo temporário isolado e movido atomicamente para o destino final somente após confirmação do hash. Em caso de divergência, o arquivo corrompido é excluído e o processo é abortado imediatamente.

### Download Seguro e Extração Atômica do JDK
Caso Java 17+ não esteja disponível no sistema operacional do host, o bot gerencia o download de um JDK portátil da BellSoft Liberica com salvaguardas rigorosas (Liberica 25 LTS nas plataformas x64; o JDK 25 não tem build x86 32-bit, então as plataformas i586 ficam no último Liberica 21.0.x):
- **Hash SHA-256 fixo**: O arquivo compactado (`.zip` no Windows, `.tar.gz` no Linux) é baixado via `download_verified` e conferido estritamente contra `JDK_DOWNLOADS` de `utils/branding.py`. Arquivos já presentes no disco só são aproveitados se o SHA-256 conferir na íntegra.
- **Extração segura no Linux**: Membros do arquivo com componentes relativos (`..`) ou caminhos absolutos são rejeitados antes de qualquer extração, utilizando o filtro seguro `data` da biblioteca padrão `tarfile`.
- **Atomicidade e integridade**: A descompactação ocorre em diretório temporário isolado e só é promovida para a pasta definitiva através de substituição atômica (`os.replace`) após conclusão bem-sucedida. Instalações quebradas ou interrompidas são substituídas por completo e uma instalação existente só é considerada utilizável se `bin/java` existir e for executável.

### Opções da JVM do Lavalink local (GC e flags de acesso)
O bot monta a linha de comando do Java em `_java_command` (`utils/music/local_lavalink.py`), lendo o major da JVM de `java -version` (`java_major_version`). Todas as opções vêm antes de `-jar Lavalink.jar`, nesta ordem: processadores, `-Xmx`, `-Xms`, GC, flags de acesso, diretório temporário, log, flag que desliga o log do corpo das requisições (`-Dlogging.request.includePayload=false`) e override.
- **Coletor de lixo**: `-XX:+UseSerialGC` quando `LAVALINK_CPU_CORES` (também passado como `-XX:ActiveProcessorCount`) vale **1**; `-XX:+UseG1GC` com 2 ou mais. A escolha usa o valor configurado, não os núcleos do host. Sempre exatamente um GC. Não são usados `UseZGC`, `MaxRAMPercentage` nem AOT cache.
- **Flags de acesso**: `--enable-native-access=ALL-UNNAMED` e `--sun-misc-unsafe-memory-access=allow` só entram em **Java 24 e 25**, faixa em que as duas existem e foram testadas. Com Java 17 a 23, 26 ou superior, ou major ilegível, nenhuma das duas é passada: é melhor uma JVM que sobe com avisos do que uma que recusa a opção. A faixa só sobe junto com o JRE da imagem, depois de um novo teste de subida.
- **Tamanho do heap**: segue `LAVALINK_INITIAL_RAM` e `LAVALINK_RAM_LIMIT`, sem mudança.
- **Trust store da JRE**: na imagem Docker, as raízes emSign C1, ECC C3 e ECC G3 (CA eMudhra; a ECC G3 saiu do `certifi` na 2026.10.19 e a C1 e a ECC C3 na 2026.10.23) já não estão no `cacerts` do Temurin 25.0.5 (as versões anteriores as traziam e o build as removia com `keytool -delete`); o build confere isso e quebra se algum Temurin futuro voltar a confiar nelas. Fora do Docker, o JDK de reserva da Liberica ainda as confia; é um risco residual aceito.

### Isolamento do Ambiente do Subprocesso (Allowlist)
Ao iniciar o Lavalink local, o processo Java não herda as variáveis de ambiente arbitrárias do bot:
- **Allowlist restrita**: Passam apenas as variáveis essenciais do sistema operacional (`PATH`, `HOME`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TZ`, `TMPDIR`, `TEMP`, `TMP`, `JAVA_HOME` e `SERVER_PORT`; no Windows também `SYSTEMROOT`, `WINDIR`, `COMSPEC`, `PATHEXT`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `PROGRAMDATA`).
- **Credenciais do bot isoladas**: Tokens de bot, chaves de API, URIs de banco de dados e segredos de sessão nunca chegam ao processo do Lavalink.
- **Trio OAuth condicional**: As propriedades `PLUGINS_YOUTUBE_OAUTH_*` só são inseridas quando `YOUTUBE_OAUTH_REFRESH_TOKEN` contiver um token válido.
- **Configurações personalizadas da JVM e plugins**: Variáveis como `JAVA_TOOL_OPTIONS`, `_JAVA_OPTIONS`, proxies de rede, `LAVALINK_SERVER_PASSWORD` ou prefixos genéricos `PLUGINS_*` presentes no ambiente do bot não são repassadas ao Lavalink. Caso precise configurá-las, defina-as diretamente no arquivo `application.yml`.

### URLs do Jar e Regras de Download (`LAVALINK_FILE_URL` e `LAVALINK_FILE_SHA256`)
O parâmetro `LAVALINK_FILE_URL` define a origem do binário `Lavalink.jar`:
- **Vazio por padrão (recomendado)**: Se `LAVALINK_FILE_URL` estiver vazio no `.env` ou `config.json`, o bot utiliza automaticamente o espelho oficial correspondente à versão do código do EmpireMuse, sem exigir configuração manual de hash.
- **Atualização automática de versões anteriores**: Se a variável contiver uma URL oficial de versões anteriores do espelho (seja `0.0.1` ou `0.0.2`, inclusive sob o nome anterior do repositório `LL-binaries`), o bot reconhece o endereço oficial e atualiza sozinho para a versão atual, emitindo uma mensagem informativa no log recomendando esvaziar a variável.
- **Hospedagem própria ou URL customizada**: Se você optar por hospedar seu próprio `Lavalink.jar` em servidor próprio:
  - **HTTPS obrigatório**: O endereço precisa obrigatoriamente utilizar o esquema `https://`. Protocolos inseguros ou não autorizados são recusados.
  - **Hash obrigatório**: É mandatório definir `LAVALINK_FILE_SHA256='<hash-sha256>'` com o hash SHA-256 do seu arquivo. Se `LAVALINK_FILE_URL` apontar para um endereço externo sem o respectivo hash, o bot recusa o download preventivamente antes de abrir qualquer conexão de rede.

### Preservação do `application.yml` Personalizado
- Se um arquivo `application.yml` já existir no diretório raiz e seu conteúdo for personalizado pelo usuário (não correspondendo a nenhum dos hashes de versões oficiais conhecidas, como `0.0.1` ou `0.0.2`), o bot **não sobrescreve o arquivo**.
- Um aviso informativo é registrado no log e a sua configuração customizada é integralmente respeitada. Arquivos oficiais de versões anteriores são migrados de forma automática para a versão atual.

### Plugins Espelhados sem Download em Tempo de Execução
- Os jars dos quatro plugins homologados (`youtube-plugin`, LavaSrc, Lyrics e Soundy) são baixados pelo bot diretamente para o diretório `plugins/` com conferência de hash SHA-256.
- O `application.yml` declara os plugins com as mesmas versões dos jars locais, garantindo que o Lavalink não precise fazer requisições dinâmicas a repositórios externos (como o Maven Central) durante a inicialização.

---

## 3. Fontes de Áudio e Decisões de Arquitetura

### 3.1. YouTube: Cifra Remota e Clientes Suportados
- **Cifra Remota (`remoteCipher`)**: A extração local de assinaturas de áudio do YouTube tornou-se inviável devido a constantes atualizações de criptografia da plataforma. O Lavalink depende de um serviço de cifra remota (`yt-cipher`) para desembaralhar streams de áudio.
  - No ambiente atual, utiliza-se a instância pública de referência (`https://cipher.kikkia.dev/`).
  - Para ambientes de produção com alto tráfego, recomenda-se implantar uma instância própria do `yt-cipher`.
- **Clientes Otimizados**: O plugin do YouTube está configurado para operar com os clientes `[MUSIC, TV, ANDROID_VR, WEB, WEBEMBEDDED, TVHTML5_SIMPLY]`, assegurando maior estabilidade e contornando restrições de bloqueio. O cliente `TV` é o único que suporta autenticação OAuth.

### 3.2. Bloqueio Anti-bot e Autenticação OAuth Opcional
Desde agosto de 2026, o YouTube intensificou restrições automáticas contra clientes externos e scrapers ("Sign in to confirm you're not a bot", "This video requires login"):
1. **Padrão Seguro**: O `application.yml` oficial da release 0.0.5 mantém `plugins.youtube.oauth.enabled: false` e o nível de log de `dev.lavalink.youtube.http.YoutubeOauth2Handler` fixado em `WARN`. Isso previne que tokens ou credenciais sejam impressos nos arquivos de log e evita ciclos contínuos de erro HTTP 400 quando não há conta vinculada.
2. **OAuth com Conta Descartável**: Caso seu servidor sofra bloqueios de IP na reprodução do YouTube, é possível ativar a autenticação OAuth vinculando uma **conta Google descartável** através da variável `YOUTUBE_OAUTH_REFRESH_TOKEN` no `.env`.
   - Consulte o guia completo e passo a passo em: [Guia de Configuração do YouTube OAuth](youtube-oauth.md).

### 3.3. Ordem de Busca e SoundCloud
- A variável `SEARCH_PROVIDERS` tem como padrão `"ytsearch ytmsearch scsearch"`, priorizando o catálogo padrão do YouTube e do YouTube Music.
- **SoundCloud via Soundy**: a fonte nativa do lavaplayer falha no HLS fMP4 do SoundCloud (`Did not detect any supported formats`; upstream [lavaplayer#189](https://github.com/lavalink-devs/lavaplayer/issues/189) e [#202](https://github.com/lavalink-devs/lavaplayer/pull/202) em aberto). Por isso o `application.yml` mantém `lavalink.server.sources.soundcloud: false` e o plugin Soundy (`plugins.soundy.enabled: true`) registra o prefixo `scsearch:` e as URLs `soundcloud.com`. Se você usa um `application.yml` personalizado, replique essas duas chaves.

---

## 4. Procedimentos de Atualização

### Atualização do Lavalink (`@bot updatell`)
Para forçar o recarregamento do arquivo `Lavalink.jar` oficial e verificar a integridade dos binários:
1. Em um canal de texto acessível pelo bot, execute mencionando o bot:
   ```text
   @teubot updatell
   ```
2. O bot baixará a versão oficial mais recente homologada pelo projeto, validará o hash SHA-256 e atualizará os arquivos necessários.
3. Reinicie o processo do bot para que o novo servidor de áudio seja iniciado.

### Atualização do Código do Bot
O comando legado de atualização em tempo de execução via Git (`@bot update` / `source_update.sh`) foi desativado por razões de segurança e previsibilidade operacional.

Para atualizar o EmpireMuse:
- **Ambientes conteinerizados (Docker)**: Realize o pull da nova imagem/release oficial e recrie o contêiner.
- **Desenvolvimento local**: Execute `git pull origin main` no terminal com a aplicação parada e atualize as dependências com `uv pip install --require-hashes --build-constraint build-constraints.txt -r requirements-lock.txt`.

### Atualizar o JDK
Quando for necessário atualizar a versão ou a release dos pacotes portáteis do JDK da BellSoft para novas versões homologadas:
1. **Baixar o arquivo da BellSoft**:
   Baixe o pacote oficial da plataforma desejada (Windows amd64/i586 `.zip`, Linux amd64/i586 `.tar.gz`; Liberica 25 LTS no x64 e o último 21.0.x no i586) em um diretório temporário isolado (por exemplo, via `mktemp -d`), sem executar nem descompactar os arquivos.
2. **Conferir o SHA-1 publicado**:
   Valide a autenticidade do arquivo baixado contra o hash publicado pela BellSoft, consultando:
   - A API pública da BellSoft: `https://api.bell-sw.com/v1/liberica/releases` (verificando o campo `sha1` ou correspondente do artefato); ou
   - O arquivo `sha1sum.txt` oficial disponibilizado na página de release da BellSoft.
   Confira também o `digest` SHA-256 do asset na release `bell-sw/Liberica` do GitHub (`gh api repos/bell-sw/Liberica/releases/tags/<versão>`): o SHA-256 do arquivo de `download.bell-sw.com` precisa ser igual a ele.
3. **Calcular o SHA-256**:
   Calcule o hash SHA-256 do arquivo baixado localmente no terminal:
   ```bash
   sha256sum bellsoft-jdk*.tar.gz  # ou CertUtil -hashfile java.zip SHA256 no Windows
   ```
4. **Editar `branding.JDK_DOWNLOADS`**:
   No arquivo `utils/branding.py`, atualize a tupla `(url, sha256)` da respectiva plataforma no dicionário `JDK_DOWNLOADS` com a URL oficial da BellSoft e a sequência de 64 caracteres hexadecimais em minúsculas do SHA-256 conferido.
5. **Rodar os testes**:
   Execute os testes automatizados para certificar a conformidade dos metadados e das rotinas de verificação:
   ```bash
   .venv/bin/python -m pytest tests/brand/test_branding.py tests/audio/test_jdk_sha256_e_ambiente.py tests/audio/test_jdk_instalacao.py
   ```

---

## 5. Checklist de Release do Espelho (`EmpireMuse-LL-binaries`)

Ao publicar uma nova versão dos binários homologados no repositório [EmpireCommunity/EmpireMuse-LL-binaries](https://github.com/EmpireCommunity/EmpireMuse-LL-binaries), execute rigorosamente a checklist de integridade e governança abaixo:

1. **Publicação dos Artefatos**:
   - Compilar ou obter os artefatos oficiais homologados (`Lavalink.jar`, `application.yml` e jars dos plugins).
   - Gerar o arquivo `SHA256SUMS` com os hashes SHA-256 de todos os arquivos.
   - Publicar a release no GitHub sob a tag semântica (ex.: `0.0.4`) contendo todos os binários e o arquivo `SHA256SUMS`.
2. **Atualização do `utils/branding.py`**:
   - Atualizar a constante `LAVALINK_BINARIES_URL` para apontar para a nova tag de release.
   - Atualizar os hashes fixos `LAVALINK_JAR_SHA256`, `LAVALINK_APPLICATION_YML_SHA256` e o dicionário `LAVALINK_PLUGIN_SHA256` com base no `SHA256SUMS` conferido.
3. **Preservação de Versões Conhecidas do `application.yml`**:
   - Mover o hash do `application.yml` da release anterior para o conjunto `LAVALINK_APPLICATION_YML_KNOWN_SHA256`. Isso permite que o bot substitua automaticamente o arquivo padrão da versão anterior, sem sobrescrever arquivos que foram customizados pelos usuários.
4. **Preservação do Histórico de URLs Oficiais (`LAVALINK_JAR_OFFICIAL_URLS`)**:
   - Adicionar a nova URL oficial do `Lavalink.jar` ao conjunto `LAVALINK_JAR_OFFICIAL_URLS`.
   - **Manter compulsoriamente todas as URLs oficiais anteriores** (inclusive tags antigas como `0.0.1`, `0.0.2` e versões sob o nome antigo `LL-binaries`). Isso assegura que instalações antigas que tenham copiado a URL no `.env` sejam migradas de forma suave e automática, sem falhas de integridade no boot.
5. **Validação e Documentação**:
   - Atualizar as constantes correspondentes no teste de guarda `tests/brand/test_branding.py` e executar a suíte com `pytest`.
   - Atualizar as referências da release na documentação técnica: `CREDITS.md`, `docs/architecture.md`, `docs/development.md` e `docs/guides/atualizar-lavalink.md`.
   - Registrar as alterações na seção `[Unreleased]` de `CHANGELOG.md`.

