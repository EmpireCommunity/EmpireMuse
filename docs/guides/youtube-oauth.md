> Documentação técnica original do [EmpireMuse](https://github.com/EmpireCommunity/EmpireMuse) sobre contorno do anti-bot do YouTube e autenticação OAuth.

# Guia de Configuração do YouTube OAuth no Lavalink Local

Este guia orienta como configurar a autenticação OAuth opcional no servidor de áudio Lavalink para contornar restrições anti-bot e falhas de reprodução em faixas do YouTube.

---

## 1. Contexto e Bloqueio Anti-bot do YouTube

Desde agosto de 2026, o YouTube intensificou os mecanismos de mitigação contra reprodutores e scrapers automatizados de áudio na Web. Dentre os sintomas comuns observados em servidores Lavalink sem autenticação, destacam-se:

- Erros como `Sign in to confirm you're not a bot` (no cliente TVHTML5_SIMPLY);
- Mensagens de `This video requires login` (no cliente ANDROID_VR);
- Mensagens de `This video is unavailable` (no cliente WEBEMBEDDED);
- Respostas vazias ou sem formatos de áudio suportados (no cliente WEB).

Como o catálogo do YouTube é o provedor primário de reprodução do EmpireMuse (e plataformas como Spotify e Deezer utilizam o YouTube para obtenção do fluxo de áudio), esses bloqueios impedem a execução da maioria das faixas solicitadas.

---

## 2. A Solução: Cliente TV e Suporte a OAuth

No plugin oficial de reprodução do YouTube (`youtube-plugin`, snapshot fixado `2be8e542`), o **cliente `TV` é o único que suporta autenticação via OAuth** (`Tv.supportsOAuth() == true`). Clientes móveis ou web ignoram o token e operam sempre em modo anônimo.

A partir da release **0.0.3** do [EmpireCommunity/EmpireMuse-LL-binaries](https://github.com/EmpireCommunity/EmpireMuse-LL-binaries) (a atual é a **0.0.5**, com a mesma configuração):
- O `application.yml` padrão inclui o cliente `TV` na lista de clientes homologados (`MUSIC, TV, ANDROID_VR, WEB, WEBEMBEDDED, TVHTML5_SIMPLY`);
- A configuração `plugins.youtube.oauth.enabled` permanece desativada (`false`) por padrão para não gerar tentativas de autenticação inválidas;
- O nível de log da classe `dev.lavalink.youtube.http.YoutubeOauth2Handler` vem fixado em `WARN`, prevenindo que tokens apareçam nos registros do terminal ou em arquivos de log.

---

## 3. Regra Mandatória: Conta Google Descartável

> [!CAUTION]
> **USE EXCLUSIVAMENTE UMA CONTA GOOGLE DESCARTÁVEL (BURNER ACCOUNT).**
> **NUNCA** utilize sua conta Google pessoal principal, corporativa, institucional ou qualquer conta que contenha dados sensíveis, canais relevantes do YouTube ou e-mails importantes.
> O uso automatizado de contas contraria as políticas do YouTube e acarreta **risco real de restrição, suspensão ou banimento permanente da conta** por parte do Google.
> Ao criar a conta descartável, evite vincular números de telefone pessoais ou endereços de e-mail de recuperação associados à sua identidade real.

---

## 4. Como Obter o Refresh Token (Execução Única)

O refresh token precisa ser gerado apenas uma vez. Escolha um dos dois métodos abaixo:

### Método A: Comando do Bot (`@teubot ytoauth`)

O EmpireMuse possui um módulo integrado (`modules/ll_yt_oauth.py`) que implementa o fluxo oficial de autenticação de dispositivos do Google diretamente no Discord:

1. Como dono do bot (o ID deve estar listado em `OWNER_IDS` no `.env` ou `config.json`), mencione o bot com o comando:
   ```text
   @teubot ytoauth
   ```
2. O bot responderá com uma mensagem de confirmação e um botão intitulado **Vincular conta do google**. Clique no botão.
3. Uma mensagem efêmera (visível apenas para você) exibirá o link de autenticação com o código de usuário pré-preenchido, no formato:
   ```text
   https://www.google.com/device?user_code=XXXX-XXXX
   ```
4. Abra o link no navegador em uma janela anônima (para assegurar que fará login com a **conta descartável**).
5. Faça login com a conta Google descartável e autorize os escopos solicitados pelo cliente do YouTube.
6. Aguarde cerca de 10 a 15 segundos até que o bot confirme o recebimento do token.
7. O bot enviará, **por mensagem direta (DM) para você**, uma mensagem de conferência exibindo o nome e o e-mail da conta autorizada:
   - Verifique se o e-mail corresponde à **conta descartável**;
   - Clique em **Sim** para confirmar; caso tenha selecionado sua conta pessoal por engano, clique em **Não** imediatamente.
8. Após a confirmação, o bot enviará o `refresh_token` gerado, também por DM, em um bloco de código. Copie esse valor e guarde-o com segurança. As DMs do bot se apagam em 10 minutos e o canal recebe só um aviso.

> **Token que já apareceu num canal:** se, numa versão anterior, o token ou a confirmação da conta foram exibidos num canal de servidor, revogue o acesso na conta Google descartável e gere um novo token.

---

### Método B: Via Inicialização Temporária do Lavalink Local

Caso prefira gerar o token manualmente sem utilizar a interface do Discord:

1. Desde a release 0.0.3, o log de autenticação do plugin vem suprimido em `WARN`. Para visualizar a solicitação de emparelhamento no console, execute o Lavalink local temporariamente definindo no terminal:
   ```bash
   LOGGING_LEVEL_DEV_LAVALINK_YOUTUBE_HTTP_YOUTUBEOAUTH2HANDLER=INFO PLUGINS_YOUTUBE_OAUTH_ENABLED=true java -jar Lavalink.jar
   ```
2. Ao inicializar, o plugin exibirá mensagens no terminal com o endereço `https://www.google.com/device` e o código de dispositivo (exemplo: `XXXX-XXXX`).
3. Acesse `https://www.google.com/device` no navegador conectado à sua **conta descartável**, insira o código e conclua a autorização.
4. O console do Lavalink imprimirá uma linha informando o `refreshToken` obtido.
5. Copie o token e pressione `Ctrl+C` imediatamente para encerrar o processo.
6. **Atenção**: Limpe ou apague o arquivo de log local (`.logs/lavalink/spring.log`) para evitar que a credencial fique persistida em texto plano no disco.

---

## 5. Configuração no `.env` (`YOUTUBE_OAUTH_REFRESH_TOKEN`)

Com o token copiado, adicione ou atualize a seguinte linha no seu arquivo `.env`:

```env
# Refresh token da conta Google descartável para o cliente TV do Lavalink
YOUTUBE_OAUTH_REFRESH_TOKEN="1//04xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

### Isolamento e Segurança do Token no Modo Local
- **Isolamento de Ambiente e Injeção em Memória**: O processo do Lavalink local não herda as variáveis de ambiente arbitrárias do bot (`os.environ`). Ele é executado via `subprocess.Popen` com um ambiente restrito por *allowlist* (`lavalink_child_env`), contendo exclusivamente variáveis essenciais do sistema operacional e `SERVER_PORT`.
- **Injeção Condicional do OAuth**: Quando a variável `YOUTUBE_OAUTH_REFRESH_TOKEN` é informada com um token válido, o bot injeta as propriedades do Spring (`PLUGINS_YOUTUBE_OAUTH_ENABLED=true`, `PLUGINS_YOUTUBE_OAUTH_REFRESH_TOKEN` e `PLUGINS_YOUTUBE_OAUTH_SKIP_INITIALIZATION=true`) exclusivamente no dicionário de ambiente isolado do subprocesso Java. Sem token configurado, nenhuma dessas três variáveis é passada ao Lavalink, mesmo se existirem no ambiente do bot.
- **Zero Persistência em Arquivo**: O token **não** é gravado no `application.yml`, **não** é passado em argumentos de linha de comando e **não** é gravado nos logs do bot. Demais segredos do bot (tokens do Discord, credenciais de banco e chaves de sessão) permanecem estritamente isolados e inacessíveis ao processo do Lavalink.

### Servidores Lavalink Externos (Remoto / VPS)

Se o Lavalink roda em outro contêiner sob o nome `LOCAL`, configure o token no `application.yml` desse nó.

Se você utiliza um nó Lavalink externo em outra máquina ou VPS (onde o bot não gerencia o processo Java local):
- O bot não tem controle sobre o ambiente do servidor remoto;
- Configure a variável `PLUGINS_YOUTUBE_OAUTH_REFRESH_TOKEN` diretamente nas variáveis de ambiente do serviço do seu servidor Lavalink ou no respectivo `application.yml` remoto.

---

## 6. Atenção aos Níveis de Log (Não Ativar `DEBUG`)

> [!WARNING]
> **NUNCA ative o nível de log `DEBUG` para o pacote `dev.lavalink.youtube` em produção.**
> O plugin do YouTube imprime o token de autorização completo no log quando executado em modo de depuração.
> Mantenha sempre o nível padrão configurado nas releases (`WARN` para `YoutubeOauth2Handler`).

---

## 7. Como Revogar o Acesso da Conta

Caso suspeite de vazamento do token ou deseje desconectar a conta do servidor:

1. Acesse a Central de Contas do Google em [myaccount.google.com](https://myaccount.google.com/) autenticado na **conta descartável**.
2. Acesse a aba **Segurança**.
3. Navegue até a seção **Seus vínculos com apps e serviços de terceiros**.
4. Localize a aplicação de vinculação do YouTube e selecione **Excluir todos os vínculos**.
5. No arquivo `.env` do bot, esvazie a chave `YOUTUBE_OAUTH_REFRESH_TOKEN=` e reinicie a aplicação.
